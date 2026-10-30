"""src/jev/runner_registry.py

Runner-profile registry, Pydantic schema validation, and pure detection logic.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jev.mech_evidence import TargetConfidence


def validate_regex_pattern(pattern: str) -> str:
    """Validates regex pattern for length cap (200), nested quantifiers, and compilability."""
    if len(pattern) > 200:
        raise ValueError(f"Regex pattern exceeds 200 characters limit: {len(pattern)}")

    # Reject nested quantifiers like (a+)+, (a*)*, (a+){1,3}, (a|b+)+, (.*)*
    if re.search(r"\([^)]*[\*\+\?\}][^)]*\)\s*[\*\+\?\{]", pattern):
        raise ValueError(f"Nested quantifiers not allowed in pattern: {pattern}")

    # Reject consecutive quantifiers like a++ or a*+
    if re.search(r"[\*\+\?]\s*[\*\+\?]", pattern):
        raise ValueError(f"Consecutive quantifiers not allowed in pattern: {pattern}")

    # Reject brace quantifier followed by another quantifier like a{1,2}+
    if re.search(r"\}\s*[\*\+\?\{]", pattern):
        raise ValueError(f"Nested or consecutive quantifiers not allowed in pattern: {pattern}")

    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}")

    return pattern


class RunnerProfile(BaseModel):
    """Schema for a single test runner profile."""
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    registry_version: str
    language: str
    runner: str
    platforms: List[str]
    marker_files: List[str]
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    executable_rule: str
    build_argv_template: Optional[List[str]] = None
    test_argv_template: List[str]
    target_capabilities: List[str] = Field(default_factory=list)
    zero_test_exit_behavior: Optional[int] = None
    precedence: int = 100
    zero_test_downgrade_markers: List[str] = Field(default_factory=list)
    failure_identity_patterns: List[str] = Field(default_factory=list)
    empty_output_behavior: Optional[str] = "weak"
    generated_output_hints: List[str] = Field(default_factory=list)
    fixture_ids: List[str] = Field(default_factory=list)

    @field_validator("zero_test_downgrade_markers", "failure_identity_patterns", mode="after")
    @classmethod
    def validate_patterns(cls, v: List[str]) -> List[str]:
        for pattern in v:
            validate_regex_pattern(pattern)
        return v


class RunnerRegistry(BaseModel):
    """Container for the runner profiles registry."""
    model_config = ConfigDict(extra="forbid")

    registry_version: str
    profiles: List[RunnerProfile]

    def get_profile(self, profile_id: str) -> Optional[RunnerProfile]:
        for p in self.profiles:
            if p.profile_id == profile_id:
                return p
        return None


_REGISTRY_CACHE: Optional[RunnerRegistry] = None


def get_default_registry_path() -> Path:
    return Path(__file__).parent / "runner_profiles.json"


def load_registry(path: Optional[Union[str, Path]] = None, reload: bool = False) -> RunnerRegistry:
    """Loads and validates the runner profiles registry from JSON."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None and not reload and path is None:
        return _REGISTRY_CACHE

    reg_path = Path(path) if path is not None else get_default_registry_path()
    if not reg_path.exists():
        raise FileNotFoundError(f"Runner profiles registry file not found: {reg_path}")

    raw_data = json.loads(reg_path.read_text(encoding="utf-8"))
    registry = RunnerRegistry.model_validate(raw_data)

    if path is None and not reload:
        _REGISTRY_CACHE = registry
    return registry


def clear_registry_cache() -> None:
    """Clears the cached registry (useful for testing)."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None


def _parse_version_tuple(v_str: str) -> Tuple[int, ...]:
    parts = []
    for part in re.split(r"[.\-+]", v_str):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts) if parts else (0,)


def is_version_supported(
    profile: RunnerProfile,
    tool_version: Optional[str] = None,
) -> bool:
    """Evaluates version predicates only when a tool version is supplied.
    
    If tool_version is None, returns True (predicates evaluated in Phase 4 when tool version is supplied).
    """
    if tool_version is None:
        return True

    tv = _parse_version_tuple(tool_version)
    if profile.min_version is not None:
        min_v = _parse_version_tuple(profile.min_version)
        if tv < min_v:
            return False
    if profile.max_version is not None:
        max_v = _parse_version_tuple(profile.max_version)
        if tv > max_v:
            return False
    return True


def _derive_runner_profile_id_from_script(script_text: str) -> str:
    s = script_text.lower()
    if "bun test" in s:
        return "bun-test"
    if "vitest" in s:
        return "vitest"
    if "jest" in s:
        return "jest"
    if "mocha" in s:
        return "mocha"
    if "pytest" in s:
        return "pytest"
    if "node --test" in s or "node:test" in s:
        return "node-native-test"
    if "deno test" in s:
        return "deno-test"
    if "cargo test" in s:
        return "cargo-test"
    if re.search(r"\bgo\s+test\b", s):
        return "go-test"
    return "node-script-unknown"


def _matches_profile(profile: RunnerProfile, repo_dir: Path) -> bool:
    """Checks whether a profile's markers match the repository without running any subprocess."""
    pid = profile.profile_id

    if pid == "pytest":
        if (repo_dir / "pytest.ini").exists():
            return True
        pyproject = repo_dir / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8", errors="replace")
                if "[tool.pytest" in text:
                    return True
            except Exception:
                pass
        try:
            if any(repo_dir.glob("test_*.py")) or any(repo_dir.glob("*_test.py")):
                return True
            tests_dir = repo_dir / "tests"
            if tests_dir.is_dir() and (any(tests_dir.glob("*.py")) or any(tests_dir.rglob("*.py"))):
                return True
        except Exception:
            pass
        return False

    if pid == "terraform":
        try:
            return any(repo_dir.glob("*.tf")) or any(p for p in repo_dir.rglob("*.tf") if ".terraform" not in p.parts)
        except Exception:
            return False

    if pid == "ansible":
        if (repo_dir / "ansible.cfg").exists() or (repo_dir / "playbooks").is_dir():
            return True
        try:
            return any(repo_dir.glob("playbook*.yml")) or any(repo_dir.glob("playbook*.yaml"))
        except Exception:
            return False

    # Standard marker files
    for marker in profile.marker_files:
        if "*" in marker or "?" in marker:
            try:
                if any(repo_dir.glob(marker)):
                    return True
            except Exception:
                pass
        else:
            if (repo_dir / marker).exists():
                return True

    return False


@dataclass(frozen=True)
class ResolvedCommand:
    ecosystem: Optional[str]
    command: Optional[List[str]]
    source: str
    reason: Optional[str] = None
    runner_profile_id: Optional[str] = None
    registry_version: Optional[str] = None
    also_matched: Optional[List[str]] = None
    ambiguity_resolved_by: Optional[str] = None


def resolve_from_registry(
    wt: Union[str, Path],
    registry: Optional[RunnerRegistry] = None,
) -> ResolvedCommand:
    """Pure manifest and marker detection using the runner registry.
    
    Preserves exact existing command behavior for supported ecosystems.
    Runs NO subprocess.
    """
    wt = Path(wt)
    if registry is None:
        registry = load_registry()
    reg_ver = registry.registry_version

    # 1. package.json script precedence (behavior-preserving)
    pkg_json_path = wt / "package.json"
    if pkg_json_path.exists():
        try:
            pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pkg_data, dict):
                scripts = pkg_data.get("scripts")
                if isinstance(scripts, dict) and "test" in scripts:
                    script_text = str(scripts["test"]).strip()
                    profile_id = _derive_runner_profile_id_from_script(script_text)
                    if (wt / "yarn.lock").exists():
                        return ResolvedCommand(
                            ecosystem="yarn",
                            command=["yarn", "test"],
                            source="package.json",
                            reason=script_text,
                            runner_profile_id=profile_id,
                            registry_version=reg_ver,
                        )
                    elif (wt / "pnpm-lock.yaml").exists():
                        return ResolvedCommand(
                            ecosystem="pnpm",
                            command=["pnpm", "test"],
                            source="package.json",
                            reason=script_text,
                            runner_profile_id=profile_id,
                            registry_version=reg_ver,
                        )
                    else:
                        return ResolvedCommand(
                            ecosystem="npm",
                            command=["npm", "test"],
                            source="package.json",
                            reason=script_text,
                            runner_profile_id=profile_id,
                            registry_version=reg_ver,
                        )
        except Exception as e:
            return ResolvedCommand(
                ecosystem="package_json",
                command=None,
                source="package.json",
                reason=f"Failed to parse package.json: {e}",
                runner_profile_id="node-script-unknown",
                registry_version=reg_ver,
            )

    # 2. Pure marker detection against runner profiles
    matching_profiles: List[RunnerProfile] = []
    for profile in registry.profiles:
        if _matches_profile(profile, wt):
            matching_profiles.append(profile)

    if not matching_profiles:
        return ResolvedCommand(
            ecosystem=None,
            command=None,
            source="none",
            reason="No test framework or test files detected",
            runner_profile_id=None,
            registry_version=reg_ver,
        )

    also_matched: Optional[List[str]] = None
    ambiguity_resolved_by: Optional[str] = None

    if len(matching_profiles) > 1:
        # Tie-breaker used ONLY for layouts the old cascade resolved (precedence <= 9)
        OLD_CASCADE_MAX_PRECEDENCE = 9
        if all(p.precedence <= OLD_CASCADE_MAX_PRECEDENCE for p in matching_profiles):
            sorted_matches = sorted(matching_profiles, key=lambda p: p.precedence)
            winner = sorted_matches[0]
            also_matched = [p.profile_id for p in sorted_matches[1:]]
            ambiguity_resolved_by = "precedence"
            matching_profiles = [winner]
        else:
            candidate_ids = sorted([p.profile_id for p in matching_profiles])
            return ResolvedCommand(
                ecosystem=None,
                command=None,
                source="ambiguous",
                reason=f"Ambiguous runner profiles detected: {', '.join(candidate_ids)}",
                runner_profile_id=None,
                registry_version=reg_ver,
                also_matched=candidate_ids,
            )

    # Exactly one profile matched
    profile = matching_profiles[0]
    pid = profile.profile_id

    def _build_cmd(eco: str, cmd: List[str], src: str) -> ResolvedCommand:
        return ResolvedCommand(
            ecosystem=eco,
            command=cmd,
            source=src,
            runner_profile_id=pid,
            registry_version=reg_ver,
            also_matched=also_matched,
            ambiguity_resolved_by=ambiguity_resolved_by,
        )

    if pid == "pytest":
        pytest_source = "pytest"
        if (wt / "pytest.ini").exists():
            pytest_source = "pytest.ini"
        elif (wt / "pyproject.toml").exists():
            pytest_source = "pyproject.toml"
        else:
            pytest_source = "python_test_files"
        return _build_cmd("pytest", [sys.executable, "-m", "pytest", "-q"], pytest_source)

    if pid == "go-test":
        return _build_cmd("go", ["go", "test", "./..."], "go.mod")

    if pid == "cargo-test":
        return _build_cmd("cargo", ["cargo", "test"], "Cargo.toml")

    if pid == "maven-surefire":
        mvn_cmd = "mvn"
        if sys.platform == "win32" and (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        elif (wt / "mvnw").exists():
            mvn_cmd = "./mvnw"
        elif (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        return _build_cmd("maven", [mvn_cmd, "test"], "pom.xml")

    if pid == "gradle-test":
        gradle_cmd = "gradle"
        if sys.platform == "win32" and (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        elif (wt / "gradlew").exists():
            gradle_cmd = "./gradlew"
        elif (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        source = "build.gradle" if (wt / "build.gradle").exists() else "build.gradle.kts"
        return _build_cmd("gradle", [gradle_cmd, "test"], source)

    if pid == "terraform":
        return _build_cmd("terraform", ["terraform", "validate"], "terraform")

    if pid == "ansible":
        import shutil
        if shutil.which("ansible-lint"):
            return _build_cmd("ansible", ["ansible-lint"], "ansible")
        elif shutil.which("ansible-playbook"):
            playbooks = list(wt.glob("playbook*.yml")) + list(wt.glob("playbook*.yaml"))
            pb_arg = str(playbooks[0]) if playbooks else "."
            return _build_cmd("ansible", ["ansible-playbook", "--syntax-check", pb_arg], "ansible")
        else:
            return _build_cmd("ansible", ["ansible-lint"], "ansible")

    if pid == "helm":
        return _build_cmd("helm", ["helm", "lint", "."], "Chart.yaml")

    # Other runner profiles
    source_marker = profile.marker_files[0] if profile.marker_files else profile.profile_id
    return _build_cmd(profile.runner, list(profile.test_argv_template), source_marker)


def build_targeted_command(
    profile: Optional[RunnerProfile],
    base_command: List[str],
    targets: Sequence[Union[str, Path]],
    worktree_dir: Optional[Path] = None,
) -> Tuple[Optional[List[str]], str, TargetConfidence]:
    """Builds the targeted command for a runner profile.

    Returns:
        (targeted_argv, selection_rule, target_confidence)
        If targeted_argv is None, indicates the target cannot be represented for this runner,
        prompting fallback to declared full suite.
    """
    if not targets:
        return (list(base_command), "declared_full_suite", TargetConfidence.DECLARED_FULL_SUITE)

    if profile is None:
        return (None, "unsupported_target", TargetConfidence.HEURISTIC)

    targets_str = [str(t) for t in targets]
    pid = profile.profile_id
    caps = profile.target_capabilities

    # 1. pytest: ["file", "node_id"]
    if pid == "pytest":
        has_node_id = any("::" in t for t in targets_str)
        rule = "exact_node_id" if has_node_id else "exact_file"
        return ([*base_command, *targets_str], rule, TargetConfidence.EXACT)

    # 2. File-targeting profiles: bun-test, deno-test, mix-test, node-native-test, mocha, ruby-rake, vitest, jest
    if "file" in caps:
        # If any target has node_id separator "::" but runner does not support node_id
        if any("::" in t for t in targets_str) and "node_id" not in caps and "filter" not in caps:
            return (None, "unsupported_target", TargetConfidence.HEURISTIC)

        if pid == "ruby-rake":
            # rake test TEST=path/to/test.rb
            return ([*base_command, f"TEST={targets_str[0]}"], "exact_file", TargetConfidence.EXACT)

        # For npm/yarn/pnpm using package scripts:
        if base_command and base_command[0] in ("npm", "yarn", "pnpm"):
            if base_command[0] == "npm" and worktree_dir and ((worktree_dir / "bun.lock").exists() or (worktree_dir / "bun.lockb").exists()):
                return (["bun", "run", "test", *targets_str], "exact_file", TargetConfidence.EXACT)
            elif base_command[0] == "npm":
                return ([*base_command, "--", *targets_str], "exact_file", TargetConfidence.EXACT)
            else:
                return ([*base_command, *targets_str], "exact_file", TargetConfidence.EXACT)

        return ([*base_command, *targets_str], "exact_file", TargetConfidence.EXACT)

    # 3. go-test: ["package"]
    if pid == "go-test":
        # Check if targets are package targets
        valid_pkgs: List[str] = []
        for t in targets_str:
            # If target ends with .go, it's a bare file, not a package
            if t.endswith(".go"):
                return (None, "unsupported_target", TargetConfidence.HEURISTIC)
            normalized = t.replace("\\", "/")
            if not normalized.startswith("./") and normalized != ".":
                normalized = f"./{normalized}"
            valid_pkgs.append(normalized)

        # Replace ./... in base_command with target package(s)
        cmd = []
        replaced = False
        for arg in base_command:
            if arg == "./...":
                cmd.extend(valid_pkgs)
                replaced = True
            else:
                cmd.append(arg)
        if not replaced:
            cmd.extend(valid_pkgs)
        return (cmd, "exact_package", TargetConfidence.EXACT)

    # 4. cargo-test: ["package", "test_name"]
    if pid == "cargo-test":
        cmd = list(base_command)
        first_t = targets_str[0]
        # If target looks like a test file in tests/ e.g. tests/it.rs
        if first_t.startswith("tests/") or first_t.startswith("tests\\") or first_t.endswith(".rs"):
            test_stem = Path(first_t).stem
            cmd.extend(["--test", test_stem])
            return (cmd, "exact_task", TargetConfidence.EXACT)
        elif first_t.startswith("-p"):
            cmd.extend(first_t.split())
            return (cmd, "exact_package", TargetConfidence.EXACT)
        else:
            # Package or test target: cargo test -p <pkg>
            for t in targets_str:
                cmd.extend(["-p", t])
            return (cmd, "exact_package", TargetConfidence.EXACT)

    # 5. maven-surefire: ["class", "method"]
    if pid == "maven-surefire":
        classes = []
        for t in targets_str:
            if t.endswith(".java") or t.endswith(".kt"):
                classes.append(Path(t).stem)
            else:
                classes.append(t)
        class_spec = ",".join(classes)
        return ([*base_command, f"-Dtest={class_spec}"], "exact_class", TargetConfidence.EXACT)

    # 6. gradle-test: ["class"]
    if pid == "gradle-test":
        cmd = list(base_command)
        for t in targets_str:
            cls_name = Path(t).stem if (t.endswith(".java") or t.endswith(".kt") or t.endswith(".groovy")) else t
            cmd.extend(["--tests", cls_name])
        return (cmd, "exact_class", TargetConfidence.EXACT)

    # 7. dotnet-test: ["filter"]
    if pid == "dotnet-test":
        filter_parts = []
        for t in targets_str:
            if "~" in t or "=" in t:
                filter_parts.append(t)
            else:
                filter_parts.append(f"FullyQualifiedName~{t}")
        filter_expr = "|".join(filter_parts)
        return ([*base_command, "--filter", filter_expr], "exact_filter", TargetConfidence.EXACT)

    # 8. cmake-ctest: ["regex"]
    if pid == "cmake-ctest":
        # If target ends with source extension, ctest does not support file paths
        if any(Path(t).suffix.lower() in (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp") for t in targets_str):
            return (None, "unsupported_target", TargetConfidence.HEURISTIC)
        regex_expr = "|".join(targets_str)
        return ([*base_command, "-R", regex_expr], "exact_regex", TargetConfidence.EXACT)

    # 9. phpunit: ["filter"]
    if pid == "phpunit":
        filter_expr = "|".join(targets_str)
        return ([*base_command, "--filter", filter_expr], "exact_filter", TargetConfidence.EXACT)

    # 10. swiftpm-test: ["filter"]
    if pid == "swiftpm-test":
        filter_expr = "|".join(targets_str)
        return ([*base_command, "--filter", filter_expr], "exact_filter", TargetConfidence.EXACT)

    # Unsupported target capabilities
    return (None, "unsupported_target", TargetConfidence.HEURISTIC)


def get_zero_test_downgrade_markers(profile: Optional[RunnerProfile] = None) -> List[str]:
    """Returns the zero-test downgrade markers for a profile, supplemented with measured markers from real toolchains."""
    markers: List[str] = []
    if profile is not None:
        markers.extend(profile.zero_test_downgrade_markers)

    pid = profile.profile_id if profile else None
    runner_specific: List[str] = []
    if pid == "bun-test":
        runner_specific = [
            r"No tests found",
            r"matched 0 tests",
            r"0 pass",
            r"0 fail",
        ]
    elif pid == "pytest":
        runner_specific = [
            r"no tests ran",
            r"collected 0 items",
            r"NO_TESTS",
            r"\b0 selected\b",
            r"\d+\s+deselected\b",
        ]
    elif pid == "jest":
        runner_specific = [
            r"No tests found",
        ]
    elif pid == "vitest":
        runner_specific = [
            r"No test files found",
            r"no tests found",
        ]
    elif pid == "mocha":
        runner_specific = [
            r"0 passing",
        ]
    elif pid == "cargo-test":
        runner_specific = [
            r"running 0 tests",
            r"0 passed;\s*0 failed",
        ]
    elif pid == "node-native-test":
        runner_specific = [
            r"pass 0\b",
            r"fail 0\b",
            r"tests 0\b",
        ]
    elif pid == "go-test":
        runner_specific = [
            r"\[no test files\]",
            r"\[no tests to run\]",
        ]
    elif pid == "deno-test":
        runner_specific = [
            r"0 passed;\s*0 failed",
        ]
    elif pid == "maven-surefire":
        runner_specific = [
            r"Tests run:\s*0,\s*Failures:\s*0",
        ]
    elif pid == "gradle-test":
        runner_specific = [
            r"NO-SOURCE",
        ]
    elif pid == "dotnet-test":
        runner_specific = [
            r"Total tests:\s*0",
        ]
    elif pid == "cmake-ctest":
        runner_specific = [
            r"No tests were found",
        ]
    elif pid == "ruby-rake":
        runner_specific = [
            r"0 runs,\s*0 assertions",
        ]
    elif pid == "phpunit":
        runner_specific = [
            r"No tests executed",
        ]
    elif pid in ("swiftpm-test", "xcode-test"):
        runner_specific = [
            r"Executed 0 tests",
        ]
    elif pid == "mix-test":
        runner_specific = [
            r"0 failures",
            r"0 tests",
        ]
    else:
        runner_specific = [
            r"collected 0 items",
            r"no tests ran",
            r"No tests found",
            r"matched 0 tests",
            r"0 pass",
            r"running 0 tests",
            r"\[no test files\]",
            r"Total tests:\s*0",
            r"No tests were found",
            r"Executed 0 tests",
        ]

    for m in runner_specific:
        if m not in markers:
            markers.append(m)
    return markers


def is_zero_test_marker_match(
    profile: Optional[RunnerProfile],
    output: str,
    exit_code: Optional[int] = None,
) -> bool:
    """Checks if output contains any zero-test downgrade marker.
    
    Markers are strictly downgrade-only: they may turn a candidate FAIL into
    UNMEASURED(NO_TEST_EXECUTION_SIGNAL), but never upgrade anything.
    """
    if not output:
        return False

    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)
    markers = get_zero_test_downgrade_markers(profile)
    for marker in markers:
        try:
            if re.search(marker, clean, re.IGNORECASE | re.MULTILINE):
                return True
        except Exception:
            if marker.lower() in clean.lower():
                return True
    return False


def parse_test_counts_summary(
    profile: Optional[RunnerProfile],
    output: str,
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """Parses execution summary counts from runner output.
    
    Returns a dict with:
        passed: int
        failed: int
        skipped: int
        all_skipped: bool
        has_positive_pass: bool
        parsed: bool
    """
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output or "")
    pid = profile.profile_id if profile else None

    passed = 0
    failed = 0
    skipped = 0
    matched = False

    # 1. Pytest: 'X passed', 'X failed', 'X skipped', 'X error'
    m_pass = re.search(r"\b(\d+)\s+passed\b", clean, re.I)
    m_fail = re.search(r"\b(\d+)\s+failed\b", clean, re.I)
    m_err = re.search(r"\b(\d+)\s+error(?:s)?\b", clean, re.I)
    m_skip = re.search(r"\b(\d+)\s+skipped\b", clean, re.I)
    if m_pass or m_fail or m_err or m_skip:
        matched = True
        if m_pass:
            passed += int(m_pass.group(1))
        if m_fail:
            failed += int(m_fail.group(1))
        if m_err:
            failed += int(m_err.group(1))
        if m_skip:
            skipped += int(m_skip.group(1))

    # 2. Bun: 'X pass', 'X fail', 'X skip'
    if not matched:
        b_pass = re.search(r"^\s*(\d+)\s+pass\b", clean, re.MULTILINE | re.I)
        b_fail = re.search(r"^\s*(\d+)\s+fail\b", clean, re.MULTILINE | re.I)
        b_skip = re.search(r"^\s*(\d+)\s+skip\b", clean, re.MULTILINE | re.I)
        if b_pass or b_fail or b_skip:
            matched = True
            if b_pass:
                passed += int(b_pass.group(1))
            if b_fail:
                failed += int(b_fail.group(1))
            if b_skip:
                skipped += int(b_skip.group(1))

    # 3. Cargo: 'test result: ok. 1 passed; 0 failed; 0 ignored'
    if not matched:
        c_m = re.search(
            r"test result:\s*(?:ok|FAILED)\.\s*(\d+)\s+passed;\s*(\d+)\s+failed;\s*(\d+)\s+ignored",
            clean,
            re.I,
        )
        if c_m:
            matched = True
            passed = int(c_m.group(1))
            failed = int(c_m.group(2))
            skipped = int(c_m.group(3))

    # 4. Node native: 'ℹ pass 1', 'ℹ fail 0', 'ℹ skipped 0'
    if not matched:
        n_pass = re.search(r"\bpass\s+(\d+)\b", clean, re.I)
        n_fail = re.search(r"\bfail\s+(\d+)\b", clean, re.I)
        n_skip = re.search(r"\bskipped\s+(\d+)\b", clean, re.I)
        if n_pass or n_fail or n_skip:
            matched = True
            if n_pass:
                passed += int(n_pass.group(1))
            if n_fail:
                failed += int(n_fail.group(1))
            if n_skip:
                skipped += int(n_skip.group(1))

    # 5. Jest / Vitest: 'Tests: 1 passed, 1 total'
    if not matched:
        j_pass = re.search(r"Tests:\s*.*?(\d+)\s+passed", clean, re.I)
        j_fail = re.search(r"Tests:\s*.*?(\d+)\s+failed", clean, re.I)
        j_skip = re.search(r"Tests:\s*.*?(\d+)\s+skipped", clean, re.I)
        if j_pass or j_fail or j_skip:
            matched = True
            if j_pass:
                passed += int(j_pass.group(1))
            if j_fail:
                failed += int(j_fail.group(1))
            if j_skip:
                skipped += int(j_skip.group(1))

    # 6. Deno: '1 passed; 0 failed; 0 ignored'
    if not matched:
        d_m = re.search(r"(\d+)\s+passed;\s*(\d+)\s+failed;\s*(\d+)\s+ignored", clean, re.I)
        if d_m:
            matched = True
            passed = int(d_m.group(1))
            failed = int(d_m.group(2))
            skipped = int(d_m.group(3))

    # 7. Mocha: '1 passing', '0 failing'
    if not matched:
        m_pass = re.search(r"(\d+)\s+passing\b", clean, re.I)
        m_fail = re.search(r"(\d+)\s+failing\b", clean, re.I)
        if m_pass or m_fail:
            matched = True
            if m_pass:
                passed += int(m_pass.group(1))
            if m_fail:
                failed += int(m_fail.group(1))

    # 8. Maven: 'Tests run: 1, Failures: 0, Errors: 0, Skipped: 0'
    if not matched:
        mv_m = re.search(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)", clean, re.I)
        if mv_m:
            matched = True
            total = int(mv_m.group(1))
            failed = int(mv_m.group(2)) + int(mv_m.group(3))
            skipped = int(mv_m.group(4))
            passed = max(0, total - failed - skipped)

    # 9. DotNet: 'Passed: 1, Failed: 0, Skipped: 0'
    if not matched:
        dn_pass = re.search(r"Passed:\s*(\d+)", clean, re.I)
        dn_fail = re.search(r"Failed:\s*(\d+)", clean, re.I)
        dn_skip = re.search(r"Skipped:\s*(\d+)", clean, re.I)
        if dn_pass or dn_fail or dn_skip:
            matched = True
            if dn_pass:
                passed += int(dn_pass.group(1))
            if dn_fail:
                failed += int(dn_fail.group(1))
            if dn_skip:
                skipped += int(dn_skip.group(1))

    # 10. Go: 'ok  package  0.01s' or '--- PASS: TestName'
    if not matched and pid == "go-test":
        if re.search(r"^ok\s+\S+", clean, re.MULTILINE) or "--- PASS:" in clean:
            matched = True
            passed = len(re.findall(r"--- PASS:", clean)) or 1
        elif re.search(r"^FAIL\s+\S+", clean, re.MULTILINE) or "--- FAIL:" in clean:
            matched = True
            failed = len(re.findall(r"--- FAIL:", clean)) or 1

    skip_indicators = (
        "(skip) skip" in clean or
        re.search(r"\bignored\b", clean, re.I) is not None or
        re.search(r"#\s*SKIP\b", clean) is not None or
        re.search(r"^\s*s\s+\[100%\]", clean, re.MULTILINE) is not None
    )
    all_skipped = False
    if passed == 0 and failed == 0:
        if skipped > 0 or skip_indicators:
            all_skipped = True

    has_positive_pass = bool(matched and passed > 0 and not all_skipped)

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "all_skipped": all_skipped,
        "has_positive_pass": has_positive_pass,
        "parsed": matched,
    }


