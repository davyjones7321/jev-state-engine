import ast
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

from pydantic import BaseModel, Field

from jev.models import CompileOutcome, MechanicalCheckResult, Subgoal, TestOutcome
from jev.process_runner import CommandResult, run_command

PROTECTED_PATHS = (".git/", ".jev/")


class PathCollisionError(RuntimeError):
    """Raised when an engine command modifies or deletes a worker-staged file."""
    pass


class StagedPathsList(list):
    """List-like container for worker_staged_paths that also indexes by path to return sha256."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hashes: Dict[str, str] = {}

    def record(self, path: str, sha256_hash: str) -> None:
        if path not in self:
            self.append(path)
        self._hashes[path] = sha256_hash

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, str):
            return self._hashes[item]
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        return self._hashes.get(key, default)

    def items(self):
        return ((p, self._hashes.get(p)) for p in self)

    def clear(self) -> None:
        super().clear()
        self._hashes.clear()

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 40_000


def parse_failing_test_ids(output: str, ecosystem: Optional[str] = None, exit_code: Optional[int] = None) -> Optional[Set[str]]:
    """Parse failing test identifiers from test runner output.

    Supports pytest, unittest, jest/vitest, go, and cargo.
    Returns:
    - Set[str] of failing test IDs if parsed
    - set() if output indicates clean pass (no failures)
    - None if output indicates test failure but no specific IDs could be parsed (fail-closed)
    """
    if not output and (exit_code is None or exit_code == 0):
        return set()
    if not output and exit_code is not None and exit_code != 0:
        return None

    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)
    ids: Set[str] = set()

    # 1. Pytest
    for match in re.finditer(r"(?:^|\n)\s*(?:FAILED|ERROR)\s+([^\s\n\r]+)", clean):
        token = match.group(1).strip()
        if token and not token.startswith("(") and not token.startswith("="):
            token = token.rstrip(":,")
            ids.add(token)

    for match in re.finditer(r"(?:^|\n)\s*([^\s\n\r]+::[^\s\n\r]+)\s+FAILED", clean):
        token = match.group(1).strip().rstrip(":,")
        if token:
            ids.add(token)

    # 2. Unittest
    for match in re.finditer(r"(?:^|\n)\s*(?:FAIL|ERROR):\s+([a-zA-Z0-9_]+)\s+\(([^)]+)\)", clean):
        func_name = match.group(1).strip()
        cls_name = match.group(2).strip()
        ids.add(f"{cls_name}.{func_name}")

    # 3. Jest / Vitest
    for match in re.finditer(r"(?:^|\n)\s*(?:[✕×]|●)\s+([^\n\r]+)", clean):
        token = match.group(1).strip()
        if token:
            ids.add(token)

    for match in re.finditer(r"(?:^|\n)\s*FAIL\s+([^\s\n\r]+)", clean):
        token = match.group(1).strip().rstrip(":,")
        if token and ("." in token or "/" in token):
            ids.add(token)

    # 4. Go test
    for match in re.finditer(r"---\s+FAIL:\s+([^\s\(\)]+)", clean):
        token = match.group(1).strip()
        if token:
            ids.add(token)

    # 5. Cargo test
    for match in re.finditer(r"(?:^|\n)\s*test\s+([^\s]+)\s+\.\.\.\s+FAILED", clean):
        token = match.group(1).strip()
        if token:
            ids.add(token)

    if exit_code is not None and exit_code != 0:
        if ids:
            return ids
        return None

    lower = clean.lower()
    failure_indicators = [
        "failed", "failure", "error", "traceback", "exception", "timed out",
        "command exited", "process exited"
    ]
    has_failure = any(ind in lower for ind in failure_indicators)

    if has_failure:
        if ids:
            return ids
        return None

    return set()



def _cap_read_text(text: str) -> str:
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n[truncated: showing first {MAX_READ_CHARS} of {len(text)} characters]"
    return text


def _run_test_subprocess(
    cmd: List[str],
    cwd: Union[str, Path],
    timeout: int,
) -> subprocess.CompletedProcess:
    res = run_command(cmd, cwd=cwd, timeout=timeout)
    if res.timed_out:
        raise RuntimeError(f"Test command timed out after {timeout} seconds and its process tree was terminated")
    if res.launch_failed:
        raise FileNotFoundError(f"Command not found: {cmd}")
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=res.exit_code if res.exit_code is not None else -1,
        stdout=res.stdout,
        stderr=res.stderr,
    )



class MainDivergedError(RuntimeError):
    """Raised when main branch diverged from base_commit or cannot be fast-forwarded."""
    pass


class TrackedModificationsError(RuntimeError):
    """Raised when working tree has tracked modifications preventing fast-forward."""
    pass


@dataclass(frozen=True)
class DiagnosticError:
    raw_line: str
    file: str
    code: str
    message: str
    line: Optional[int] = None
    column: Optional[int] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.code, self.message)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __iter__(self):
        return iter((self.file, self.code, self.message))


class BuildCheckResult(BaseModel):
    passed: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed


class SuppressionCheckResult(BaseModel):
    passed: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed

    def __iter__(self):
        return iter((self.passed, self.detail))


class UnresolvedImportCheckResult(BaseModel):
    passed: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed

    def __iter__(self):
        return iter((self.passed, self.detail))


@dataclass(frozen=True)
class AddedDiffLine:
    file: str
    line_number: int
    text: str


class ScopeCheckResult(BaseModel):
    passed: bool
    out_of_scope: List[str] = Field(default_factory=list)
    detail: str = ""

    def __bool__(self) -> bool:
        return self.passed


def _is_junction_or_symlink(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    if sys.platform == "win32":
        try:
            st = os.stat(str(path), follow_symlinks=False)
            if hasattr(st, "st_file_attributes") and (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except Exception:
            pass
    return False


ALL_SOURCE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go",
    ".rs",
    ".java", ".kt",
    ".py",
}

SUPPRESSION_DIRECTIVES: Dict[str, List[str]] = {
    ".ts": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".tsx": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".js": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".jsx": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".mjs": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".cjs": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".mts": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".cts": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".vue": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".svelte": ["@ts-ignore", "@ts-nocheck", "@ts-expect-error"],
    ".py": ["# type: ignore", "# mypy: ignore-errors", "# pyright: ignore"],
    ".pyi": ["# type: ignore", "# mypy: ignore-errors", "# pyright: ignore"],
    ".go": [],
    ".rs": [],
    ".java": [],
    ".kt": [],
    ".kts": [],
}

UNRESOLVED_IMPORT_RESOLVERS: Dict[str, Optional[str]] = {
    ".ts": "js_ts",
    ".tsx": "js_ts",
    ".js": "js_ts",
    ".jsx": "js_ts",
    ".mjs": "js_ts",
    ".cjs": "js_ts",
    ".mts": "js_ts",
    ".cts": "js_ts",
    ".vue": "js_ts",
    ".svelte": "js_ts",
    ".py": None,
    ".go": None,
    ".rs": None,
    ".java": None,
    ".kt": None,
}

NODE_BUILTIN_MODULES = {
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "fs/promises", "http", "http2",
    "https", "inspector", "module", "net", "os", "path", "path/posix",
    "path/win32", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "stream/consumers", "stream/promises",
    "stream/web", "string_decoder", "sys", "timers", "timers/promises",
    "tls", "trace_events", "tty", "url", "util", "util/types", "v8",
    "vm", "wasi", "worker_threads", "zlib", "test"
}

NPM_PACKAGE_PATTERN = re.compile(r"^(?:@[a-z0-9][a-z0-9_.-]*/)?[a-z0-9][a-z0-9_.-]*$")


class BaseCompileHandler:
    name: str = ""
    source_extensions: set[str] = set()

    def matches(self, touched_files: set[str], wt: Path, repo_dir: Path) -> bool:
        return any(Path(f).suffix.lower() in self.source_extensions for f in touched_files)

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        raise NotImplementedError

    def run_command(
        self,
        cmd: List[str],
        cwd: Path,
        touched_files: List[str],
        is_base: bool = False,
        base_commit: Optional[str] = None,
        timeout: Optional[int] = 120,
    ) -> tuple[int, str, str]:
        exec_cmd = cmd
        if cmd:
            resolved = shutil.which(cmd[0])
            if resolved:
                exec_cmd = [resolved] + cmd[1:]
        res = run_command(exec_cmd, cwd=cwd, timeout=timeout)
        if res.timed_out:
            raise subprocess.TimeoutExpired(cmd=exec_cmd, timeout=timeout or 120)
        if res.launch_failed:
            raise FileNotFoundError(f"Command not found: {cmd}")
        return res.exit_code if res.exit_code is not None else -1, res.stdout, res.stderr


class JsTsCompileHandler(BaseCompileHandler):
    name = "js_ts"
    source_extensions = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

    def matches(self, touched_files: set[str], wt: Path, repo_dir: Path) -> bool:
        has_ext = any(Path(f).suffix.lower() in self.source_extensions for f in touched_files)
        has_manifest = (
            (wt / "package.json").exists()
            or (repo_dir / "package.json").exists()
            or (wt / "tsconfig.json").exists()
            or (repo_dir / "tsconfig.json").exists()
        )
        return has_ext and has_manifest

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        if (wt / "yarn.lock").exists() or (repo_dir / "yarn.lock").exists():
            pm = "yarn"
        elif (wt / "pnpm-lock.yaml").exists() or (repo_dir / "pnpm-lock.yaml").exists():
            pm = "pnpm"
        else:
            pm = "npm"

        pkg_json_path = wt / "package.json"
        if not pkg_json_path.exists():
            pkg_json_path = repo_dir / "package.json"

        scripts = {}
        if pkg_json_path.exists():
            try:
                pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(pkg_data, dict):
                    scripts = pkg_data.get("scripts") or {}
            except Exception:
                pass

        has_tsconfig = (wt / "tsconfig.json").exists() or (repo_dir / "tsconfig.json").exists()
        has_typecheck = "typecheck" in scripts
        has_build = "build" in scripts

        # Can a compile command be determined?
        if not has_typecheck and not has_tsconfig and not has_build:
            return None, CompileOutcome.NO_COMPILE_COMMAND, "No compile command found: package.json has no 'typecheck' or 'build' script, and no tsconfig.json exists."

        # A compile command can be determined! Check if dependencies are ready:
        has_nm = (
            (wt / "node_modules").exists()
            or (repo_dir / "node_modules").exists()
            or _is_junction_or_symlink(wt / "node_modules")
            or _is_junction_or_symlink(repo_dir / "node_modules")
        )
        if not has_nm:
            return None, CompileOutcome.ENV_NOT_READY, "Environment not ready: node_modules is missing in repository root. Please install dependencies before running."

        if "typecheck" in scripts:
            return [pm, "run", "typecheck"], None, ""

        if has_tsconfig:
            tsc_bin = None
            for candidate in [
                wt / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc"),
                repo_dir / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc"),
            ]:
                if candidate.exists():
                    tsc_bin = str(candidate)
                    break
            if not tsc_bin:
                tsc_which = shutil.which("tsc")
                if tsc_which:
                    tsc_bin = tsc_which

            if tsc_bin:
                return [tsc_bin, "--noEmit"], None, ""
            if shutil.which("npx"):
                return ["npx", "tsc", "--noEmit"], None, ""
            return ["tsc", "--noEmit"], None, ""

        if "build" in scripts:
            return [pm, "run", "build"], None, ""

        return None, CompileOutcome.NO_COMPILE_COMMAND, "No compile command found: package.json has no 'typecheck' or 'build' script, and no tsconfig.json exists."


class GoCompileHandler(BaseCompileHandler):
    name = "go"
    source_extensions = {".go"}

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        if not (wt / "go.mod").exists() and not (repo_dir / "go.mod").exists():
            return None, CompileOutcome.NO_COMPILE_COMMAND, "No go.mod found for Go source files."
        return ["go", "build", "./..."], None, ""


class RustCompileHandler(BaseCompileHandler):
    name = "rust"
    source_extensions = {".rs"}

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        if not (wt / "Cargo.toml").exists() and not (repo_dir / "Cargo.toml").exists():
            return None, CompileOutcome.NO_COMPILE_COMMAND, "No Cargo.toml found for Rust source files."
        return ["cargo", "check"], None, ""


class MavenCompileHandler(BaseCompileHandler):
    name = "maven"
    source_extensions = {".java", ".kt"}

    def matches(self, touched_files: set[str], wt: Path, repo_dir: Path) -> bool:
        has_ext = any(Path(f).suffix.lower() in self.source_extensions for f in touched_files)
        return has_ext and ((wt / "pom.xml").exists() or (repo_dir / "pom.xml").exists())

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        mvn_cmd = "mvn"
        if (wt / "mvnw").exists():
            mvn_cmd = "./mvnw"
        elif (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        elif (repo_dir / "mvnw").exists():
            mvn_cmd = "./mvnw"
        elif (repo_dir / "mvnw.cmd").exists():
            mvn_cmd = str(repo_dir / "mvnw.cmd")
        return [mvn_cmd, "-q", "compile"], None, ""


class GradleCompileHandler(BaseCompileHandler):
    name = "gradle"
    source_extensions = {".java", ".kt"}

    def matches(self, touched_files: set[str], wt: Path, repo_dir: Path) -> bool:
        has_ext = any(Path(f).suffix.lower() in self.source_extensions for f in touched_files)
        has_gradle = (
            (wt / "build.gradle").exists()
            or (wt / "build.gradle.kts").exists()
            or (repo_dir / "build.gradle").exists()
            or (repo_dir / "build.gradle.kts").exists()
        )
        return has_ext and has_gradle

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        gradle_cmd = "gradle"
        if (wt / "gradlew").exists():
            gradle_cmd = "./gradlew"
        elif (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        elif (repo_dir / "gradlew").exists():
            gradle_cmd = "./gradlew"
        elif (repo_dir / "gradlew.bat").exists():
            gradle_cmd = str(repo_dir / "gradlew.bat")
        return [gradle_cmd, "classes", "-q"], None, ""


class JavaKotlinFallbackHandler(BaseCompileHandler):
    name = "java_kotlin"
    source_extensions = {".java", ".kt"}

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        return None, CompileOutcome.NO_COMPILE_COMMAND, "No Maven (pom.xml) or Gradle (build.gradle) build file found for Java/Kotlin source changes."


class PythonCompileHandler(BaseCompileHandler):
    name = "python"
    source_extensions = {".py"}

    def prepare_and_get_command(
        self, wt: Path, repo_dir: Path, touched_files: List[str]
    ) -> tuple[Optional[List[str]], Optional[CompileOutcome], str]:
        return ["ast.parse"], None, ""

    def run_command(
        self,
        cmd: List[str],
        cwd: Path,
        touched_files: List[str],
        is_base: bool = False,
        base_commit: Optional[str] = None,
    ) -> tuple[int, str, str]:
        for f in touched_files:
            rel = Path(f).as_posix()
            if not rel.endswith(".py"):
                continue
            if is_base:
                commit = base_commit or "HEAD"
                res = subprocess.run(
                    ["git", "show", f"{commit}:{rel}"],
                    cwd=cwd,
                    capture_output=True,
                )
                if res.returncode != 0:
                    continue
                source_bytes = res.stdout
            else:
                p = Path(f)
                if not p.is_absolute():
                    p = cwd / p
                if not p.exists():
                    continue
                try:
                    source_bytes = p.read_bytes()
                except Exception:
                    continue

            try:
                ast.parse(source_bytes, filename=rel)
            except SyntaxError as e:
                err_msg = f"SyntaxError in {rel}: {e.msg} (line {e.lineno})"
                return 1, "", err_msg
            except Exception as e:
                return 1, "", f"Compile error in {rel}: {str(e)}"
        return 0, "", ""


ECOSYSTEM_COMPILE_HANDLERS = [
    JsTsCompileHandler(),
    GoCompileHandler(),
    RustCompileHandler(),
    MavenCompileHandler(),
    GradleCompileHandler(),
    JavaKotlinFallbackHandler(),
    PythonCompileHandler(),
]


@dataclass(frozen=True)
class ResolvedTestCommand:
    ecosystem: Optional[str]
    command: Optional[List[str]]
    source: str
    reason: Optional[str] = None


def compare_test_runs(before: Optional[dict], after: Optional[dict]) -> dict:
    """Compare recorded baseline and candidate outcomes without executing tests."""
    baseline = before.get("outcome") if before else None
    candidate = after.get("outcome") if after else None
    return {
        "before": baseline,
        "after": candidate,
        "regressed": baseline == TestOutcome.PASSED.value and candidate != TestOutcome.PASSED.value,
        "improved": baseline is not None and baseline != TestOutcome.PASSED.value and candidate == TestOutcome.PASSED.value,
    }


def resolve_test_command(wt: Union[str, Path]) -> ResolvedTestCommand:
    """Detects the test command from repository manifests without running subprocesses or writing files.

    Returns a ResolvedTestCommand with ecosystem, command (list or None), source (rule/file matched),
    and reason (explanation if command is None).
    """
    wt = Path(wt)

    # 1. package.json test script
    pkg_json_path = wt / "package.json"
    if pkg_json_path.exists():
        try:
            pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pkg_data, dict):
                scripts = pkg_data.get("scripts")
                if isinstance(scripts, dict) and "test" in scripts:
                    if (wt / "yarn.lock").exists():
                        return ResolvedTestCommand(ecosystem="yarn", command=["yarn", "test"], source="package.json")
                    elif (wt / "pnpm-lock.yaml").exists():
                        return ResolvedTestCommand(ecosystem="pnpm", command=["pnpm", "test"], source="package.json")
                    else:
                        return ResolvedTestCommand(ecosystem="npm", command=["npm", "test"], source="package.json")
        except Exception as e:
            return ResolvedTestCommand(
                ecosystem="package_json",
                command=None,
                source="package.json",
                reason=f"Failed to parse package.json: {e}",
            )

    # 2. go.mod
    if (wt / "go.mod").exists():
        return ResolvedTestCommand(ecosystem="go", command=["go", "test", "./..."], source="go.mod")

    # 3. Cargo.toml
    if (wt / "Cargo.toml").exists():
        return ResolvedTestCommand(ecosystem="cargo", command=["cargo", "test"], source="Cargo.toml")

    # 4. pom.xml
    if (wt / "pom.xml").exists():
        mvn_cmd = "mvn"
        if sys.platform == "win32" and (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        elif (wt / "mvnw").exists():
            mvn_cmd = "./mvnw"
        elif (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        return ResolvedTestCommand(ecosystem="maven", command=[mvn_cmd, "test"], source="pom.xml")

    # 5. build.gradle / build.gradle.kts
    if (wt / "build.gradle").exists() or (wt / "build.gradle.kts").exists():
        gradle_cmd = "gradle"
        if sys.platform == "win32" and (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        elif (wt / "gradlew").exists():
            gradle_cmd = "./gradlew"
        elif (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        source = "build.gradle" if (wt / "build.gradle").exists() else "build.gradle.kts"
        return ResolvedTestCommand(ecosystem="gradle", command=[gradle_cmd, "test"], source=source)

    # 6. pytest signals
    has_pytest = False
    pytest_source = None
    if (wt / "pytest.ini").exists():
        has_pytest = True
        pytest_source = "pytest.ini"
    elif (wt / "pyproject.toml").exists():
        try:
            pyproj_text = (wt / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest" in pyproj_text:
                has_pytest = True
                pytest_source = "pyproject.toml"
        except Exception:
            pass
    if not has_pytest:
        try:
            has_pytest = (
                any(wt.glob("test_*.py"))
                or any(wt.glob("*_test.py"))
                or ((wt / "tests").is_dir() and (any((wt / "tests").glob("*.py")) or any((wt / "tests").rglob("*.py"))))
            )
            if has_pytest:
                pytest_source = "python_test_files"
        except Exception:
            pass

    if has_pytest:
        return ResolvedTestCommand(
            ecosystem="pytest",
            command=[sys.executable, "-m", "pytest", "-q"],
            source=pytest_source or "pytest",
        )

    # 7. IaC signals
    # Terraform
    has_tf = False
    try:
        has_tf = any(wt.glob("*.tf")) or any(p for p in wt.rglob("*.tf") if ".terraform" not in p.parts)
    except Exception:
        pass

    if has_tf:
        return ResolvedTestCommand(ecosystem="terraform", command=["terraform", "validate"], source="terraform")

    # Ansible
    has_ansible = (
        (wt / "ansible.cfg").exists()
        or (wt / "playbooks").is_dir()
        or any(wt.glob("playbook*.yml"))
        or any(wt.glob("playbook*.yaml"))
    )
    if has_ansible:
        if shutil.which("ansible-lint"):
            return ResolvedTestCommand(ecosystem="ansible", command=["ansible-lint"], source="ansible")
        elif shutil.which("ansible-playbook"):
            playbooks = list(wt.glob("playbook*.yml")) + list(wt.glob("playbook*.yaml"))
            pb_arg = str(playbooks[0]) if playbooks else "."
            return ResolvedTestCommand(
                ecosystem="ansible",
                command=["ansible-playbook", "--syntax-check", pb_arg],
                source="ansible",
            )
        else:
            return ResolvedTestCommand(ecosystem="ansible", command=["ansible-lint"], source="ansible")

    # Helm
    if (wt / "Chart.yaml").exists():
        return ResolvedTestCommand(ecosystem="helm", command=["helm", "lint", "."], source="Chart.yaml")

    # 8. Fallback
    return ResolvedTestCommand(
        ecosystem=None,
        command=None,
        source="none",
        reason="No test framework or test files detected",
    )


class Workspace:
    SUPPRESSION_DIRECTIVES = SUPPRESSION_DIRECTIVES
    UNRESOLVED_IMPORT_RESOLVERS = UNRESOLVED_IMPORT_RESOLVERS

    def __init__(
        self,
        repo_dir: Optional[Union[str, Path]] = None,
        worktree_dir: Optional[Union[str, Path]] = None,
        test_timeout: Optional[int] = None,
    ):
        self.repo_dir = Path(repo_dir).resolve() if repo_dir else Path.cwd().resolve()
        self.worktree_dir = (
            Path(worktree_dir).resolve() if worktree_dir else self.repo_dir
        )
        self.base_commit: Optional[str] = self._get_head_commit()
        self.last_test_run: Optional[dict] = None
        self.last_compile_run: Optional[dict] = None
        self._baseline_test_cache: Dict[str, Dict[str, Any]] = {}
        self.worker_staged_paths = StagedPathsList()
        self.worker_staged_hashes = self.worker_staged_paths._hashes
        self.engine_touched_paths: List[str] = []
        self.collision_paths: List[str] = []

        if test_timeout is not None:
            try:
                ival = int(test_timeout)
                if ival <= 0:
                    raise ValueError()
                self.test_timeout = ival
            except (ValueError, TypeError):
                raise ValueError(f"test_timeout must be a positive integer, got {test_timeout}")
        else:
            env_val = os.environ.get("JEV_TEST_TIMEOUT")
            if env_val is not None:
                try:
                    ival = int(env_val)
                    if ival <= 0:
                        raise ValueError()
                    self.test_timeout = ival
                except (ValueError, TypeError):
                    logger.warning("Invalid JEV_TEST_TIMEOUT '%s': must be a positive integer; falling back to 600", env_val)
                    self.test_timeout = 600
            else:
                self.test_timeout = 600

    def _run_test_subprocess(
        self,
        cmd: List[str],
        cwd: Union[str, Path],
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        eff_timeout = timeout if timeout is not None else self.test_timeout
        return _run_test_subprocess(cmd, cwd=cwd, timeout=eff_timeout)


    def _record_test_run(
        self,
        ecosystem: Optional[str],
        command: Optional[List[str]],
        exit_code: Optional[int],
        stdout: str = "",
        stderr: str = "",
        outcome: TestOutcome = TestOutcome.NO_TESTS_COLLECTED,
        executed_command: Optional[List[str]] = None,
    ) -> TestOutcome:
        stdout_tail = stdout[-1000:] if stdout else ""
        stderr_tail = stderr[-1000:] if stderr else ""
        output_tail = stdout_tail
        if stderr_tail:
            output_tail = f"{output_tail}\n[stderr]\n{stderr_tail}".strip() if output_tail else stderr_tail

        full_output = f"{stdout}\n{stderr}" if stderr else stdout
        parsed_ids = parse_failing_test_ids(full_output, ecosystem=ecosystem, exit_code=exit_code)
        if outcome == TestOutcome.PASSED or exit_code == 0:
            parsed_ids = set()
        elif (outcome == TestOutcome.FAILED or (exit_code is not None and exit_code != 0)):
            if parsed_ids is not None and len(parsed_ids) == 0:
                parsed_ids = None

        self.last_test_run = {
            "ecosystem": ecosystem,
            "command": command,
            "executed_command": executed_command if executed_command is not None else command,
            "exit_code": exit_code,
            # Bounded evidence: short output is exact; longer output is explicitly marked as truncated.
            "stdout": stdout_tail,
            "stderr": stderr_tail,
            "stdout_truncated": len(stdout) > 1000,
            "stderr_truncated": len(stderr) > 1000,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "output_tail": output_tail,
            "outcome": outcome.value if hasattr(outcome, "value") else str(outcome),
            "failing_test_ids": list(parsed_ids) if parsed_ids is not None else None,
        }
        return outcome

    def _get_head_commit(self) -> Optional[str]:
        res = self._run_repo_git(["rev-parse", "HEAD"])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        return None

    def get_current_head(self) -> str:
        sha = self._get_head_commit()
        if not sha:
            raise RuntimeError("Could not resolve current HEAD")
        return sha

    def _run_git(self, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=self.worktree_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def _run_repo_git(self, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def resolve_in_worktree(self, path: Union[str, Path]) -> Path:
        """Resolves path within worktree_dir, enforcing strict containment.

        Rejects:
        - Absolute paths (POSIX starting with '/', Windows drive letters e.g. 'C:', UNC paths)
        - Paths with '..' segments (relative parent traversal)
        - Symlinks pointing outside worktree_dir
        - Backslash or mixed-slash parent traversal
        Preserves:
        - Dot-files like '.env', '.gitignore'
        - Leading relative directory paths like '.github/workflows/ci.yml'
        """
        import posixpath
        raw_str = str(path).strip()
        norm_posix = raw_str.replace("\\", "/")
        worktree_root = self.worktree_dir.resolve()

        # Paths are an untrusted, platform-independent relative-path
        # contract. Never resolve an absolute path and accept it merely
        # because it happens to point below the worktree.
        if (
            norm_posix.startswith("/")
            or re.match(r"^[a-zA-Z]:", raw_str)
            or Path(path).is_absolute()
        ):
            raise ValueError(f"Absolute path '{path}' is not allowed within worktree.")

        if norm_posix.startswith("./"):
            stripped = norm_posix[2:]
        else:
            stripped = norm_posix

        normalized = posixpath.normpath(stripped)
        parts = normalized.split("/")
        if ".." in parts or normalized == ".." or normalized.startswith("../"):
            raise ValueError(f"Path '{path}' traverses outside worktree.")

        candidate = self.worktree_dir / normalized
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(worktree_root)
        except ValueError:
            raise PermissionError(f"Path '{path}' resolves outside worktree to '{resolved_candidate}'.")

        return candidate

    def _resolve_for_read(self, path: Union[str, Path]) -> Path:
        """Resolves path for reading in run_read_tool only.

        Enforces strict worktree containment by default via resolve_in_worktree(),
        but allows reads that resolve strictly inside <repo_dir>/node_modules.
        Traversal using '..' (e.g. node_modules/../../x) is strictly blocked.
        """
        import posixpath
        raw_str = str(path).strip()
        norm_posix = raw_str.replace("\\", "/")
        try:
            return self.resolve_in_worktree(path)
        except (ValueError, PermissionError) as original_err:
            # A subgoal worktree may expose repository node_modules through a
            # symlink. Keep that read-only exception, but not for absolute or
            # traversing paths.
            if (
                norm_posix.startswith("/")
                or re.match(r"^[a-zA-Z]:", raw_str)
                or Path(path).is_absolute()
            ):
                raise original_err

            stripped = norm_posix[2:] if norm_posix.startswith("./") else norm_posix
            normalized = posixpath.normpath(stripped)
            parts = normalized.split("/")
            if ".." in parts or normalized == ".." or normalized.startswith("../"):
                raise ValueError(f"Path '{path}' traverses outside workspace.")

            nm_dir = self.repo_dir / "node_modules"
            if not nm_dir.exists():
                raise original_err
            candidate = (self.worktree_dir / normalized).resolve()
            try:
                candidate.relative_to(nm_dir.resolve())
                return candidate
            except ValueError:
                raise original_err

    def run_read_tool(self, cmd: str, args: Optional[List[str]] = None) -> str:
        """Executes read-only CLI commands in worktree_dir and returns stdout or stderr."""
        args_list = args or []
        full_cmd = [cmd] + args_list

        resolved_target: Optional[Path] = None
        # Enforce path containment on file reading commands (e.g. cat) across all platforms
        if cmd == "cat" and args_list:
            resolved_args: List[str] = []
            for arg in args_list:
                if not arg.startswith("-"):
                    try:
                        resolved_target = self._resolve_for_read(arg)
                        resolved_args.append(str(resolved_target))
                    except (ValueError, PermissionError) as e:
                        return f"cat: {arg}: Access denied outside workspace: {e}"
                else:
                    resolved_args.append(arg)

            if not shutil.which("cat"):
                target = resolved_target if resolved_target is not None else (self.worktree_dir / args_list[0])
                if target.is_dir():
                    return f"cat: {args_list[0]}: Is a directory"
                if target.exists() and target.is_file():
                    try:
                        return _cap_read_text(target.read_text(encoding="utf-8", errors="replace"))
                    except Exception as e:
                        return str(e)
                return f"cat: {args_list[0]}: No such file or directory"

            full_cmd = [cmd] + resolved_args

        try:
            res = subprocess.run(
                full_cmd,
                cwd=self.worktree_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            out = res.stdout if res.returncode == 0 else res.stderr
            return _cap_read_text(out) if cmd == "cat" else out

        except FileNotFoundError:
            if cmd == "cat" and args_list:
                target = resolved_target if resolved_target is not None else (self.worktree_dir / args_list[0])
                if target.is_dir():
                    return f"cat: {args_list[0]}: Is a directory"
                if target.exists() and target.is_file():
                    try:
                        return _cap_read_text(target.read_text(encoding="utf-8", errors="replace"))
                    except Exception as e:
                        return str(e)
                return f"cat: {args_list[0]}: No such file or directory"
            return f"Command not found: {cmd}"

    def stage_file_mutation(self, path: Union[str, Path], content: str) -> None:
        import posixpath
        raw_str = str(path).replace("\\", "/").strip()
        if Path(path).is_absolute() or raw_str.startswith("/") or re.match(r"^[a-zA-Z]:", raw_str):
            target_path = self.resolve_in_worktree(path)

        norm_str = posixpath.normpath(raw_str)
        if norm_str.startswith("./"):
            norm_str = norm_str[2:]

        if norm_str == "." or norm_str.startswith(".."):
            raise PermissionError(f"Modifying path with traversal is blocked: '{path}'.")

        for prot in PROTECTED_PATHS:
            prot_prefix = prot.rstrip("/")
            if norm_str == prot_prefix or norm_str.startswith(f"{prot_prefix}/"):
                raise PermissionError(f"Modifying files in {prot} is blocked: '{path}'.")

        if norm_str == "node_modules" or norm_str.startswith("node_modules/"):
            raise PermissionError(f"Modifying files in node_modules is blocked: '{path}'.")

        target_path = self.resolve_in_worktree(norm_str)
        try:
            rel = target_path.resolve().relative_to(self.worktree_dir.resolve())
            rel_posix = rel.as_posix()
        except ValueError:
            raise PermissionError(f"Path is outside worktree or traversal rejected: '{path}'.")

        for prot in PROTECTED_PATHS:
            prot_prefix = prot.rstrip("/")
            if rel_posix == prot_prefix or rel_posix.startswith(f"{prot_prefix}/"):
                raise PermissionError(f"Modifying files in {prot} is blocked: '{path}'.")

        if rel_posix == "node_modules" or rel_posix.startswith("node_modules/"):
            raise PermissionError(f"Modifying files in node_modules is blocked: '{path}'.")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content
        target_path.write_bytes(content_bytes)
        sha256_hash = hashlib.sha256(content_bytes).hexdigest()
        self.worker_staged_paths.record(rel_posix, sha256_hash)
        self._run_git(["add", rel_posix])

    def get_staged_diff(self) -> str:
        if self.worker_staged_paths:
            res = self._run_git(["diff", "HEAD", "--"] + list(self.worker_staged_paths))
            if res.returncode != 0:
                res = self._run_git(["diff", "--cached", "--"] + list(self.worker_staged_paths))
            return res.stdout
        res = self._run_git(["diff", "HEAD"])
        if res.returncode != 0:
            res = self._run_git(["diff", "--cached"])
        return res.stdout

    def get_cumulative_diff(self, base_ref: Optional[str] = None) -> str:
        """Returns the full cumulative diff since workspace initialization or base_ref (both committed and uncommitted)."""
        ref = base_ref or self.base_commit
        if self.worker_staged_paths:
            if ref:
                res = self._run_git(["diff", ref, "--"] + list(self.worker_staged_paths))
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout
            return self.get_staged_diff()
        if ref:
            res = self._run_git(["diff", ref])
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        return self.get_staged_diff()

    def get_manifest_changes(
        self, old_ref: str, new_ref: Optional[str] = None
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """Returns a dict of repo-relative posix paths to (old_text, new_text) for dependency manifests changed between old_ref and new_ref."""
        from jev.dependency_delta import is_dependency_manifest_path

        args = ["diff", "--name-status", "--no-renames", old_ref]
        if new_ref is not None:
            args.append(new_ref)

        res = self._run_git(args)
        if res.returncode != 0:
            raise RuntimeError(f"git diff failed with exit code {res.returncode}: {res.stderr}")

        diff_status: Dict[str, str] = {}
        if res.stdout:
            for line in res.stdout.splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    status_code = parts[0].strip().upper()
                    p = parts[1].strip()
                    posix_p = Path(p).as_posix()
                    if is_dependency_manifest_path(posix_p):
                        diff_status[posix_p] = status_code[0] if status_code else ""

        untracked_paths: Set[str] = set()
        if new_ref is None:
            status_res = self._run_git(["status", "--porcelain", "-uall"])
            if status_res.returncode != 0:
                raise RuntimeError(f"git status failed with exit code {status_res.returncode}: {status_res.stderr}")
            if status_res.stdout:
                for line in status_res.stdout.splitlines():
                    if len(line) > 3 and line[:2] == "??":
                        p = line[3:].strip()
                        if p.startswith('"') and p.endswith('"'):
                            p = p[1:-1]
                        posix_p = Path(p).as_posix()
                        if is_dependency_manifest_path(posix_p):
                            untracked_paths.add(posix_p)

        all_paths = sorted(set(diff_status.keys()) | untracked_paths)
        changes: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

        for posix_p in all_paths:
            # Determine old_text
            if posix_p in untracked_paths and posix_p not in diff_status:
                old_text = None
            else:
                code = diff_status.get(posix_p, "")
                if code == "A":
                    old_text = None
                else:
                    show_old = self._run_git(["show", f"{old_ref}:{posix_p}"])
                    if show_old.returncode != 0:
                        raise RuntimeError(f"git show {old_ref}:{posix_p} failed with exit code {show_old.returncode}: {show_old.stderr}")
                    old_text = show_old.stdout

            # Determine new_text
            if new_ref is not None:
                code = diff_status.get(posix_p, "")
                if code == "D":
                    new_text = None
                else:
                    show_new = self._run_git(["show", f"{new_ref}:{posix_p}"])
                    if show_new.returncode != 0:
                        raise RuntimeError(f"git show {new_ref}:{posix_p} failed with exit code {show_new.returncode}: {show_new.stderr}")
                    new_text = show_new.stdout
            else:
                code = diff_status.get(posix_p, "")
                if code == "D":
                    new_text = None
                else:
                    target_file = self.worktree_dir / posix_p
                    if target_file.is_file():
                        try:
                            new_text = target_file.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            new_text = None
                    else:
                        new_text = None

            if old_text is not None or new_text is not None:
                changes[posix_p] = (old_text, new_text)

        return changes

    def _compute_provenance_deltas(self) -> None:
        if self.worktree_dir == self.repo_dir:
            return
        status_res = self._run_git(["status", "--porcelain", "-uall"])
        if status_res.returncode == 0 and status_res.stdout:
            for line in status_res.stdout.splitlines():
                if len(line) < 4:
                    continue
                raw_path = line[3:].strip()
                if raw_path.startswith('"') and raw_path.endswith('"'):
                    raw_path = raw_path[1:-1]
                posix_path = Path(raw_path).as_posix()
                if posix_path not in self.worker_staged_paths:
                    if posix_path not in self.engine_touched_paths:
                        self.engine_touched_paths.append(posix_path)

        for staged_path, expected_hash in self.worker_staged_paths.items():
            fpath = self.worktree_dir / staged_path
            if not fpath.exists() or not fpath.is_file():
                if staged_path not in self.collision_paths:
                    self.collision_paths.append(staged_path)
            else:
                try:
                    curr_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()
                    if curr_hash != expected_hash:
                        if staged_path not in self.collision_paths:
                            self.collision_paths.append(staged_path)
                except Exception:
                    if staged_path not in self.collision_paths:
                        self.collision_paths.append(staged_path)

    def _discard_engine_touched(self) -> None:
        if not self.engine_touched_paths:
            return
        if self.worktree_dir == self.repo_dir:
            return
        status_res = self._run_git(["status", "--porcelain", "-uall"])
        if status_res.returncode != 0 or not status_res.stdout:
            return

        tracked_to_revert: List[str] = []
        untracked_to_clean: List[str] = []

        for line in status_res.stdout.splitlines():
            if len(line) < 4:
                continue
            code = line[:2]
            raw_path = line[3:].strip()
            if raw_path.startswith('"') and raw_path.endswith('"'):
                raw_path = raw_path[1:-1]
            posix_path = Path(raw_path).as_posix()
            if posix_path in self.engine_touched_paths:
                if code == "??":
                    untracked_to_clean.append(posix_path)
                else:
                    tracked_to_revert.append(posix_path)

        if tracked_to_revert:
            self._run_git(["checkout", "--"] + tracked_to_revert)
        if untracked_to_clean:
            self._run_git(["clean", "-f", "--"] + untracked_to_clean)

    def commit_subgoal(self, message: str = "Subgoal committed") -> None:
        self._compute_provenance_deltas()
        if self.collision_paths:
            raise PathCollisionError(
                f"Collision detected: worker-staged file(s) modified or deleted by engine: {', '.join(self.collision_paths)}"
            )
        if self.worker_staged_paths:
            self._run_git(["add", "--"] + list(self.worker_staged_paths))
            self._run_git(["commit", "-m", message])
        else:
            self._run_git(["commit", "-m", message])

    def rollback_subgoal(self) -> None:
        self._run_git(["reset", "--hard", "HEAD"])
        self._run_git(["clean", "-fd"])

    @staticmethod
    def _is_junction_or_symlink(path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink():
            return True
        if sys.platform == "win32":
            try:
                st = os.stat(str(path), follow_symlinks=False)
                if hasattr(st, "st_file_attributes") and (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    return True
            except Exception:
                pass
        return False

    @classmethod
    def _unlink_node_modules(cls, wt_or_path: Path) -> None:
        nm = wt_or_path / "node_modules" if wt_or_path.name != "node_modules" else wt_or_path
        if cls._is_junction_or_symlink(nm):
            try:
                if sys.platform == "win32":
                    os.rmdir(str(nm))
                else:
                    os.unlink(str(nm))
            except Exception:
                try:
                    os.unlink(str(nm))
                except Exception:
                    pass

    @classmethod
    def _link_node_modules(cls, src_repo_dir: Path, target_worktree_dir: Path) -> None:
        src_nm = src_repo_dir / "node_modules"
        target_nm = target_worktree_dir / "node_modules"
        if src_nm.exists() and src_nm.is_dir() and not target_nm.exists() and not cls._is_junction_or_symlink(target_nm):
            if sys.platform == "win32":
                try:
                    import _winapi
                    _winapi.CreateJunction(os.path.abspath(str(src_nm)), os.path.abspath(str(target_nm)))
                except Exception:
                    pass
            else:
                try:
                    os.symlink(os.path.abspath(str(src_nm)), os.path.abspath(str(target_nm)), target_is_directory=True)
                except Exception:
                    pass

    @classmethod
    def _force_rmtree(cls, path: Path) -> None:
        cls._unlink_node_modules(path)
        def on_error(func, p, exc_info):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except Exception:
                pass
        if path.exists():
            shutil.rmtree(path, onerror=on_error)

    def create_integration_branch(self, branch_name: str, base_commit: Optional[str] = None) -> str:
        """Creates an integration branch from base_commit."""
        base_ref = base_commit or self.base_commit or "HEAD"
        self._run_repo_git(["branch", "-D", branch_name])
        res = self._run_repo_git(["branch", branch_name, base_ref])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create integration branch {branch_name} from {base_ref}: {res.stderr or res.stdout}")
        return branch_name

    def delete_integration_branch(self, branch_name: str) -> None:
        """Deletes the integration branch."""
        self._run_repo_git(["branch", "-D", branch_name])

    def get_branch_commit(self, branch_name: str) -> str:
        """Returns the commit SHA for a branch."""
        res = self._run_repo_git(["rev-parse", branch_name])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        raise RuntimeError(f"Could not resolve commit for branch {branch_name}")

    def create_subgoal_worktree(self, subgoal_id: str, base_ref: Optional[str] = None) -> Path:
        """Creates an isolated git worktree for a subgoal branching from base_ref."""
        worktree_base = self.repo_dir / ".jev-worktrees"
        worktree_base.mkdir(parents=True, exist_ok=True)

        exclude_file = self.repo_dir / ".git" / "info" / "exclude"
        if exclude_file.exists():
            try:
                content = exclude_file.read_text(encoding="utf-8")
                entries = []
                if ".jev-worktrees" not in content.splitlines():
                    entries.append(".jev-worktrees")
                if ".jev-worktrees/" not in content.splitlines():
                    entries.append(".jev-worktrees/")
                if "node_modules" not in content.splitlines():
                    entries.append("node_modules")
                if "node_modules/" not in content.splitlines():
                    entries.append("node_modules/")
                if entries:
                    exclude_file.write_text(content.rstrip() + "\n" + "\n".join(entries) + "\n", encoding="utf-8")
            except Exception:
                pass

        worktree_path = (worktree_base / f"subgoal-{subgoal_id}").resolve()
        branch_name = f"jev-subgoal-{subgoal_id}"

        # Clean up if prior branch or worktree directory exists
        if worktree_path.exists():
            self._unlink_node_modules(worktree_path)
            self._run_repo_git(["worktree", "remove", "--force", str(worktree_path)])
            if worktree_path.exists():
                self._force_rmtree(worktree_path)

        self._run_repo_git(["branch", "-D", branch_name])

        start_point = base_ref or "HEAD"
        res = self._run_repo_git(["worktree", "add", "-b", branch_name, str(worktree_path), start_point])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {res.stderr or res.stdout}")

        self._link_node_modules(self.repo_dir, worktree_path)

        self.worktree_dir = worktree_path
        self.current_worktree_path = worktree_path
        self.current_worktree_branch = branch_name
        self.worker_staged_paths.clear()
        self.engine_touched_paths = []
        self.collision_paths = []
        return worktree_path

    def merge_subgoal_worktree(
        self,
        worktree_path: Optional[Union[str, Path]] = None,
        branch_name: Optional[str] = None,
        target_branch: Optional[str] = None,
    ) -> None:
        """Merges changes from the subgoal worktree strictly into target_branch (fast-forward) and cleans up.

        Refuses to merge directly to main without an explicit target_branch.
        """
        if not target_branch:
            raise ValueError("target_branch is required for merge_subgoal_worktree; direct merge to main is disallowed")

        wt_path = Path(worktree_path).resolve() if worktree_path else getattr(self, "current_worktree_path", None)
        br_name = branch_name or getattr(self, "current_worktree_branch", None)

        if wt_path and wt_path.exists():
            self._compute_provenance_deltas()
            if self.collision_paths:
                raise PathCollisionError(
                    f"Collision detected: worker-staged file(s) modified or deleted by engine: {', '.join(self.collision_paths)}"
                )
            if self.worker_staged_paths:
                subprocess.run(["git", "add", "--"] + list(self.worker_staged_paths), cwd=wt_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
                diff_check = subprocess.run(["git", "diff", "--cached", "--"] + list(self.worker_staged_paths), cwd=wt_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
            else:
                subprocess.run(["git", "add", "-A"], cwd=wt_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
                diff_check = subprocess.run(["git", "diff", "--cached"], cwd=wt_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if diff_check.stdout.strip():
                commit_msg = f"Subgoal committed ({br_name})" if br_name else "Subgoal committed"
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=wt_path, capture_output=True, text=True, encoding="utf-8", errors="replace")

        self.worktree_dir = self.repo_dir

        if br_name:
            anc_check = self._run_repo_git(["merge-base", "--is-ancestor", target_branch, br_name])
            if anc_check.returncode != 0:
                raise RuntimeError(
                    f"Cannot fast-forward merge {br_name} into {target_branch}: {target_branch} is not an ancestor of {br_name}"
                )
            subgoal_sha_res = self._run_repo_git(["rev-parse", br_name])
            if subgoal_sha_res.returncode != 0:
                raise RuntimeError(f"Could not resolve commit for subgoal branch {br_name}")
            subgoal_sha = subgoal_sha_res.stdout.strip()

            update_res = self._run_repo_git(["update-ref", f"refs/heads/{target_branch}", subgoal_sha])
            if update_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to fast-forward merge {br_name} into {target_branch}: {update_res.stderr or update_res.stdout}"
                )

        if wt_path:
            self._unlink_node_modules(wt_path)
            self._run_repo_git(["worktree", "remove", "--force", str(wt_path)])
            if wt_path.exists():
                self._force_rmtree(wt_path)
            self._run_repo_git(["worktree", "prune"])

        if br_name:
            self._run_repo_git(["branch", "-D", br_name])

        self.current_worktree_path = None
        self.current_worktree_branch = None

    def create_verify_worktree(self, integration_branch: str) -> Path:
        """Creates a dedicated detached worktree for final verification from the integration branch."""
        worktree_base = self.repo_dir / ".jev-worktrees"
        worktree_base.mkdir(parents=True, exist_ok=True)

        verify_wt_path = (worktree_base / f"verify-{uuid.uuid4().hex[:8]}").resolve()
        if verify_wt_path.exists():
            self._unlink_node_modules(verify_wt_path)
            self._run_repo_git(["worktree", "remove", "--force", str(verify_wt_path)])
            if verify_wt_path.exists():
                self._force_rmtree(verify_wt_path)

        res = self._run_repo_git(["worktree", "add", "--detach", str(verify_wt_path), integration_branch])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create verify worktree from {integration_branch}: {res.stderr or res.stdout}")

        self._link_node_modules(self.repo_dir, verify_wt_path)
        self.worktree_dir = verify_wt_path
        return verify_wt_path

    def discard_verify_worktree(self, verify_path: Union[str, Path]) -> None:
        """Discards the verify worktree safely, ensuring node_modules is unlinked first."""
        v_path = Path(verify_path).resolve()
        self.worktree_dir = self.repo_dir
        if v_path.exists() or v_path.is_symlink():
            self._unlink_node_modules(v_path)
            self._run_repo_git(["worktree", "remove", "--force", str(v_path)])
            if v_path.exists():
                self._force_rmtree(v_path)
            self._run_repo_git(["worktree", "prune"])

    def fast_forward_main(self, integration_branch: str, base_commit: Optional[str] = None) -> None:
        """Fast-forwards main to the integration branch.

        Refuses if repo_dir has tracked modifications or if main diverged from base_commit.
        """
        self.worktree_dir = self.repo_dir

        # 1. Check for tracked modifications (ignore untracked files with -uno)
        status_res = self._run_repo_git(["status", "--porcelain", "-uno"])
        if status_res.stdout.strip():
            raise TrackedModificationsError(
                f"Cannot fast-forward main: working tree has tracked modifications:\n{status_res.stdout}"
            )

        # 2. Check if main moved since base_commit
        current_head = self._get_head_commit()
        if base_commit and current_head != base_commit:
            raise MainDivergedError(
                f"Cannot fast-forward main: HEAD ({current_head}) does not match base_commit ({base_commit})"
            )

        # 3. Check if integration_branch is a fast-forward from HEAD
        anc_res = self._run_repo_git(["merge-base", "--is-ancestor", "HEAD", integration_branch])
        if anc_res.returncode != 0:
            raise MainDivergedError(
                f"Cannot fast-forward main: {integration_branch} is not a descendant of main"
            )

        # 4. Perform fast-forward merge
        merge_res = self._run_repo_git(["merge", "--ff-only", integration_branch])
        if merge_res.returncode != 0:
            self._run_repo_git(["merge", "--abort"])
            raise RuntimeError(
                f"Failed to fast-forward main to {integration_branch}: {merge_res.stderr or merge_res.stdout}"
            )

    def discard_subgoal_worktree(
        self,
        worktree_path: Optional[Union[str, Path]] = None,
        branch_name: Optional[str] = None,
    ) -> None:
        """Discards the subgoal worktree and deletes its branch without modifying main repo."""
        wt_path = Path(worktree_path).resolve() if worktree_path else getattr(self, "current_worktree_path", None)
        br_name = branch_name or getattr(self, "current_worktree_branch", None)

        self.worktree_dir = self.repo_dir

        if wt_path:
            self._unlink_node_modules(wt_path)
            self._run_repo_git(["worktree", "remove", "--force", str(wt_path)])
            if wt_path.exists():
                self._force_rmtree(wt_path)
            self._run_repo_git(["worktree", "prune"])

        if br_name:
            self._run_repo_git(["branch", "-D", br_name])

        self.current_worktree_path = None
        self.current_worktree_branch = None

    def check_build(
        self,
        files: Optional[Union[List[Union[str, Path]], str, Path]] = None,
        *,
        scope: Optional[Union[List[Union[str, Path]], str, Path]] = None,
    ) -> BuildCheckResult:
        ignore_dirs = {
            ".git",
            ".pytest_cache",
            ".venv",
            "venv",
            "env",
            ".env",
            "__pycache__",
            ".mypy_cache",
            ".tox",
            "build",
            "dist",
            "site-packages",
        }

        target_scope = files if files is not None else scope
        if target_scope is not None:
            if isinstance(target_scope, (str, Path)):
                candidate_files = [target_scope]
            else:
                candidate_files = list(target_scope)
        else:
            candidate_files = []

        if not candidate_files:
            diff_str = self.get_staged_diff()
            candidate_files = list(self._extract_files_from_diff(diff_str))

        target_files: List[Path] = []
        for f in candidate_files:
            p = Path(f)
            if not p.is_absolute():
                p = self.worktree_dir / p
            p = p.resolve()

            try:
                rel_parts = p.relative_to(self.worktree_dir.resolve()).parts
            except ValueError:
                rel_parts = p.parts

            if any(
                part in ignore_dirs or (part.startswith(".") and part != ".")
                for part in rel_parts[:-1]
            ):
                continue

            if p.is_dir():
                for sub_p in p.rglob("*.py"):
                    try:
                        sub_rel_parts = sub_p.relative_to(
                            self.worktree_dir.resolve()
                        ).parts
                    except ValueError:
                        sub_rel_parts = sub_p.parts
                    if not any(
                        part in ignore_dirs or (part.startswith(".") and part != ".")
                        for part in sub_rel_parts[:-1]
                    ):
                        target_files.append(sub_p)
            elif p.suffix == ".py" and p.exists():
                target_files.append(p)

        seen = set()
        unique_target_files: List[Path] = []
        for file_path in target_files:
            if file_path not in seen:
                seen.add(file_path)
                unique_target_files.append(file_path)

        for file_path in unique_target_files:
            if not file_path.exists():
                continue
            try:
                # Read raw bytes to allow ast.parse to handle encoding cookies and BOM
                source_bytes = file_path.read_bytes()
                ast.parse(source_bytes, filename=str(file_path))
            except SyntaxError as e:
                return BuildCheckResult(
                    passed=False,
                    detail=f"SyntaxError in {file_path.name}: {e.msg} (line {e.lineno})",
                )
            except Exception as e:
                return BuildCheckResult(
                    passed=False,
                    detail=f"Build error in {file_path.name}: {str(e)}",
                )
        return BuildCheckResult(passed=True, detail="")

    def resolve_test_command(self) -> ResolvedTestCommand:
        return resolve_test_command(self.worktree_dir)

    def run_tests(self, targets: Optional[Sequence[Union[str, Path]]] = None) -> TestOutcome:
        """Run the detected suite, optionally narrowing supported runners to relative test targets.

        Targeted runs support pytest file/node IDs and package.json test scripts.
        Unsupported ecosystems do not silently run their full suites. An omitted
        or empty target sequence preserves the existing full-suite behavior.
        """
        wt = self.worktree_dir
        res = resolve_test_command(wt)

        if isinstance(targets, (str, bytes, Path)):
            raise ValueError("Test targets must be a sequence of relative paths, not a command string")
        if targets:
            if len(targets) > 32:
                raise ValueError("At most 32 test targets are allowed")
            validated: List[str] = []
            for target in targets:
                if not isinstance(target, (str, Path)):
                    raise ValueError("Each test target must be a relative path")
                raw = str(target)
                if len(raw) > 512:
                    raise ValueError("Test target exceeds 512 characters")
                path, sep, node = raw.partition("::") if res.ecosystem == "pytest" else (raw, "", "")
                if not path or (sep and not node) or (res.ecosystem != "pytest" and "::" in raw):
                    raise ValueError(f"Invalid test target: {raw!r}")
                candidate = self.resolve_in_worktree(path)
                if not candidate.exists():
                    raise ValueError(f"Test target does not exist: {raw!r}")
                # Keep each target a worktree-relative argv item, never a runner option.
                relative = candidate.relative_to(wt).as_posix()
                if relative == ".":
                    raise ValueError("A test target must narrow the suite")
                if relative.startswith("-"):
                    raise ValueError(f"Test target cannot start with '-': {raw!r}")
                if res.ecosystem in ("npm", "yarn", "pnpm") and not re.fullmatch(r"[A-Za-z0-9_./@-]+", relative):
                    raise ValueError(f"Package test target contains unsupported characters: {raw!r}")
                validated.append(relative + (sep + node if sep else ""))

            if res.ecosystem == "pytest":
                res = ResolvedTestCommand(res.ecosystem, [*res.command, *validated], res.source) if res.command else res
            elif res.ecosystem in ("npm", "yarn", "pnpm") and res.command:
                if res.ecosystem == "npm" and ((wt / "bun.lock").exists() or (wt / "bun.lockb").exists()):
                    res = ResolvedTestCommand("bun", ["bun", "run", "test", *validated], res.source)
                else:
                    separator = ["--"] if res.ecosystem == "npm" else []
                    res = ResolvedTestCommand(res.ecosystem, [*res.command, *separator, *validated], res.source)
            else:
                return self._record_test_run(
                    res.ecosystem, None, None, "", "Targeted tests unsupported for this ecosystem", TestOutcome.NO_TESTS_COLLECTED
                )

        if res.command is None:
            if res.ecosystem == "package_json" and res.reason:
                return self._record_test_run("package_json", None, -1, "", res.reason, TestOutcome.FAILED)
            return self._record_test_run(None, None, None, "", "", TestOutcome.NO_TESTS_COLLECTED)

        cmd = res.command
        ecosystem = res.ecosystem

        if ecosystem in ("yarn", "pnpm", "npm", "bun", "go", "cargo", "maven", "gradle"):
            resolved = shutil.which(cmd[0])
            if not resolved:
                return self._record_test_run(
                    ecosystem, cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                )
            exec_cmd = [resolved] + cmd[1:]
            try:
                sub_res = self._run_test_subprocess(
                    exec_cmd,
                    cwd=wt,
                    timeout=self.test_timeout,
                )
                outcome = TestOutcome.PASSED if sub_res.returncode == 0 else TestOutcome.FAILED
                return self._record_test_run(
                    ecosystem, cmd, sub_res.returncode, sub_res.stdout, sub_res.stderr, outcome,
                    executed_command=exec_cmd,
                )
            except Exception as e:
                return self._record_test_run(ecosystem, cmd, -1, "", str(e), TestOutcome.FAILED, executed_command=exec_cmd)

        elif ecosystem == "pytest":
            try:
                sub_res = self._run_test_subprocess(
                    cmd,
                    cwd=wt,
                    timeout=self.test_timeout,
                )
                if sub_res.returncode == 0:
                    outcome = TestOutcome.PASSED
                elif sub_res.returncode == 5:
                    outcome = TestOutcome.NO_TESTS_COLLECTED
                else:
                    outcome = TestOutcome.FAILED
                return self._record_test_run("pytest", cmd, sub_res.returncode, sub_res.stdout, sub_res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("pytest", cmd, -1, "", str(e), TestOutcome.FAILED)

        elif ecosystem == "terraform":
            if shutil.which("terraform"):
                try:
                    sub_res = self._run_test_subprocess(
                        cmd,
                        cwd=wt,
                        timeout=self.test_timeout,
                    )
                    outcome = TestOutcome.PASSED if sub_res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("terraform", cmd, sub_res.returncode, sub_res.stdout, sub_res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("terraform", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "terraform", cmd, None, "", "Executable 'terraform' not found.", TestOutcome.ENV_NOT_READY
            )

        elif ecosystem == "ansible":
            if shutil.which("ansible-lint") or shutil.which("ansible-playbook"):
                try:
                    sub_res = self._run_test_subprocess(
                        cmd,
                        cwd=wt,
                        timeout=self.test_timeout,
                    )
                    outcome = TestOutcome.PASSED if sub_res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("ansible", cmd, sub_res.returncode, sub_res.stdout, sub_res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("ansible", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "ansible", cmd, None, "", "Executable 'ansible-lint' not found.", TestOutcome.ENV_NOT_READY
            )

        elif ecosystem == "helm":
            if shutil.which("helm"):
                try:
                    sub_res = self._run_test_subprocess(
                        cmd,
                        cwd=wt,
                        timeout=self.test_timeout,
                    )
                    outcome = TestOutcome.PASSED if sub_res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("helm", cmd, sub_res.returncode, sub_res.stdout, sub_res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("helm", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "helm", cmd, None, "", "Executable 'helm' not found.", TestOutcome.ENV_NOT_READY
            )

        return self._record_test_run(None, None, None, "", "", TestOutcome.NO_TESTS_COLLECTED)

    def run_baseline_tests(
        self,
        base_commit: Optional[str] = None,
        test_policy: str = "auto",
    ) -> Dict[str, Any]:
        """Run tests on the base ref and record failing test IDs.

        Cached per base commit so it runs once per base commit across attempts.
        Respects test_policy (if 'never', returns exempt without running).
        """
        if test_policy == "never":
            return {
                "outcome": TestOutcome.EXEMPT_BY_POLICY.value,
                "failing_ids": set(),
                "ran": False,
            }

        commit_key = str(base_commit).strip() if base_commit else (self._get_head_commit() or "HEAD")
        if commit_key in self._baseline_test_cache:
            return self._baseline_test_cache[commit_key]

        try:
            test_outcome = self.run_tests(targets=None)
            test_run_details = getattr(self, "last_test_run", None) or {}
            exit_code = test_run_details.get("exit_code")
            failing_ids = test_run_details.get("failing_test_ids")

            if test_outcome == TestOutcome.PASSED or exit_code == 0:
                res = {
                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
                    "failing_ids": set(),
                    "ran": True,
                    "details": test_run_details,
                }
            elif test_outcome == TestOutcome.FAILED or (exit_code is not None and exit_code != 0):
                if failing_ids is not None:
                    res = {
                        "outcome": TestOutcome.FAILED.value,
                        "failing_ids": set(failing_ids),
                        "ran": True,
                        "details": test_run_details,
                    }
                else:
                    res = {
                        "outcome": TestOutcome.FAILED.value,
                        "failing_ids": None,
                        "unparseable": True,
                        "ran": True,
                        "details": test_run_details,
                    }
            else:
                res = {
                    "outcome": test_outcome.value if hasattr(test_outcome, "value") else str(test_outcome),
                    "failing_ids": set(),
                    "ran": True,
                    "details": test_run_details,
                }
        except Exception as e:
            res = {
                "outcome": TestOutcome.FAILED.value,
                "failing_ids": None,
                "error": str(e),
                "timed_out": "timed out" in str(e).lower(),
                "ran": True,
            }

        self._baseline_test_cache[commit_key] = res
        return res

    def run_targeted_tests(self, targets: Sequence[Union[str, Path]]) -> TestOutcome:
        """Run only the explicitly selected, worktree-contained test targets."""
        return self.run_tests(targets=targets)


    @staticmethod
    def _clean_diff_path(raw_path: str) -> str:
        p = raw_path.strip()
        if p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        if p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        return Path(p).as_posix()

    @classmethod
    def _extract_files_from_diff(cls, diff_str: str) -> set[str]:
        import re
        touched: set[str] = set()
        for line in diff_str.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("--- "):
                target = line_str[4:].strip()
                if target != "/dev/null":
                    touched.add(cls._clean_diff_path(target))
            elif line_str.startswith("+++ "):
                target = line_str[4:].strip()
                if target != "/dev/null":
                    touched.add(cls._clean_diff_path(target))
            elif line_str.startswith("rename from "):
                touched.add(cls._clean_diff_path(line_str[12:].strip()))
            elif line_str.startswith("rename to "):
                touched.add(cls._clean_diff_path(line_str[10:].strip()))
            elif line_str.startswith("copy from "):
                touched.add(cls._clean_diff_path(line_str[10:].strip()))
            elif line_str.startswith("copy to "):
                touched.add(cls._clean_diff_path(line_str[8:].strip()))
            elif line_str.startswith("diff --git "):
                rest = line_str[11:].strip()
                if rest.startswith('"a/'):
                    m = re.match(r'^"a/(.+?)"\s+"b/(.+?)"$', rest)
                    if m:
                        touched.add(cls._clean_diff_path(m.group(1)))
                        touched.add(cls._clean_diff_path(m.group(2)))
                elif rest.startswith("a/"):
                    idx = rest.find(" b/")
                    if idx != -1:
                        p_a = rest[2:idx]
                        p_b = rest[idx + 3:]
                        touched.add(cls._clean_diff_path(p_a))
                        touched.add(cls._clean_diff_path(p_b))
        return touched

    @classmethod
    def _iter_added_lines(cls, diff: str) -> Iterator[AddedDiffLine]:
        """Walks added lines of diff yielding AddedDiffLine(file, line_number, text).

        Decides line type based on first character after the first @@ hunk header of a file.
        Resets state on 'diff --git '.
        """
        current_file: Optional[str] = None
        in_hunk: bool = False
        current_line_num: int = 0
        hunk_header_re = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")

        for raw_line in diff.splitlines():
            if raw_line.startswith("diff --git "):
                current_file = None
                in_hunk = False
                current_line_num = 0
                continue

            if not in_hunk:
                if raw_line.startswith("+++ "):
                    target = raw_line[4:].strip()
                    if target.startswith("b/"):
                        target = target[2:]
                    if target == "/dev/null":
                        current_file = None
                    else:
                        current_file = cls._clean_diff_path(target)
                    continue

            m = hunk_header_re.match(raw_line)
            if m:
                in_hunk = True
                current_line_num = int(m.group(1))
                continue

            if in_hunk and current_file is not None:
                first_char = raw_line[:1]
                if first_char == "+":
                    yield AddedDiffLine(file=current_file, line_number=current_line_num, text=raw_line[1:])
                    current_line_num += 1
                elif first_char == " ":
                    current_line_num += 1
                elif first_char == "-":
                    pass
                elif first_char == "\\":
                    pass

    def check_suppression(self, diff: Optional[str] = None) -> SuppressionCheckResult:
        """Scans only the added lines of the staged diff for compiler or type-check suppression directives."""
        if diff is None:
            diff = self.get_staged_diff()

        if not diff or not diff.strip():
            return SuppressionCheckResult(passed=True, detail="")

        for item in self._iter_added_lines(diff):
            ext = Path(item.file).suffix.lower()
            directives = self.SUPPRESSION_DIRECTIVES.get(ext, [])
            if not directives:
                continue

            for directive in directives:
                matched = False
                if directive in item.text:
                    matched = True
                elif directive.startswith("#"):
                    norm_dir = directive.replace(" ", "")
                    norm_line = item.text.replace(" ", "")
                    if norm_dir in norm_line:
                        matched = True

                if matched:
                    detail = (
                        f"Compiler/type-check suppression directive '{directive}' found in {item.file} "
                        f"at line {item.line_number}: '{item.text.strip()}'. "
                        f"Do not suppress compiler errors. Fix the cause, or escalate if it cannot be fixed."
                    )
                    return SuppressionCheckResult(passed=False, detail=detail)

        return SuppressionCheckResult(passed=True, detail="")

    @staticmethod
    def _reduce_to_package_name(specifier: str) -> str:
        parts = specifier.split("/")
        if specifier.startswith("@"):
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            return specifier
        return parts[0]

    def _resolve_package(self, file_path: str, pkg_name: str) -> bool:
        p = Path(file_path)
        current_dir = p.parent

        dirs_to_check = []
        curr = current_dir
        while True:
            dirs_to_check.append(curr)
            if curr == Path(".") or curr == Path("") or str(curr) in (".", ""):
                break
            parent = curr.parent
            if parent == curr:
                break
            curr = parent

        if Path(".") not in dirs_to_check:
            dirs_to_check.append(Path("."))

        for d in dirs_to_check:
            for base in (self.worktree_dir, self.repo_dir):
                candidate = base / d / "node_modules" / pkg_name
                if candidate.exists() or candidate.is_dir() or candidate.is_file() or candidate.is_symlink():
                    return True

        return False

    def _is_declared_in_package_json(self, file_path: str, pkg_name: str) -> bool:
        p = Path(file_path)
        current_dir = p.parent

        dirs_to_check = []
        curr = current_dir
        while True:
            dirs_to_check.append(curr)
            if curr == Path(".") or curr == Path("") or str(curr) in (".", ""):
                break
            parent = curr.parent
            if parent == curr:
                break
            curr = parent

        if Path(".") not in dirs_to_check:
            dirs_to_check.append(Path("."))

        dep_keys = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")

        for d in dirs_to_check:
            for base in (self.worktree_dir, self.repo_dir):
                pkg_json_path = base / d / "package.json"
                if pkg_json_path.exists() and pkg_json_path.is_file():
                    try:
                        data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            for k in dep_keys:
                                deps = data.get(k)
                                if isinstance(deps, dict) and pkg_name in deps:
                                    return True
                    except Exception:
                        pass
        return False

    @classmethod
    def _is_npm_package_name(cls, pkg_name: str) -> bool:
        return bool(NPM_PACKAGE_PATTERN.match(pkg_name))

    def _has_node_modules_in_chain(self, file_path: str) -> bool:
        p = Path(file_path)
        current_dir = p.parent

        dirs_to_check = []
        curr = current_dir
        while True:
            dirs_to_check.append(curr)
            if curr == Path(".") or curr == Path("") or str(curr) in (".", ""):
                break
            parent = curr.parent
            if parent == curr:
                break
            curr = parent

        if Path(".") not in dirs_to_check:
            dirs_to_check.append(Path("."))

        for d in dirs_to_check:
            for base in (self.worktree_dir, self.repo_dir):
                nm = base / d / "node_modules"
                if nm.exists() or nm.is_dir() or nm.is_symlink():
                    return True
        return False

    def _get_tsconfig_info(self, file_path: str) -> tuple[List[str], List[Path]]:
        p = Path(file_path)
        current_dir = p.parent

        dirs_to_check = []
        curr = current_dir
        while True:
            dirs_to_check.append(curr)
            if curr == Path(".") or curr == Path("") or str(curr) in (".", ""):
                break
            parent = curr.parent
            if parent == curr:
                break
            curr = parent

        if Path(".") not in dirs_to_check:
            dirs_to_check.append(Path("."))

        aliases: set[str] = {"@/"}
        base_urls: list[Path] = []
        visited: set[str] = set()

        def _load_and_follow(cfg_path: Path, depth: int) -> None:
            if depth > 5:
                return
            resolved_key = str(cfg_path.resolve())
            if resolved_key in visited:
                return
            visited.add(resolved_key)

            try:
                text = cfg_path.read_text(encoding="utf-8")
            except Exception:
                return

            text_no_comments = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
            text_no_comments = re.sub(r"/\*.*?\*/", "", text_no_comments, flags=re.DOTALL)
            try:
                data = json.loads(text_no_comments)
            except Exception:
                return

            if not isinstance(data, dict):
                return

            compiler_opts = data.get("compilerOptions")
            if isinstance(compiler_opts, dict):
                paths = compiler_opts.get("paths")
                if isinstance(paths, dict):
                    for alias_key in paths.keys():
                        if alias_key.endswith("*"):
                            aliases.add(alias_key[:-1])
                        else:
                            aliases.add(alias_key)

                base_url_val = compiler_opts.get("baseUrl")
                if base_url_val and isinstance(base_url_val, str):
                    cfg_dir = cfg_path.parent
                    rel_to_root = None
                    for root in (self.worktree_dir, self.repo_dir):
                        try:
                            rel_to_root = cfg_dir.relative_to(root)
                            break
                        except ValueError:
                            pass
                    if rel_to_root is not None:
                        base_urls.append(rel_to_root / base_url_val)
                    else:
                        base_urls.append(cfg_dir / base_url_val)

            extends_val = data.get("extends")
            if extends_val:
                if isinstance(extends_val, str):
                    extends_list = [extends_val]
                elif isinstance(extends_val, list):
                    extends_list = extends_val
                else:
                    extends_list = []

                cfg_dir = cfg_path.parent
                rel_cfg_dir = None
                for root in (self.worktree_dir, self.repo_dir):
                    try:
                        rel_cfg_dir = cfg_dir.relative_to(root)
                        break
                    except ValueError:
                        pass

                for ext_target in extends_list:
                    if not isinstance(ext_target, str) or ext_target.startswith("@"):
                        continue

                    target_p = Path(ext_target)
                    candidate_rels = []
                    if rel_cfg_dir is not None:
                        candidate_rels.append(rel_cfg_dir / target_p)
                        if not ext_target.endswith(".json"):
                            candidate_rels.append(rel_cfg_dir / Path(f"{ext_target}.json"))
                    else:
                        candidate_rels.append(cfg_dir / target_p)
                        if not ext_target.endswith(".json"):
                            candidate_rels.append(cfg_dir / Path(f"{ext_target}.json"))

                    found = False
                    for cand in candidate_rels:
                        for root in (self.worktree_dir, self.repo_dir):
                            full_path = cand if cand.is_absolute() else root / cand
                            if full_path.exists() and full_path.is_file():
                                _load_and_follow(full_path, depth + 1)
                                found = True
                                break
                        if found:
                            break

        for d in dirs_to_check:
            for base in (self.worktree_dir, self.repo_dir):
                for cfg_name in ("tsconfig.json", "jsconfig.json"):
                    cfg_path = base / d / cfg_name
                    if cfg_path.exists() and cfg_path.is_file():
                        _load_and_follow(cfg_path, 0)

        return list(aliases), base_urls

    def _get_tsconfig_path_aliases(self, file_path: str) -> List[str]:
        aliases, _ = self._get_tsconfig_info(file_path)
        return aliases

    def _matches_base_url(self, first_segment: str, base_urls: List[Path]) -> bool:
        if not base_urls:
            return False
        for bu in base_urls:
            for root in (self.worktree_dir, self.repo_dir):
                bu_path = bu if bu.is_absolute() else root / bu
                target = bu_path / first_segment
                if target.exists():
                    return True
                for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json"):
                    if (bu_path / f"{first_segment}{ext}").is_file():
                        return True
        return False

    def check_unresolved_imports(self, diff: Optional[str] = None) -> UnresolvedImportCheckResult:
        """Scans added lines of JS/TS-family files for uninstalled package imports."""
        if diff is None:
            diff = self.get_staged_diff()

        if not diff or not diff.strip():
            return UnresolvedImportCheckResult(passed=True, detail="")

        from_re = re.compile(r"""(?:\b(?:import|export)\b[\s\S]*?\bfrom|\}\s*from)\s+['"]([^'"]+)['"]""")
        side_effect_re = re.compile(r"""\bimport\s+['"]([^'"]+)['"]""")
        dynamic_re = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")
        require_re = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
        declare_mod_re = re.compile(r"""\bdeclare\s+module\s+['"]([^'"]+)['"]""")

        for item in self._iter_added_lines(diff):
            ext = Path(item.file).suffix.lower()
            resolver = self.UNRESOLVED_IMPORT_RESOLVERS.get(ext)
            if resolver != "js_ts":
                continue

            # If no node_modules directory exists anywhere in the resolution chain,
            # check_unresolved_imports returns passed for this file.
            if not self._has_node_modules_in_chain(item.file):
                continue

            raw_text = item.text
            line_no_block = re.sub(r"/\*.*?\*/", "", raw_text)
            idx = line_no_block.find("//")
            code = line_no_block[:idx] if idx != -1 else line_no_block
            code = code.strip()
            if not code or code.startswith("*"):
                continue

            specifiers: List[str] = []
            for m in from_re.finditer(code):
                specifiers.append(m.group(1))
            for m in side_effect_re.finditer(code):
                specifiers.append(m.group(1))
            for m in dynamic_re.finditer(code):
                specifiers.append(m.group(1))
            for m in require_re.finditer(code):
                specifiers.append(m.group(1))
            for m in declare_mod_re.finditer(code):
                specifiers.append(m.group(1))

            if not specifiers:
                continue

            path_aliases, base_urls = self._get_tsconfig_info(item.file)

            for spec in specifiers:
                if spec.startswith("./") or spec.startswith("../") or spec in (".", ".."):
                    continue
                if spec.startswith("/") or re.match(r"^[a-zA-Z]:", spec):
                    continue
                if spec.startswith("node:") or spec in NODE_BUILTIN_MODULES:
                    continue
                if "*" in spec:
                    continue
                if any(spec == alias or (alias.endswith("/") and spec.startswith(alias)) or (not alias.endswith("/") and spec.startswith(alias + "/")) for alias in path_aliases):
                    continue

                first_segment = spec.split("/")[0]
                if self._matches_base_url(first_segment, base_urls):
                    continue

                pkg_name = self._reduce_to_package_name(spec)
                if pkg_name in NODE_BUILTIN_MODULES:
                    continue

                if not self._is_npm_package_name(pkg_name):
                    continue

                if not self._resolve_package(item.file, pkg_name):
                    declared_str = "yes" if self._is_declared_in_package_json(item.file, pkg_name) else "no"
                    detail = (
                        f"Package '{pkg_name}' is imported in {item.file} at line {item.line_number} but is not installed "
                        f"(declared in package.json: {declared_str}). Do not import packages that are not installed. "
                        f"Use only installed packages, or escalate if the ticket requires a package that does not exist."
                    )
                    return UnresolvedImportCheckResult(passed=False, detail=detail)

        return UnresolvedImportCheckResult(passed=True, detail="")

    def check_scope(
        self,
        diff: Optional[Union[str, List[str]]] = None,
        scope: Optional[Union[List[str], str, Path]] = None,
    ) -> ScopeCheckResult:
        if scope is None and isinstance(diff, list):
            scope = diff
            diff = self.get_staged_diff()
        elif diff is None:
            diff = self.get_staged_diff()

        diff_str = str(diff)
        if isinstance(scope, (str, Path)):
            scope_list = [scope]
        else:
            scope_list = list(scope) if scope is not None else []

        touched_files = self._extract_files_from_diff(diff_str)

        norm_scope = {Path(f).as_posix() for f in scope_list}
        out_of_scope = [
            f for f in sorted(touched_files) if Path(f).as_posix() not in norm_scope
        ]

        if out_of_scope:
            return ScopeCheckResult(
                passed=False,
                out_of_scope=out_of_scope,
                detail=f"Out of scope files touched: {', '.join(out_of_scope)}",
            )
        return ScopeCheckResult(passed=True, out_of_scope=[], detail="")

    @classmethod
    def _parse_ts_js_comment_line(cls, line: str, in_block: bool) -> tuple[bool, bool]:
        """Validates if a TypeScript/JavaScript line is exclusively a comment/doc and updates block state.

        Returns (is_valid_comment, new_in_block).
        """
        s = line.strip()
        if in_block:
            if "*/" not in s:
                return (True, True)
            idx = s.find("*/")
            remainder = s[idx + 2:].strip()
            if not remainder:
                return (True, False)
            return cls._parse_ts_js_comment_line(remainder, in_block=False)
        else:
            if not s:
                return (True, False)
            if s.startswith("//"):
                return (True, False)
            if s.startswith("/*"):
                idx = s.find("*/", 2)
                if idx == -1:
                    return (True, True)
                remainder = s[idx + 2:].strip()
                if not remainder:
                    return (True, False)
                return cls._parse_ts_js_comment_line(remainder, in_block=False)
            if s.startswith("*/"):
                remainder = s[2:].strip()
                if not remainder:
                    return (True, False)
                return cls._parse_ts_js_comment_line(remainder, in_block=False)
            if s == "*" or s.startswith("* ") or s.startswith("*\t") or s.startswith("*@"):
                if "*/" in s:
                    idx = s.find("*/")
                    remainder = s[idx + 2:].strip()
                    if not remainder:
                        return (True, False)
                    return cls._parse_ts_js_comment_line(remainder, in_block=False)
                return (True, True)
            return (False, False)

    @classmethod
    def _parse_python_comment_line(cls, line: str, in_docstring: Optional[str]) -> tuple[bool, Optional[str]]:
        """Validates if a Python line is exclusively a comment/docstring and updates docstring state.

        Returns (is_valid_comment, new_in_docstring).
        """
        s = line.strip()
        if in_docstring is not None:
            if in_docstring not in s:
                return (True, in_docstring)
            idx = s.find(in_docstring)
            remainder = s[idx + 3:].strip()
            if not remainder or remainder.startswith("#"):
                return (True, None)
            return (False, None)
        else:
            if not s:
                return (True, None)
            if s.startswith("#"):
                return (True, None)
            if s.startswith('"""'):
                idx = s.find('"""', 3)
                if idx == -1:
                    return (True, '"""')
                remainder = s[idx + 3:].strip()
                if not remainder or remainder.startswith("#"):
                    return (True, None)
                return (False, None)
            if s.startswith("'''"):
                idx = s.find("'''", 3)
                if idx == -1:
                    return (True, "'''")
                remainder = s[idx + 3:].strip()
                if not remainder or remainder.startswith("#"):
                    return (True, None)
                return (False, None)
            return (False, None)

    @classmethod
    def _is_docs_only_diff(cls, diff_str: str) -> bool:
        """Fail-closed classifier that verifies if a staged git diff contains ONLY documentation or comment changes.

        Rules:
        1. Empty diff or whitespace only -> False.
        2. Any file rename, deletion, copy, or mode change -> False (fail closed).
        3. For each file touched:
           - Markdown/text documentation files (.md, .txt, .rst, .adoc, etc.) -> all line changes permitted.
           - TypeScript / JavaScript (.ts, .tsx, .js, .jsx, .mjs, .cjs) -> every added/removed line must be
             pure whitespace, single-line comment (//), or JSDoc/block comment (/* ... */, /** ... */).
             Any executable logic, statements, or signature changes outside comments -> False.
           - Python (.py, .pyi) -> every added/removed line must be pure whitespace, single-line comment (#),
             or triple-quoted docstring (''' or \"\"\"). Any statement outside comments/docstrings -> False.
           - Any other file extension -> False (fail closed).
        4. If even a single line of real executable logic is touched across any file -> False.
        """
        if not diff_str or not diff_str.strip():
            return False

        # Fail closed on file deletion, renaming, or mode changes
        for line in diff_str.splitlines():
            line_strip = line.strip()
            if (
                line_strip.startswith("deleted file mode ")
                or line_strip.startswith("rename from ")
                or line_strip.startswith("rename to ")
                or line_strip.startswith("copy from ")
                or line_strip.startswith("copy to ")
            ):
                return False

        raw_blocks = diff_str.split("diff --git ")
        file_blocks = [b for b in raw_blocks if b.strip()]
        if not file_blocks:
            return False

        DOC_EXTS = {".md", ".markdown", ".mdown", ".mkdn", ".txt", ".rst", ".adoc", ".asciidoc"}
        TS_JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
        PY_EXTS = {".py", ".pyi"}

        has_modifications = False

        for block in file_blocks:
            # Check for deleted file (+++ /dev/null)
            if re.search(r"^\+\+\+\s+/dev/null", block, re.MULTILINE):
                return False

            match_plus = re.search(r"^\+\+\+\s+b/(.+)$", block, re.MULTILINE)
            if match_plus:
                target_path = match_plus.group(1).strip()
            else:
                first_line = block.splitlines()[0]
                match_git = re.search(r"b/(.+)$", first_line)
                if match_git:
                    target_path = match_git.group(1).strip()
                else:
                    return False

            target_path = cls._clean_diff_path(target_path)
            ext = Path(target_path).suffix.lower()

            if ext in DOC_EXTS:
                for line in block.splitlines():
                    if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
                        has_modifications = True
                continue

            if ext not in TS_JS_EXTS and ext not in PY_EXTS:
                return False

            in_block_add = False
            in_block_rem = False
            in_py_doc_add: Optional[str] = None
            in_py_doc_rem: Optional[str] = None

            for line in block.splitlines():
                if line.startswith("@@"):
                    in_block_add = False
                    in_block_rem = False
                    in_py_doc_add = None
                    in_py_doc_rem = None
                    continue

                if line.startswith("+++") or line.startswith("---"):
                    continue

                if line.startswith(" "):
                    content = line[1:]
                    if ext in TS_JS_EXTS:
                        _, in_block_add = cls._parse_ts_js_comment_line(content, in_block_add)
                        _, in_block_rem = cls._parse_ts_js_comment_line(content, in_block_rem)
                    elif ext in PY_EXTS:
                        _, in_py_doc_add = cls._parse_python_comment_line(content, in_py_doc_add)
                        _, in_py_doc_rem = cls._parse_python_comment_line(content, in_py_doc_rem)
                elif line.startswith("+"):
                    has_modifications = True
                    content = line[1:]
                    if ext in TS_JS_EXTS:
                        is_valid, in_block_add = cls._parse_ts_js_comment_line(content, in_block_add)
                        if not is_valid:
                            return False
                    elif ext in PY_EXTS:
                        is_valid, in_py_doc_add = cls._parse_python_comment_line(content, in_py_doc_add)
                        if not is_valid:
                            return False
                elif line.startswith("-"):
                    has_modifications = True
                    content = line[1:]
                    if ext in TS_JS_EXTS:
                        is_valid, in_block_rem = cls._parse_ts_js_comment_line(content, in_block_rem)
                        if not is_valid:
                            return False
                    elif ext in PY_EXTS:
                        is_valid, in_py_doc_rem = cls._parse_python_comment_line(content, in_py_doc_rem)
                        if not is_valid:
                            return False

        return has_modifications

    @classmethod
    def _normalize_diagnostic_path(cls, path_str: str, root_dir: Optional[Path] = None) -> str:
        s = path_str.strip().replace("\\", "/")
        if root_dir is not None:
            try:
                resolved_root = root_dir.resolve().as_posix()
                s = s.replace(resolved_root, "")
                s = s.replace(root_dir.as_posix(), "")
            except Exception:
                pass
        s = s.lstrip("./").lstrip("/")
        return s.strip()

    @classmethod
    def _normalize_diagnostic_line(cls, line: str, root_dir: Optional[Path] = None) -> str:
        s = line.strip()
        if root_dir is not None:
            try:
                s = s.replace("\\", "/")
                resolved_root = root_dir.resolve().as_posix()
                s = s.replace(resolved_root, "")
                s = s.replace(root_dir.as_posix(), "")
            except Exception:
                pass
        return s.strip()

    @classmethod
    def parse_diagnostics(
        cls, output: str, root_dir: Optional[Path] = None
    ) -> List[DiagnosticError]:
        raw_lines = [line.strip() for line in output.splitlines() if line.strip()]
        errors: List[DiagnosticError] = []

        ts_pat1 = re.compile(r"^(.*?):(\d+):(\d+)\s*-\s*error(?:\s+([A-Za-z0-9]+))?:\s*(.*)$")
        ts_pat2 = re.compile(r"^(.*?)\((\d+),\s*(\d+)\):\s*error(?:\s+([A-Za-z0-9]+))?:\s*(.*)$")
        mvn_pat3 = re.compile(r"^\[ERROR\]\s*(.*?):\[(\d+),\s*(\d+)\]\s*(.*)$")
        mvn_pat4 = re.compile(r"^\[ERROR\]\s*(.*?):(\d+):\s*(?:error:\s*)?(.*)$")
        kt_pat5 = re.compile(r"^e:\s*(.*?):(?:\s*\(?(\d+),\s*(\d+)\)?|(\d+):(\d+)):?\s*(?:error:\s*)?(.*)$")
        rust_pat6a = re.compile(r"^([a-zA-Z0-9_./\\-]+):(\d+):(\d+):\s*error(?:\[([A-Za-z0-9]+)\])?:\s*(.*)$")
        rust_pat6b = re.compile(r"^error\[([A-Za-z0-9]+)\]:\s*(.*)$")
        py_pat7a = re.compile(r"^SyntaxError in (.*?):\s*(.*?)(?:\s*\(line \d+\))?$")
        py_pat7b = re.compile(r"^Compile error in (.*?):\s*(.*)$")
        generic_pat8 = re.compile(r"^([a-zA-Z0-9_./\\-]+):(\d+)(?::(\d+))?:\s*(?:(?:error|syntax error|warning):\s*)?(.*)$", re.IGNORECASE)

        for line in raw_lines:
            m = ts_pat1.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                code = m.group(4) or ""
                msg = m.group(5).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code=code, message=msg, line=int(m.group(2)), column=int(m.group(3))))
                continue

            m = ts_pat2.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                code = m.group(4) or ""
                msg = m.group(5).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code=code, message=msg, line=int(m.group(2)), column=int(m.group(3))))
                continue

            m = mvn_pat3.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                msg = m.group(4).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code="", message=msg, line=int(m.group(2)), column=int(m.group(3))))
                continue

            m = mvn_pat4.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                msg = m.group(3).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code="", message=msg, line=int(m.group(2)), column=None))
                continue

            m = kt_pat5.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                line_no = int(m.group(2) or m.group(4) or 0)
                col_no = int(m.group(3) or m.group(5) or 0)
                msg = m.group(6).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code="", message=msg, line=line_no, column=col_no))
                continue

            m = rust_pat6a.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                code = m.group(4) or ""
                msg = m.group(5).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code=code, message=msg, line=int(m.group(2)), column=int(m.group(3))))
                continue

            m = rust_pat6b.match(line)
            if m:
                code = m.group(1) or ""
                msg = m.group(2).strip()
                errors.append(DiagnosticError(raw_line=line, file="", code=code, message=msg))
                continue

            m = py_pat7a.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                msg = m.group(2).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code="SyntaxError", message=msg))
                continue
            m = py_pat7b.match(line)
            if m:
                file_p = cls._normalize_diagnostic_path(m.group(1), root_dir)
                msg = m.group(2).strip()
                errors.append(DiagnosticError(raw_line=line, file=file_p, code="CompileError", message=msg))
                continue

            m = generic_pat8.match(line)
            if m:
                candidate_file = m.group(1).strip()
                ext = Path(candidate_file).suffix.lower()
                if ext in ALL_SOURCE_EXTENSIONS or "/" in candidate_file or "\\" in candidate_file:
                    file_p = cls._normalize_diagnostic_path(candidate_file, root_dir)
                    line_no = int(m.group(2))
                    col_no = int(m.group(3)) if m.group(3) else None
                    raw_msg = m.group(4).strip()
                    code_match = re.match(r"^(?:error\s+)?([A-Za-z0-9]+):\s*(.*)$", raw_msg)
                    if code_match and code_match.group(1).startswith(("TS", "E", "CS")):
                        code = code_match.group(1)
                        msg = code_match.group(2).strip()
                    else:
                        code = ""
                        msg = raw_msg
                    errors.append(DiagnosticError(raw_line=line, file=file_p, code=code, message=msg, line=line_no, column=col_no))
                    continue

            if line.lower().startswith("error:"):
                msg = line[6:].strip()
                errors.append(DiagnosticError(raw_line=line, file="", code="", message=msg))
                continue

        return errors

    @classmethod
    def _extract_errors(
        cls, output: str, root_dir: Optional[Path] = None
    ) -> List[DiagnosticError]:
        return cls.parse_diagnostics(output, root_dir)

    @classmethod
    def _extract_error_lines(
        cls, output: str, root_dir: Optional[Path] = None
    ) -> List[str]:
        return [e.raw_line for e in cls.parse_diagnostics(output, root_dir)]

    def check_compile(
        self,
        diff: Optional[str] = None,
        scope: Optional[Union[List[Union[str, Path]], str, Path]] = None,
        base_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        if diff is None:
            diff = self.get_staged_diff()
        diff_str = str(diff)

        # 1. Exempt if docs-only diff
        if self._is_docs_only_diff(diff_str):
            record = {
                "ecosystem": None,
                "command": None,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.EXEMPT.value,
                "detail": "Docs-only diff exempt from compile.",
            }
            self.last_compile_run = record
            return record

        touched_files = list(self._extract_files_from_diff(diff_str))
        if scope is not None:
            if isinstance(scope, (str, Path)):
                scope_set = {Path(scope).as_posix()}
            else:
                scope_set = {Path(s).as_posix() for s in scope}
            if scope_set:
                touched_files = [f for f in touched_files if Path(f).as_posix() in scope_set]

        touched_source_files = [
            f for f in touched_files if Path(f).suffix.lower() in ALL_SOURCE_EXTENSIONS
        ]

        # 2. Exempt if no source files touched (e.g. CSS, JSON, images, docs, IaC)
        if not touched_source_files:
            record = {
                "ecosystem": None,
                "command": None,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.EXEMPT.value,
                "detail": "Diff touches no compiled source files.",
            }
            self.last_compile_run = record
            return record

        # 3. Find matching ecosystem handler
        handler: Optional[BaseCompileHandler] = None
        for h in ECOSYSTEM_COMPILE_HANDLERS:
            if h.matches(set(touched_source_files), self.worktree_dir, self.repo_dir):
                handler = h
                break

        if handler is None:
            record = {
                "ecosystem": None,
                "command": None,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.EXEMPT.value,
                "detail": f"Diff touches no source files of a detected ecosystem.",
            }
            self.last_compile_run = record
            return record

        # 4. Prepare command and check environment
        cmd, outcome_override, prep_detail = handler.prepare_and_get_command(
            self.worktree_dir, self.repo_dir, touched_source_files
        )
        if outcome_override is not None:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": outcome_override.value,
                "detail": prep_detail,
            }
            self.last_compile_run = record
            return record

        # 5. Run compile in worktree_dir
        try:
            exit_code, stdout, stderr = handler.run_command(
                cmd, self.worktree_dir, touched_source_files, is_base=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.ENV_NOT_READY.value,
                "detail": f"Environment not ready: {type(e).__name__} running compile command {cmd}: {e}",
            }
            self.last_compile_run = record
            return record
        except Exception as e:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.ENV_NOT_READY.value,
                "detail": f"Environment not ready: {type(e).__name__} running compile command {cmd}: {e}",
            }
            self.last_compile_run = record
            return record

        stdout_tail = stdout[-1000:] if stdout else ""
        stderr_tail = stderr[-1000:] if stderr else ""
        combined_output = f"{stdout}\n{stderr}".strip()
        output_tail = combined_output[-1000:] if combined_output else ""

        if exit_code == 0:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": 0,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "output_tail": output_tail,
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.PASSED.value,
                "detail": "Compile passed.",
            }
            self.last_compile_run = record
            return record

        # 6. Exit code != 0: Extract errors and run baseline comparison in a detached worktree at base_commit
        wt_errors = self.parse_diagnostics(combined_output, self.worktree_dir)
        if not wt_errors:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "output_tail": output_tail,
                "new_errors": [],
                "base_errors": [],
                "outcome": CompileOutcome.FAILED.value,
                "detail": f"Compile command exited {exit_code} but no parsable errors were found; output tail: {output_tail}",
            }
            self.last_compile_run = record
            return record

        base_commit_ref = base_commit or self.base_commit or "HEAD"
        worktree_base = self.repo_dir / ".jev-worktrees"
        worktree_base.mkdir(parents=True, exist_ok=True)

        exclude_file = self.repo_dir / ".git" / "info" / "exclude"
        if exclude_file.exists():
            try:
                content = exclude_file.read_text(encoding="utf-8")
                entries = []
                if ".jev-worktrees" not in content.splitlines():
                    entries.append(".jev-worktrees")
                if ".jev-worktrees/" not in content.splitlines():
                    entries.append(".jev-worktrees/")
                if "node_modules" not in content.splitlines():
                    entries.append("node_modules")
                if "node_modules/" not in content.splitlines():
                    entries.append("node_modules/")
                if entries:
                    exclude_file.write_text(content.rstrip() + "\n" + "\n".join(entries) + "\n", encoding="utf-8")
            except Exception:
                pass

        base_wt_path = (worktree_base / f"baseline-{uuid.uuid4().hex[:8]}").resolve()

        res = self._run_repo_git(["worktree", "add", "--detach", str(base_wt_path), base_commit_ref])
        if res.returncode != 0:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "output_tail": output_tail,
                "new_errors": [e.raw_line for e in wt_errors],
                "base_errors": [],
                "outcome": CompileOutcome.ENV_NOT_READY.value,
                "detail": f"Environment not ready: failed to create temporary baseline worktree at {base_commit_ref}: {res.stderr or res.stdout}",
            }
            self.last_compile_run = record
            return record

        base_exit = None
        base_stdout = ""
        base_stderr = ""
        try:
            self._link_node_modules(self.repo_dir, base_wt_path)
            base_exit, base_stdout, base_stderr = handler.run_command(
                cmd, base_wt_path, touched_source_files, is_base=True, base_commit=base_commit_ref
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "output_tail": output_tail,
                "new_errors": [e.raw_line for e in wt_errors],
                "base_errors": [],
                "outcome": CompileOutcome.ENV_NOT_READY.value,
                "detail": f"Environment not ready: {type(e).__name__} running base compile command {cmd}: {e}",
            }
            self.last_compile_run = record
            return record
        except Exception as e:
            record = {
                "ecosystem": handler.name,
                "command": cmd,
                "exit_code": exit_code,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "output_tail": output_tail,
                "new_errors": [e.raw_line for e in wt_errors],
                "base_errors": [],
                "outcome": CompileOutcome.ENV_NOT_READY.value,
                "detail": f"Environment not ready: {type(e).__name__} running base compile command {cmd}: {e}",
            }
            self.last_compile_run = record
            return record
        finally:
            self._unlink_node_modules(base_wt_path)
            self._run_repo_git(["worktree", "remove", "--force", str(base_wt_path)])
            if base_wt_path.exists():
                self._force_rmtree(base_wt_path)
            self._run_repo_git(["worktree", "prune"])

        base_combined = f"{base_stdout}\n{base_stderr}".strip()
        base_errors = self.parse_diagnostics(base_combined, base_wt_path)

        remaining_base = Counter(e.key for e in base_errors)
        new_error_objs = []
        for err in wt_errors:
            if remaining_base[err.key] > 0:
                remaining_base[err.key] -= 1
            else:
                new_error_objs.append(err)

        new_error_lines = [e.raw_line for e in new_error_objs]
        base_error_lines = [e.raw_line for e in base_errors]

        if not new_error_objs:
            outcome = CompileOutcome.PASSED
            detail = f"Pre-existing errors passed ({len(wt_errors)} pre-existing error(s) present in base commit)."
        else:
            outcome = CompileOutcome.FAILED
            err_preview = new_error_lines[0] if new_error_lines else "Unknown compile failure"
            detail = f"Compile failed with {len(new_error_objs)} new error(s): {err_preview}"

        record = {
            "ecosystem": handler.name,
            "command": cmd,
            "exit_code": exit_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "output_tail": output_tail,
            "new_errors": new_error_lines,
            "base_errors": base_error_lines,
            "outcome": outcome.value,
            "detail": detail,
        }
        self.last_compile_run = record
        return record

    def _finalize_mechanical_result(self, res: MechanicalCheckResult) -> MechanicalCheckResult:
        self._compute_provenance_deltas()
        res.checks["provenance"] = {
            "worker_staged_paths": list(self.worker_staged_paths),
            "engine_touched_paths": sorted(list(self.engine_touched_paths)),
            "collision_paths": sorted(list(self.collision_paths)),
        }
        self._discard_engine_touched()
        return res

    def run_mechanical_checks(
        self,
        subgoal: Subgoal,
        base_commit: Optional[str] = None,
        test_policy: str = "auto",
        test_targets: Optional[Sequence[Union[str, Path]]] = None,
    ) -> MechanicalCheckResult:
        self.last_test_run = None
        self.last_compile_run = None

        # 1. check_build
        build_res = self.check_build(subgoal.scope)
        if not build_res.passed:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="build",
                detail=build_res.detail,
                checks_run=["build"],
                checks={
                    "build": {"ran": True, "passed": False, "detail": build_res.detail},
                    "suppression": {"ran": False, "passed": None},
                    "unresolved_import": {"ran": False, "passed": None},
                    "compile": {"ran": False, "passed": None},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=None,
                test_runner_outcome=None,
            ))

        # 2. check_suppression
        diff = self.get_staged_diff()
        suppression_res = self.check_suppression(diff=diff)
        if not suppression_res.passed:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="suppression",
                detail=suppression_res.detail,
                checks_run=["build", "suppression"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": False, "detail": suppression_res.detail},
                    "unresolved_import": {"ran": False, "passed": None},
                    "compile": {"ran": False, "passed": None},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=None,
                test_runner_outcome=None,
            ))

        # 3. check_unresolved_imports
        unresolved_res = self.check_unresolved_imports(diff=diff)
        if not unresolved_res.passed:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="unresolved_import",
                detail=unresolved_res.detail,
                checks_run=["build", "suppression", "unresolved_import"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": False, "detail": unresolved_res.detail},
                    "compile": {"ran": False, "passed": None},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=None,
                test_runner_outcome=None,
            ))

        # 4. check_compile
        compile_res = self.check_compile(diff=diff, scope=subgoal.scope, base_commit=base_commit)
        compile_outcome = compile_res.get("outcome")

        if compile_outcome == CompileOutcome.FAILED.value:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="compile",
                detail=compile_res.get("detail", "Compile check failed."),
                checks_run=["build", "suppression", "unresolved_import", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            ))
        elif compile_outcome == CompileOutcome.NO_COMPILE_COMMAND.value:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="no_compile_command",
                detail=compile_res.get("detail", "No compile command found."),
                checks_run=["build", "suppression", "unresolved_import", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            ))
        elif compile_outcome == CompileOutcome.ENV_NOT_READY.value:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="env_not_ready",
                detail=compile_res.get("detail", "Environment not ready."),
                checks_run=["build", "suppression", "unresolved_import", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            ))

        # 5. run_tests
        should_run_tests = False
        if test_policy == "always":
            should_run_tests = True
        elif test_policy == "auto":
            should_run_tests = getattr(subgoal, "expects_tests", True)
        elif test_policy in ("never", "verify-only"):
            should_run_tests = False

        if should_run_tests:
            test_outcome = self.run_tests(targets=test_targets)
            test_run_details = getattr(self, "last_test_run", None)
            if isinstance(test_run_details, dict):
                test_run_details["ran"] = True
        else:
            test_outcome = TestOutcome.NO_TESTS_COLLECTED
            self._record_test_run(
                None, None, None, "", "", TestOutcome.NO_TESTS_COLLECTED
            )
            test_run_details = getattr(self, "last_test_run", None)
            if isinstance(test_run_details, dict):
                test_run_details["ran"] = False

        test_classification: Optional[str] = None
        full_suite_clean: Optional[bool] = None
        baseline_failures: Optional[int] = None
        new_failures: Optional[int] = None
        baseline_failing_ids: Optional[List[str]] = None
        new_failing_ids: Optional[List[str]] = None

        if test_policy == "never":
            test_classification = TestOutcome.EXEMPT_BY_POLICY.value
            full_suite_clean = None
            baseline_failures = 0
            new_failures = 0
        elif not should_run_tests:
            if not getattr(subgoal, "expects_tests", True):
                test_classification = TestOutcome.NO_TESTS_COLLECTED.value
            else:
                test_classification = TestOutcome.EXEMPT_BY_POLICY.value
            full_suite_clean = None
            baseline_failures = 0
            new_failures = 0

        if should_run_tests and test_outcome == TestOutcome.ENV_NOT_READY:
            err_detail = (
                (test_run_details.get("stderr_tail") or test_run_details.get("output_tail") or "").strip()
                if test_run_details
                else "Test runner executable not found."
            )
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="env_not_ready",
                detail=f"Environment not ready: {err_detail}",
                checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": True, "passed": False, "detail": f"Environment not ready: {err_detail}"},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=test_run_details,
                test_classification=TestOutcome.ENV_NOT_READY.value,
                full_suite_clean=False,
            ))
        elif should_run_tests and test_outcome == TestOutcome.NO_TESTS_COLLECTED:
            test_classification = TestOutcome.NO_TESTS_COLLECTED.value
            full_suite_clean = None
            baseline_failures = 0
            new_failures = 0
            if subgoal.expects_tests:
                if not self._is_docs_only_diff(diff):
                    detail_msg = "No tests collected when expects_tests is True and diff contains code changes."
                    return self._finalize_mechanical_result(MechanicalCheckResult(
                        passed=False,
                        failed_check="no_tests_collected",
                        detail=detail_msg,
                        checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                        checks={
                            "build": {"ran": True, "passed": True, "detail": ""},
                            "suppression": {"ran": True, "passed": True, "detail": ""},
                            "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                            "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                            "tests": {"ran": True, "passed": False, "detail": detail_msg},
                            "scope": {"ran": False, "passed": None},
                        },
                        compile_outcome=compile_res,
                        test_runner_outcome=test_run_details,
                        test_classification=test_classification,
                        full_suite_clean=False,
                        baseline_failures=0,
                        new_failures=0,
                    ))
        elif should_run_tests and test_outcome in (TestOutcome.PASSED, TestOutcome.FAILED):
            commit_key = str(base_commit).strip() if base_commit else (self._get_head_commit() or "HEAD")
            baseline = self._baseline_test_cache.get(commit_key)

            cand_ids = test_run_details.get("failing_test_ids") if isinstance(test_run_details, dict) else None
            cand_exit = test_run_details.get("exit_code") if isinstance(test_run_details, dict) else None

            if baseline is not None:
                base_err = baseline.get("error")
                base_unp = baseline.get("unparseable")
                base_timeout = baseline.get("timed_out")
                base_failing_raw = baseline.get("failing_ids")
                base_set = set(base_failing_raw) if base_failing_raw is not None else None

                if base_err or base_unp or base_timeout or base_set is None:
                    reason = base_err or ("timed out" if base_timeout else "unparseable test IDs in baseline")
                    detail_msg = f"Baseline run failed ({reason}); failing closed as REGRESSION_DETECTED."
                    return self._finalize_mechanical_result(MechanicalCheckResult(
                        passed=False,
                        failed_check="tests",
                        detail=detail_msg,
                        checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                        checks={
                            "build": {"ran": True, "passed": True, "detail": ""},
                            "suppression": {"ran": True, "passed": True, "detail": ""},
                            "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                            "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                            "tests": {"ran": True, "passed": False, "detail": detail_msg},
                            "scope": {"ran": False, "passed": None},
                        },
                        compile_outcome=compile_res,
                        test_runner_outcome=test_run_details,
                        test_classification=TestOutcome.REGRESSION_DETECTED.value,
                        full_suite_clean=False,
                        baseline_failures=None,
                        new_failures=1,
                    ))

                if test_outcome == TestOutcome.FAILED or (cand_exit is not None and cand_exit != 0):
                    if cand_ids is None:
                        detail_msg = "Test IDs unparseable from failing run; failing closed as REGRESSION_DETECTED."
                        return self._finalize_mechanical_result(MechanicalCheckResult(
                            passed=False,
                            failed_check="tests",
                            detail=detail_msg,
                            checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                            checks={
                                "build": {"ran": True, "passed": True, "detail": ""},
                                "suppression": {"ran": True, "passed": True, "detail": ""},
                                "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                                "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                                "tests": {"ran": True, "passed": False, "detail": detail_msg},
                                "scope": {"ran": False, "passed": None},
                            },
                            compile_outcome=compile_res,
                            test_runner_outcome=test_run_details,
                            test_classification=TestOutcome.REGRESSION_DETECTED.value,
                            full_suite_clean=False,
                            baseline_failures=len(base_set),
                            new_failures=1,
                        ))

                    cand_set = set(cand_ids)
                    new_failing_set = cand_set - base_set
                    if len(new_failing_set) > 0:
                        new_list = sorted(list(new_failing_set))
                        detail_msg = f"Regressions detected ({len(new_failing_set)} new failing test(s)): {', '.join(new_list)}"
                        return self._finalize_mechanical_result(MechanicalCheckResult(
                            passed=False,
                            failed_check="tests",
                            detail=detail_msg,
                            checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                            checks={
                                "build": {"ran": True, "passed": True, "detail": ""},
                                "suppression": {"ran": True, "passed": True, "detail": ""},
                                "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                                "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                                "tests": {"ran": True, "passed": False, "detail": detail_msg},
                                "scope": {"ran": False, "passed": None},
                            },
                            compile_outcome=compile_res,
                            test_runner_outcome=test_run_details,
                            test_classification=TestOutcome.REGRESSION_DETECTED.value,
                            full_suite_clean=(len(base_set) == 0),
                            baseline_failures=len(base_set),
                            new_failures=len(new_failing_set),
                            baseline_failing_ids=sorted(list(base_set)),
                            new_failing_ids=new_list,
                        ))
                    else:
                        test_classification = TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
                        full_suite_clean = False
                        baseline_failures = len(base_set)
                        new_failures = 0
                        baseline_failing_ids = sorted(list(base_set))
                        new_failing_ids = []
                else:
                    if len(base_set) > 0:
                        test_classification = TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
                        full_suite_clean = False
                        baseline_failures = len(base_set)
                        new_failures = 0
                        baseline_failing_ids = sorted(list(base_set))
                        new_failing_ids = []
                    else:
                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
                        full_suite_clean = True
                        baseline_failures = 0
                        new_failures = 0
                        baseline_failing_ids = []
                        new_failing_ids = []
            else:
                if test_outcome == TestOutcome.FAILED or (cand_exit is not None and cand_exit != 0):
                    commit_desc = base_commit or commit_key or "unknown"
                    test_detail = f"no baseline for {commit_desc}"
                    cand_set = set(cand_ids) if cand_ids else set()
                    return self._finalize_mechanical_result(MechanicalCheckResult(
                        passed=False,
                        failed_check="tests",
                        detail=test_detail,
                        checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                        checks={
                            "build": {"ran": True, "passed": True, "detail": ""},
                            "suppression": {"ran": True, "passed": True, "detail": ""},
                            "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                            "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                            "tests": {"ran": True, "passed": False, "detail": test_detail},
                            "scope": {"ran": False, "passed": None},
                        },
                        compile_outcome=compile_res,
                        test_runner_outcome=test_run_details,
                        test_classification=TestOutcome.REGRESSION_DETECTED.value,
                        full_suite_clean=False,
                        baseline_failures=None,
                        new_failures=len(cand_set) if cand_set else 1,
                        baseline_failing_ids=[],
                        new_failing_ids=sorted(list(cand_set)) if cand_set else [],
                    ))
                else:
                    test_classification = TestOutcome.FULL_SUITE_PASSED.value
                    full_suite_clean = True
                    baseline_failures = 0
                    new_failures = 0
                    baseline_failing_ids = []
                    new_failing_ids = []
        elif test_outcome == TestOutcome.NO_TEST_FRAMEWORK:
            test_classification = TestOutcome.NO_TEST_FRAMEWORK.value
            full_suite_clean = True
            baseline_failures = 0
            new_failures = 0

        # 6. check_scope
        scope_res = self.check_scope(diff, subgoal.scope)
        if not scope_res.passed:
            return self._finalize_mechanical_result(MechanicalCheckResult(
                passed=False,
                failed_check="scope",
                detail=scope_res.detail,
                checks_run=["build", "suppression", "unresolved_import", "compile", "tests", "scope"] if should_run_tests else ["build", "suppression", "unresolved_import", "compile", "scope"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "suppression": {"ran": True, "passed": True, "detail": ""},
                    "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": should_run_tests, "passed": True, "detail": "" if should_run_tests else f"Skipped per test policy: {test_policy}"},
                    "scope": {"ran": True, "passed": False, "detail": scope_res.detail},
                },
                compile_outcome=compile_res,
                test_runner_outcome=test_run_details,
                test_classification=test_classification,
                full_suite_clean=full_suite_clean,
                baseline_failures=baseline_failures,
                new_failures=new_failures,
                baseline_failing_ids=baseline_failing_ids,
                new_failing_ids=new_failing_ids,
            ))

        untested_flag = (
            f"Untested pass: skipped per test_policy={test_policy}."
            if not should_run_tests
            else (
                "Untested pass: NO_TEST_FRAMEWORK."
                if test_outcome == TestOutcome.NO_TEST_FRAMEWORK
                else (
                    "Untested pass: docs-only diff with NO_TESTS_COLLECTED."
                    if test_outcome == TestOutcome.NO_TESTS_COLLECTED and subgoal.expects_tests
                    else (
                        "Untested pass: expects_tests=False with NO_TESTS_COLLECTED."
                        if test_outcome == TestOutcome.NO_TESTS_COLLECTED
                        else ""
                    )
                )
            )
        )
        success_detail = untested_flag
        if not success_detail:
            if test_classification == TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value:
                success_detail = f"Regression-free: {baseline_failures or 0} baseline failure(s) present, 0 new failures."
            else:
                success_detail = "passed"

        return self._finalize_mechanical_result(MechanicalCheckResult(
            passed=True,
            failed_check=None,
            detail=success_detail,
            checks_run=["build", "suppression", "unresolved_import", "compile", "tests", "scope"] if should_run_tests else ["build", "suppression", "unresolved_import", "compile", "scope"],
            checks={
                "build": {"ran": True, "passed": True, "detail": ""},
                "suppression": {"ran": True, "passed": True, "detail": ""},
                "unresolved_import": {"ran": True, "passed": True, "detail": ""},
                "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                "tests": {"ran": should_run_tests, "passed": True, "detail": success_detail},
                "scope": {"ran": True, "passed": True, "detail": ""},
            },
            compile_outcome=compile_res,
            test_runner_outcome=test_run_details,
            test_classification=test_classification,
            full_suite_clean=full_suite_clean,
            baseline_failures=baseline_failures,
            new_failures=new_failures,
            baseline_failing_ids=baseline_failing_ids,
            new_failing_ids=new_failing_ids,
        ))

