import ast
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field

from jev.models import MechanicalCheckResult, Subgoal, TestOutcome


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

    def _run_git(self, args: List[str], check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=self.worktree_dir,
            capture_output=True,
            text=True,
            check=check,
        )

    def run_read_tool(self, cmd: str, args: Optional[List[str]] = None) -> str:
        full_cmd = [cmd] + (args or [])
        res = subprocess.run(
            full_cmd,
            cwd=self.worktree_dir,
            capture_output=True,
            text=True,
        )
        return res.stdout if res.returncode == 0 else res.stderr

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

    def commit_subgoal(self, message: str = "Subgoal committed") -> None:
        self._run_git(["add", "-A"])
        self._run_git(["commit", "-m", message])

    def rollback_subgoal(self) -> None:
        self._run_git(["reset", "--hard", "HEAD"])
        self._run_git(["clean", "-fd"])

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
        res = subprocess.run(
            [sys.executable, "-m", "pytest"],
            cwd=self.worktree_dir,
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return TestOutcome.PASSED
        elif res.returncode == 5:
            return TestOutcome.NO_TESTS_COLLECTED
        else:
            return TestOutcome.FAILED

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

    def run_mechanical_checks(self, subgoal: Subgoal) -> MechanicalCheckResult:
        # 1. check_build
        build_res = self.check_build(subgoal.scope)
        if not build_res.passed:
            return MechanicalCheckResult(
                passed=False,
                failed_check="build",
                detail=build_res.detail,
            )

        # 2. run_tests
        test_outcome = self.run_tests()
        if test_outcome == TestOutcome.FAILED:
            return MechanicalCheckResult(
                passed=False,
                failed_check="tests",
                detail="Unit tests failed.",
            )
        elif test_outcome == TestOutcome.NO_TESTS_COLLECTED:
            if subgoal.expects_tests:
                return MechanicalCheckResult(
                    passed=False,
                    failed_check="no_tests_collected",
                    detail="No tests collected when expects_tests is True.",
                )

        # 3. check_scope
        diff = self.get_staged_diff()
        scope_res = self.check_scope(diff, subgoal.scope)
        if not scope_res.passed:
            return MechanicalCheckResult(
                passed=False,
                failed_check="scope",
                detail=scope_res.detail,
            )

        untested_flag = (
            "Untested pass: expects_tests=False with NO_TESTS_COLLECTED."
            if test_outcome == TestOutcome.NO_TESTS_COLLECTED
            else ""
        )
        return MechanicalCheckResult(
            passed=True,
            failed_check=None,
            detail=untested_flag,
        )
