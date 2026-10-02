import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from jev.models import CompileOutcome, MechanicalCheckResult, Subgoal, TestOutcome


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
        res = subprocess.run(
            exec_cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return res.returncode, res.stdout, res.stderr


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


class Workspace:
    def __init__(
        self,
        repo_dir: Optional[Union[str, Path]] = None,
        worktree_dir: Optional[Union[str, Path]] = None,
    ):
        self.repo_dir = Path(repo_dir).resolve() if repo_dir else Path.cwd().resolve()
        self.worktree_dir = (
            Path(worktree_dir).resolve() if worktree_dir else self.repo_dir
        )
        self.base_commit: Optional[str] = self._get_head_commit()
        self.last_test_run: Optional[dict] = None
        self.last_compile_run: Optional[dict] = None

    def _record_test_run(
        self,
        ecosystem: Optional[str],
        command: Optional[List[str]],
        exit_code: Optional[int],
        stdout: str = "",
        stderr: str = "",
        outcome: TestOutcome = TestOutcome.NO_TESTS_COLLECTED,
    ) -> TestOutcome:
        stdout_tail = stdout[-1000:] if stdout else ""
        stderr_tail = stderr[-1000:] if stderr else ""
        output_tail = stdout_tail
        if stderr_tail:
            output_tail = f"{output_tail}\n[stderr]\n{stderr_tail}".strip() if output_tail else stderr_tail

        self.last_test_run = {
            "ecosystem": ecosystem,
            "command": command,
            "exit_code": exit_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "output_tail": output_tail,
            "outcome": outcome.value if hasattr(outcome, "value") else str(outcome),
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

    def run_read_tool(self, cmd: str, args: Optional[List[str]] = None) -> str:
        """Executes read-only CLI commands in worktree_dir and returns stdout or stderr."""
        args_list = args or []
        full_cmd = [cmd] + args_list
        if cmd == "cat" and not shutil.which("cat"):
            if args_list:
                target = Path(args_list[0])
                if not target.is_absolute():
                    target = self.worktree_dir / target
                try:
                    target.resolve().relative_to(self.worktree_dir.resolve())
                except ValueError:
                    return f"cat: {args_list[0]}: Access denied outside workspace"
                if target.is_dir():
                    return f"cat: {args_list[0]}: Is a directory"
                if target.exists() and target.is_file():
                    try:
                        return target.read_text(encoding="utf-8", errors="replace")
                    except Exception as e:
                        return str(e)
                return f"cat: {args_list[0]}: No such file or directory"
            return ""
        try:
            res = subprocess.run(
                full_cmd,
                cwd=self.worktree_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return res.stdout if res.returncode == 0 else res.stderr

        except FileNotFoundError:
            if cmd == "cat" and args_list:
                target = Path(args_list[0])
                if not target.is_absolute():
                    target = self.worktree_dir / target
                try:
                    target.resolve().relative_to(self.worktree_dir.resolve())
                except ValueError:
                    return f"cat: {args_list[0]}: Access denied outside workspace"
                if target.is_dir():
                    return f"cat: {args_list[0]}: Is a directory"
                if target.exists() and target.is_file():
                    try:
                        return target.read_text(encoding="utf-8", errors="replace")
                    except Exception as e:
                        return str(e)
                return f"cat: {args_list[0]}: No such file or directory"
            return f"Command not found: {cmd}"

    def stage_file_mutation(self, path: Union[str, Path], content: str) -> None:
        p = Path(path)
        if p.is_absolute():
            try:
                rel_path = p.resolve().relative_to(self.worktree_dir.resolve())
            except ValueError:
                rel_path = p
        else:
            rel_path = p
        target_path = self.worktree_dir / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        self._run_git(["add", str(rel_path)])

    def get_staged_diff(self) -> str:
        self._run_git(["add", "-A"])
        res = self._run_git(["diff", "HEAD"])
        if res.returncode != 0:
            res = self._run_git(["diff", "--cached"])
        return res.stdout

    def get_cumulative_diff(self, base_ref: Optional[str] = None) -> str:
        """Returns the full cumulative diff since workspace initialization or base_ref (both committed and uncommitted)."""
        ref = base_ref or self.base_commit
        if ref:
            res = self._run_git(["diff", ref])
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        return self.get_staged_diff()

    def commit_subgoal(self, message: str = "Subgoal committed") -> None:
        self._run_git(["add", "-A"])
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
                if ".jev-worktrees" not in content:
                    entries.append(".jev-worktrees/")
                if "node_modules" not in content:
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

    def run_tests(self) -> TestOutcome:
        wt = self.worktree_dir

        # 1. package.json test script
        pkg_json_path = wt / "package.json"
        if pkg_json_path.exists():
            try:
                pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(pkg_data, dict):
                    scripts = pkg_data.get("scripts")
                    if isinstance(scripts, dict) and "test" in scripts:
                        if (wt / "yarn.lock").exists():
                            cmd = ["yarn", "test"]
                            ecosystem = "yarn"
                        elif (wt / "pnpm-lock.yaml").exists():
                            cmd = ["pnpm", "test"]
                            ecosystem = "pnpm"
                        else:
                            cmd = ["npm", "test"]
                            ecosystem = "npm"
                        resolved = shutil.which(cmd[0])
                        if not resolved:
                            return self._record_test_run(
                                ecosystem, cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                            )
                        exec_cmd = [resolved] + cmd[1:]
                        res = subprocess.run(
                            exec_cmd,
                            cwd=wt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                        outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                        return self._record_test_run(ecosystem, cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("package_json", None, -1, "", str(e), TestOutcome.FAILED)

        # 2. go.mod
        if (wt / "go.mod").exists():
            cmd = ["go", "test", "./..."]
            resolved = shutil.which(cmd[0])
            if not resolved:
                return self._record_test_run(
                    "go", cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                )
            exec_cmd = [resolved] + cmd[1:]
            try:
                res = subprocess.run(
                    exec_cmd,
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                return self._record_test_run("go", cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("go", cmd, -1, "", str(e), TestOutcome.FAILED)

        # 3. Cargo.toml
        if (wt / "Cargo.toml").exists():
            cmd = ["cargo", "test"]
            resolved = shutil.which(cmd[0])
            if not resolved:
                return self._record_test_run(
                    "cargo", cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                )
            exec_cmd = [resolved] + cmd[1:]
            try:
                res = subprocess.run(
                    exec_cmd,
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                return self._record_test_run("cargo", cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("cargo", cmd, -1, "", str(e), TestOutcome.FAILED)

        # 4. pom.xml
        if (wt / "pom.xml").exists():
            mvn_cmd = "mvn"
            if sys.platform == "win32" and (wt / "mvnw.cmd").exists():
                mvn_cmd = str(wt / "mvnw.cmd")
            elif (wt / "mvnw").exists():
                mvn_cmd = "./mvnw"
            elif (wt / "mvnw.cmd").exists():
                mvn_cmd = str(wt / "mvnw.cmd")
            cmd = [mvn_cmd, "test"]
            resolved = shutil.which(cmd[0])
            if not resolved:
                return self._record_test_run(
                    "maven", cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                )
            exec_cmd = [resolved] + cmd[1:]
            try:
                res = subprocess.run(
                    exec_cmd,
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                return self._record_test_run("maven", cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("maven", cmd, -1, "", str(e), TestOutcome.FAILED)

        # 5. build.gradle / build.gradle.kts
        if (wt / "build.gradle").exists() or (wt / "build.gradle.kts").exists():
            gradle_cmd = "gradle"
            if sys.platform == "win32" and (wt / "gradlew.bat").exists():
                gradle_cmd = str(wt / "gradlew.bat")
            elif (wt / "gradlew").exists():
                gradle_cmd = "./gradlew"
            elif (wt / "gradlew.bat").exists():
                gradle_cmd = str(wt / "gradlew.bat")
            cmd = [gradle_cmd, "test"]
            resolved = shutil.which(cmd[0])
            if not resolved:
                return self._record_test_run(
                    "gradle", cmd, None, "", f"Executable '{cmd[0]}' not found.", TestOutcome.ENV_NOT_READY
                )
            exec_cmd = [resolved] + cmd[1:]
            try:
                res = subprocess.run(
                    exec_cmd,
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                return self._record_test_run("gradle", cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("gradle", cmd, -1, "", str(e), TestOutcome.FAILED)

        # 6. pytest signals
        has_pytest = False
        if (wt / "pytest.ini").exists():
            has_pytest = True
        elif (wt / "pyproject.toml").exists():
            try:
                pyproj_text = (wt / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
                if "[tool.pytest" in pyproj_text:
                    has_pytest = True
            except Exception:
                pass
        if not has_pytest:
            try:
                has_pytest = (
                    any(wt.glob("test_*.py"))
                    or any(wt.glob("*_test.py"))
                    or ((wt / "tests").is_dir() and (any((wt / "tests").glob("*.py")) or any((wt / "tests").rglob("*.py"))))
                )
            except Exception:
                pass

        if has_pytest:
            cmd = [sys.executable, "-m", "pytest", "-q"]
            try:
                res = subprocess.run(
                    cmd,
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if res.returncode == 0:
                    outcome = TestOutcome.PASSED
                elif res.returncode == 5:
                    outcome = TestOutcome.NO_TESTS_COLLECTED
                else:
                    outcome = TestOutcome.FAILED
                return self._record_test_run("pytest", cmd, res.returncode, res.stdout, res.stderr, outcome)
            except Exception as e:
                return self._record_test_run("pytest", cmd, -1, "", str(e), TestOutcome.FAILED)

        # 7. IaC signals
        # Terraform
        has_tf = False
        try:
            has_tf = any(wt.glob("*.tf")) or any(p for p in wt.rglob("*.tf") if ".terraform" not in p.parts)
        except Exception:
            pass

        if has_tf:
            if shutil.which("terraform"):
                cmd = ["terraform", "validate"]
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("terraform", cmd, res.returncode, res.stdout, res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("terraform", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "terraform", ["terraform", "validate"], None, "", "Executable 'terraform' not found.", TestOutcome.ENV_NOT_READY
            )

        # Ansible
        has_ansible = (
            (wt / "ansible.cfg").exists()
            or (wt / "playbooks").is_dir()
            or any(wt.glob("playbook*.yml"))
            or any(wt.glob("playbook*.yaml"))
        )
        if has_ansible:
            if shutil.which("ansible-lint"):
                cmd = ["ansible-lint"]
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("ansible", cmd, res.returncode, res.stdout, res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("ansible", cmd, -1, "", str(e), TestOutcome.FAILED)
            elif shutil.which("ansible-playbook"):
                playbooks = list(wt.glob("playbook*.yml")) + list(wt.glob("playbook*.yaml"))
                pb_arg = str(playbooks[0]) if playbooks else "."
                cmd = ["ansible-playbook", "--syntax-check", pb_arg]
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("ansible", cmd, res.returncode, res.stdout, res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("ansible", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "ansible", ["ansible-lint"], None, "", "Executable 'ansible-lint' not found.", TestOutcome.ENV_NOT_READY
            )

        # Helm
        if (wt / "Chart.yaml").exists():
            cmd = ["helm", "lint", "."]
            if shutil.which("helm"):
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    outcome = TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                    return self._record_test_run("helm", cmd, res.returncode, res.stdout, res.stderr, outcome)
                except Exception as e:
                    return self._record_test_run("helm", cmd, -1, "", str(e), TestOutcome.FAILED)
            return self._record_test_run(
                "helm", cmd, None, "", "Executable 'helm' not found.", TestOutcome.ENV_NOT_READY
            )

        # 8. Fallback
        return self._record_test_run(None, None, None, "", "", TestOutcome.NO_TESTS_COLLECTED)


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
                if ".jev-worktrees" not in content:
                    entries.append(".jev-worktrees/")
                if "node_modules" not in content:
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

    def run_mechanical_checks(
        self,
        subgoal: Subgoal,
        base_commit: Optional[str] = None,
        test_policy: str = "auto",
    ) -> MechanicalCheckResult:
        self.last_test_run = None
        self.last_compile_run = None

        # 1. check_build
        build_res = self.check_build(subgoal.scope)
        if not build_res.passed:
            return MechanicalCheckResult(
                passed=False,
                failed_check="build",
                detail=build_res.detail,
                checks_run=["build"],
                checks={
                    "build": {"ran": True, "passed": False, "detail": build_res.detail},
                    "compile": {"ran": False, "passed": None},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=None,
                test_runner_outcome=None,
            )

        # 2. check_compile
        diff = self.get_staged_diff()
        compile_res = self.check_compile(diff=diff, scope=subgoal.scope, base_commit=base_commit)
        compile_outcome = compile_res.get("outcome")

        if compile_outcome == CompileOutcome.FAILED.value:
            return MechanicalCheckResult(
                passed=False,
                failed_check="compile",
                detail=compile_res.get("detail", "Compile check failed."),
                checks_run=["build", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            )
        elif compile_outcome == CompileOutcome.NO_COMPILE_COMMAND.value:
            return MechanicalCheckResult(
                passed=False,
                failed_check="no_compile_command",
                detail=compile_res.get("detail", "No compile command found."),
                checks_run=["build", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            )
        elif compile_outcome == CompileOutcome.ENV_NOT_READY.value:
            return MechanicalCheckResult(
                passed=False,
                failed_check="env_not_ready",
                detail=compile_res.get("detail", "Environment not ready."),
                checks_run=["build", "compile"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": False, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": False, "passed": None},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=None,
            )

        # 3. run_tests
        should_run_tests = False
        if test_policy == "always":
            should_run_tests = True
        elif test_policy == "auto":
            should_run_tests = getattr(subgoal, "expects_tests", True)
        elif test_policy in ("never", "verify-only"):
            should_run_tests = False

        if should_run_tests:
            test_outcome = self.run_tests()
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

        if should_run_tests and test_outcome == TestOutcome.ENV_NOT_READY:
            err_detail = (
                (test_run_details.get("stderr_tail") or test_run_details.get("output_tail") or "").strip()
                if test_run_details
                else "Test runner executable not found."
            )
            return MechanicalCheckResult(
                passed=False,
                failed_check="env_not_ready",
                detail=f"Environment not ready: {err_detail}",
                checks_run=["build", "compile", "tests"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": True, "passed": False, "detail": f"Environment not ready: {err_detail}"},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=test_run_details,
            )
        elif should_run_tests and test_outcome == TestOutcome.FAILED:
            return MechanicalCheckResult(
                passed=False,
                failed_check="tests",
                detail="Unit tests failed.",
                checks_run=["build", "compile", "tests"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": True, "passed": False, "detail": "Unit tests failed."},
                    "scope": {"ran": False, "passed": None},
                },
                compile_outcome=compile_res,
                test_runner_outcome=test_run_details,
            )
        elif should_run_tests and test_outcome == TestOutcome.NO_TESTS_COLLECTED:
            if subgoal.expects_tests:
                if not self._is_docs_only_diff(diff):
                    detail_msg = "No tests collected when expects_tests is True and diff contains code changes."
                    return MechanicalCheckResult(
                        passed=False,
                        failed_check="no_tests_collected",
                        detail=detail_msg,
                        checks_run=["build", "compile", "tests"],
                        checks={
                            "build": {"ran": True, "passed": True, "detail": ""},
                            "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                            "tests": {"ran": True, "passed": False, "detail": detail_msg},
                            "scope": {"ran": False, "passed": None},
                        },
                        compile_outcome=compile_res,
                        test_runner_outcome=test_run_details,
                    )
        elif test_outcome == TestOutcome.NO_TEST_FRAMEWORK:
            pass

        # 4. check_scope
        scope_res = self.check_scope(diff, subgoal.scope)
        if not scope_res.passed:
            return MechanicalCheckResult(
                passed=False,
                failed_check="scope",
                detail=scope_res.detail,
                checks_run=["build", "compile", "tests", "scope"] if should_run_tests else ["build", "compile", "scope"],
                checks={
                    "build": {"ran": True, "passed": True, "detail": ""},
                    "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                    "tests": {"ran": should_run_tests, "passed": True, "detail": "" if should_run_tests else f"Skipped per test policy: {test_policy}"},
                    "scope": {"ran": True, "passed": False, "detail": scope_res.detail},
                },
                compile_outcome=compile_res,
                test_runner_outcome=test_run_details,
            )

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
        return MechanicalCheckResult(
            passed=True,
            failed_check=None,
            detail=untested_flag,
            checks_run=["build", "compile", "tests", "scope"] if should_run_tests else ["build", "compile", "scope"],
            checks={
                "build": {"ran": True, "passed": True, "detail": ""},
                "compile": {"ran": True, "passed": True, "detail": compile_res.get("detail", "")},
                "tests": {"ran": should_run_tests, "passed": True, "detail": untested_flag or "passed"},
                "scope": {"ran": True, "passed": True, "detail": ""},
            },
            compile_outcome=compile_res,
            test_runner_outcome=test_run_details,
        )

