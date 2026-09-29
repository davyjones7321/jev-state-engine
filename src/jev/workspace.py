import ast
import json
import os
import re
import shutil
import stat
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
        self.base_commit: Optional[str] = self._get_head_commit()

    def _get_head_commit(self) -> Optional[str]:
        res = self._run_git(["rev-parse", "HEAD"])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        return None

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

    def get_cumulative_diff(self) -> str:
        """Returns the full cumulative diff since workspace initialization (both committed and uncommitted)."""
        if self.base_commit:
            res = self._run_git(["diff", self.base_commit])
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
    def _force_rmtree(path: Path) -> None:
        def on_error(func, p, exc_info):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except Exception:
                pass
        if path.exists():
            shutil.rmtree(path, onerror=on_error)

    def create_subgoal_worktree(self, subgoal_id: str) -> Path:
        """Creates an isolated git worktree for a subgoal."""
        worktree_base = self.repo_dir / ".jev-worktrees"
        worktree_base.mkdir(parents=True, exist_ok=True)

        exclude_file = self.repo_dir / ".git" / "info" / "exclude"
        if exclude_file.exists():
            try:
                content = exclude_file.read_text(encoding="utf-8")
                if ".jev-worktrees" not in content:
                    exclude_file.write_text(content.rstrip() + "\n.jev-worktrees/\n", encoding="utf-8")
            except Exception:
                pass

        worktree_path = (worktree_base / f"subgoal-{subgoal_id}").resolve()
        branch_name = f"jev-subgoal-{subgoal_id}"

        # Clean up if prior branch or worktree directory exists
        if worktree_path.exists():
            self._run_repo_git(["worktree", "remove", "--force", str(worktree_path)])
            if worktree_path.exists():
                self._force_rmtree(worktree_path)

        self._run_repo_git(["branch", "-D", branch_name])

        res = self._run_repo_git(["worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"])
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {res.stderr or res.stdout}")

        self.worktree_dir = worktree_path
        self.current_worktree_path = worktree_path
        self.current_worktree_branch = branch_name
        return worktree_path

    def merge_subgoal_worktree(
        self,
        worktree_path: Optional[Union[str, Path]] = None,
        branch_name: Optional[str] = None,
    ) -> None:
        """Merges changes from the subgoal worktree into the main repo branch and cleans up."""
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
            merge_res = self._run_repo_git(["merge", "--ff-only", br_name])
            if merge_res.returncode != 0:
                self._run_repo_git(["merge", "--abort"])
                raise RuntimeError(
                    f"Failed to fast-forward merge subgoal branch {br_name}: {merge_res.stderr or merge_res.stdout}"
                )

        if wt_path:
            self._run_repo_git(["worktree", "remove", "--force", str(wt_path)])
            if wt_path.exists():
                self._force_rmtree(wt_path)
            self._run_repo_git(["worktree", "prune"])

        if br_name:
            self._run_repo_git(["branch", "-D", br_name])

        self.current_worktree_path = None
        self.current_worktree_branch = None

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
                        elif (wt / "pnpm-lock.yaml").exists():
                            cmd = ["pnpm", "test"]
                        else:
                            cmd = ["npm", "test"]
                        res = subprocess.run(
                            cmd,
                            cwd=wt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                        return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
            except Exception:
                pass

        # 2. go.mod
        if (wt / "go.mod").exists():
            try:
                res = subprocess.run(
                    ["go", "test", "./..."],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
            except Exception:
                return TestOutcome.FAILED

        # 3. Cargo.toml
        if (wt / "Cargo.toml").exists():
            try:
                res = subprocess.run(
                    ["cargo", "test"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
            except Exception:
                return TestOutcome.FAILED

        # 4. pom.xml
        if (wt / "pom.xml").exists():
            mvn_cmd = "mvn"
            if (wt / "mvnw").exists():
                mvn_cmd = "./mvnw"
            elif (wt / "mvnw.cmd").exists():
                mvn_cmd = str(wt / "mvnw.cmd")
            try:
                res = subprocess.run(
                    [mvn_cmd, "test"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
            except Exception:
                return TestOutcome.FAILED

        # 5. build.gradle / build.gradle.kts
        if (wt / "build.gradle").exists() or (wt / "build.gradle.kts").exists():
            gradle_cmd = "gradle"
            if (wt / "gradlew").exists():
                gradle_cmd = "./gradlew"
            elif (wt / "gradlew.bat").exists():
                gradle_cmd = str(wt / "gradlew.bat")
            try:
                res = subprocess.run(
                    [gradle_cmd, "test"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
            except Exception:
                return TestOutcome.FAILED

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
            res = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=wt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res.returncode == 0:
                return TestOutcome.PASSED
            elif res.returncode == 5:
                return TestOutcome.NO_TESTS_COLLECTED
            else:
                return TestOutcome.FAILED

        # 7. IaC signals
        # Terraform
        has_tf = False
        try:
            has_tf = any(wt.glob("*.tf")) or any(p for p in wt.rglob("*.tf") if ".terraform" not in p.parts)
        except Exception:
            pass

        if has_tf:
            if shutil.which("terraform"):
                try:
                    res = subprocess.run(
                        ["terraform", "validate"],
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                except Exception:
                    return TestOutcome.FAILED
            return TestOutcome.NO_TEST_FRAMEWORK

        # Ansible
        has_ansible = (
            (wt / "ansible.cfg").exists()
            or (wt / "playbooks").is_dir()
            or any(wt.glob("playbook*.yml"))
            or any(wt.glob("playbook*.yaml"))
        )
        if has_ansible:
            if shutil.which("ansible-lint"):
                try:
                    res = subprocess.run(
                        ["ansible-lint"],
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                except Exception:
                    return TestOutcome.FAILED
            elif shutil.which("ansible-playbook"):
                playbooks = list(wt.glob("playbook*.yml")) + list(wt.glob("playbook*.yaml"))
                pb_arg = str(playbooks[0]) if playbooks else "."
                try:
                    res = subprocess.run(
                        ["ansible-playbook", "--syntax-check", pb_arg],
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                except Exception:
                    return TestOutcome.FAILED
            return TestOutcome.NO_TEST_FRAMEWORK

        # Helm
        if (wt / "Chart.yaml").exists():
            if shutil.which("helm"):
                try:
                    res = subprocess.run(
                        ["helm", "lint", "."],
                        cwd=wt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    return TestOutcome.PASSED if res.returncode == 0 else TestOutcome.FAILED
                except Exception:
                    return TestOutcome.FAILED
            return TestOutcome.NO_TEST_FRAMEWORK

        # 8. Fallback
        return TestOutcome.NO_TESTS_COLLECTED


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
        diff = self.get_staged_diff()
        if test_outcome == TestOutcome.FAILED:
            return MechanicalCheckResult(
                passed=False,
                failed_check="tests",
                detail="Unit tests failed.",
            )
        elif test_outcome == TestOutcome.NO_TESTS_COLLECTED:
            if subgoal.expects_tests:
                if not self._is_docs_only_diff(diff):
                    return MechanicalCheckResult(
                        passed=False,
                        failed_check="no_tests_collected",
                        detail="No tests collected when expects_tests is True and diff contains code changes.",
                    )
        elif test_outcome == TestOutcome.NO_TEST_FRAMEWORK:
            pass

        # 3. check_scope
        scope_res = self.check_scope(diff, subgoal.scope)
        if not scope_res.passed:
            return MechanicalCheckResult(
                passed=False,
                failed_check="scope",
                detail=scope_res.detail,
            )

        untested_flag = (
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
        return MechanicalCheckResult(
            passed=True,
            failed_check=None,
            detail=untested_flag,
        )

