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
