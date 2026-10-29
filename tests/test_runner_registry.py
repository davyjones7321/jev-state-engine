"""tests/test_runner_registry.py

Tests for Phase 2A runner-profile registry, Pydantic schema validation,
pure marker detection, ambiguity handling, regex safety against ReDoS,
command identity, and cache key versioning.
Includes Phase 2 Fixes: precedence tie-breaking for old-cascade polyglot layouts,
also_matched and ambiguity_resolved_by tracking in command identity and MechanicalEvidence,
differential resolution testing over a table of layouts, and verification of
the 7 intentional new-coverage cases.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from jev.models import MechStatus, TestOutcome
from jev.runner_registry import (
    RunnerProfile,
    RunnerRegistry,
    clear_registry_cache,
    is_version_supported,
    load_registry,
    validate_regex_pattern,
)
from jev.workspace import ResolvedTestCommand, Workspace, resolve_test_command


# ==============================================================================
# 1. Schema Validation (Pydantic) & Registry Loading
# ==============================================================================

def test_registry_loads_and_validates_schema():
    """Default registry JSON loads, validates against Pydantic schema, and has version 1.0.0."""
    clear_registry_cache()
    registry = load_registry()
    assert isinstance(registry, RunnerRegistry)
    assert registry.registry_version == "1.0.0"
    assert len(registry.profiles) == 21

    # Check key profile IDs
    profile_ids = [p.profile_id for p in registry.profiles]
    assert "pytest" in profile_ids
    assert "cargo-test" in profile_ids
    assert "go-test" in profile_ids
    assert "node-native-test" in profile_ids
    assert "xcode-test" in profile_ids
    assert "terraform" in profile_ids
    assert "ansible" in profile_ids
    assert "helm" in profile_ids


def test_schema_rejects_extra_fields():
    """Profiles reject unknown/extra fields (extra='forbid')."""
    valid_data = {
        "profile_id": "custom",
        "registry_version": "1.0.0",
        "language": "python",
        "runner": "pytest",
        "platforms": ["linux"],
        "marker_files": ["pytest.ini"],
        "executable_rule": "python",
        "test_argv_template": ["pytest"],
        "unrecognized_field": "disallowed",
    }
    with pytest.raises(ValidationError):
        RunnerProfile.model_validate(valid_data)


def test_schema_rejects_missing_required_fields():
    """Profiles reject missing required fields."""
    incomplete_data = {
        "profile_id": "custom",
        "registry_version": "1.0.0",
        # missing language, runner, platforms, etc.
    }
    with pytest.raises(ValidationError):
        RunnerProfile.model_validate(incomplete_data)


# ==============================================================================
# 2. Regex Validation (Length Cap <= 200, Nested Quantifiers, ReDoS Safety)
# ==============================================================================

def test_regex_pattern_valid():
    """Valid regex patterns are accepted."""
    assert validate_regex_pattern(r"^FAILED\s+([^:]+)::(\S+)") == r"^FAILED\s+([^:]+)::(\S+)"
    assert validate_regex_pattern(r"No tests found, exiting with code 1") == r"No tests found, exiting with code 1"
    assert validate_regex_pattern(r"(\d+)\s+failed") == r"(\d+)\s+failed"


def test_regex_pattern_exceeds_length_cap():
    """Patterns exceeding 200 characters are rejected."""
    long_pattern = "a" * 201
    with pytest.raises(ValueError, match="exceeds 200 characters"):
        validate_regex_pattern(long_pattern)


def test_regex_pattern_rejects_nested_quantifiers():
    """Patterns with nested or consecutive quantifiers are rejected."""
    nested_patterns = [
        r"(a+)+",
        r"(a*)*",
        r"([0-9]+)+",
        r"(a|b+)+",
        r"(a+){1,3}",
        r"(.*)*",
        r"a++",
        r"a*+",
        r"a{2,4}+",
    ]
    for np in nested_patterns:
        with pytest.raises(ValueError, match="Nested|Consecutive"):
            validate_regex_pattern(np)


def test_regex_pattern_rejects_invalid_syntax():
    """Invalid regex syntax is rejected."""
    with pytest.raises(ValueError, match="Invalid regex pattern"):
        validate_regex_pattern(r"[unclosed-bracket")


def test_pathological_input_safety():
    """Test that all regex patterns in the registry complete against pathological inputs in < 100ms."""
    clear_registry_cache()
    registry = load_registry(reload=True)
    pathological_string = "a" * 50000 + "!"

    for profile in registry.profiles:
        all_patterns = profile.zero_test_downgrade_markers + profile.failure_identity_patterns
        for pat in all_patterns:
            compiled = re.compile(pat, re.MULTILINE)
            start = time.perf_counter()
            _ = compiled.search(pathological_string)
            elapsed = time.perf_counter() - start
            assert elapsed < 0.1, f"Pattern {pat} in {profile.profile_id} took too long: {elapsed:.4f}s"


# ==============================================================================
# 3. Pure Detection Runs NO Subprocess
# ==============================================================================

def test_detection_runs_no_subprocess(tmp_path):
    """Manifest and marker detection runs zero subprocesses."""
    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")

    with patch("subprocess.run") as mock_run, \
         patch("subprocess.Popen") as mock_popen, \
         patch("subprocess.call") as mock_call, \
         patch("subprocess.check_output") as mock_check_output:
        res = resolve_test_command(tmp_path)
        assert res.command == ["go", "test", "./..."]
        assert res.runner_profile_id == "go-test"

        assert mock_run.call_count == 0
        assert mock_popen.call_count == 0
        assert mock_call.call_count == 0
        assert mock_check_output.call_count == 0


# ==============================================================================
# 4. Version Predicates (Stored and Evaluated Only When Supplied)
# ==============================================================================

def test_version_predicates_evaluation():
    """Version predicates are ignored during detection and only evaluated when tool version is supplied."""
    profile = RunnerProfile(
        profile_id="test-runner",
        registry_version="1.0.0",
        language="python",
        runner="pytest",
        platforms=["linux"],
        marker_files=["pytest.ini"],
        min_version="6.0.0",
        max_version="8.5.0",
        executable_rule="python",
        test_argv_template=["pytest"],
    )

    # When tool_version is None (pure detection): supported!
    assert is_version_supported(profile, None) is True

    # When tool_version is supplied:
    assert is_version_supported(profile, "5.4.1") is False  # below min
    assert is_version_supported(profile, "6.0.0") is True   # exact min
    assert is_version_supported(profile, "7.2.1") is True   # in range
    assert is_version_supported(profile, "8.5.0") is True   # exact max
    assert is_version_supported(profile, "9.0.0") is False  # above max


# ==============================================================================
# 5. One Fixture Per Row: All 21 Profiles Resolved
# ==============================================================================

@pytest.mark.parametrize(
    "profile_id, marker_filename, expected_cmd_prefix",
    [
        ("pytest", "pytest.ini", ["-m", "pytest", "-q"]),
        ("node-native-test", "node_test.config.js", ["node", "--test"]),
        ("jest", "jest.config.js", ["npx", "jest"]),
        ("mocha", ".mocharc.json", ["npx", "mocha"]),
        ("vitest", "vitest.config.ts", ["npx", "vitest", "run"]),
        ("bun-test", "bunfig.toml", ["bun", "test"]),
        ("deno-test", "deno.json", ["deno", "test"]),
        ("go-test", "go.mod", ["go", "test", "./..."]),
        ("cargo-test", "Cargo.toml", ["cargo", "test"]),
        ("maven-surefire", "pom.xml", ["test"]),
        ("gradle-test", "build.gradle", ["test"]),
        ("dotnet-test", "app.csproj", ["dotnet", "test"]),
        ("cmake-ctest", "CMakeLists.txt", ["ctest", "--output-on-failure"]),
        ("ruby-rake", "Rakefile", ["bundle", "exec", "rake", "test"]),
        ("phpunit", "phpunit.xml", ["vendor/bin/phpunit"]),
        ("swiftpm-test", "Package.swift", ["swift", "test"]),
        ("mix-test", "mix.exs", ["mix", "test"]),
        ("xcode-test", "app.xcodeproj", ["xcodebuild", "test"]),
        ("terraform", "main.tf", ["terraform", "validate"]),
        ("ansible", "ansible.cfg", ["ansible-lint"]),
        ("helm", "Chart.yaml", ["helm", "lint", "."]),
    ],
)
def test_one_fixture_per_row_detection(tmp_path, profile_id, marker_filename, expected_cmd_prefix):
    """Each of the 21 profiles is successfully detected from its marker file."""
    repo = tmp_path / profile_id
    repo.mkdir()
    (repo / marker_filename).write_text("# marker\n", encoding="utf-8")

    res = resolve_test_command(repo)
    assert res.runner_profile_id == profile_id
    assert res.command is not None
    assert res.registry_version == "1.0.0"

    # Verify command contains expected tokens
    for token in expected_cmd_prefix:
        assert any(token in arg for arg in res.command)


# ==============================================================================
# 6. Ambiguity Handling & Precedence Tie-Breaking
# ==============================================================================

def test_precedence_tie_breaking_for_four_layouts(tmp_path):
    """For the four layouts the old cascade resolved, precedence breaks ties preserving old behavior."""
    # 1. go.mod + Cargo.toml -> go-test wins (precedence 2 < 3)
    repo1 = tmp_path / "poly_go_cargo"
    repo1.mkdir()
    (repo1 / "go.mod").write_text("module test\n", encoding="utf-8")
    (repo1 / "Cargo.toml").write_text("[package]\nname = \"test\"\n", encoding="utf-8")

    res1 = resolve_test_command(repo1)
    assert res1.runner_profile_id == "go-test"
    assert res1.command == ["go", "test", "./..."]
    assert res1.also_matched == ["cargo-test"]
    assert res1.ambiguity_resolved_by == "precedence"

    # 2. pyproject.toml(pytest) + go.mod -> go-test wins (precedence 2 < 6)
    repo2 = tmp_path / "poly_pytest_go"
    repo2.mkdir()
    (repo2 / "go.mod").write_text("module test\n", encoding="utf-8")
    (repo2 / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    res2 = resolve_test_command(repo2)
    assert res2.runner_profile_id == "go-test"
    assert res2.command == ["go", "test", "./..."]
    assert res2.also_matched == ["pytest"]
    assert res2.ambiguity_resolved_by == "precedence"

    # 3. pom.xml + build.gradle -> maven-surefire wins (precedence 4 < 5)
    repo3 = tmp_path / "poly_mvn_gradle"
    repo3.mkdir()
    (repo3 / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    (repo3 / "build.gradle").write_text("// gradle\n", encoding="utf-8")

    res3 = resolve_test_command(repo3)
    assert res3.runner_profile_id == "maven-surefire"
    assert res3.command is not None and "test" in res3.command
    assert res3.also_matched == ["gradle-test"]
    assert res3.ambiguity_resolved_by == "precedence"

    # 4. main.tf + pytest markers -> pytest wins (precedence 6 < 7)
    repo4 = tmp_path / "poly_tf_pytest"
    repo4.mkdir()
    (repo4 / "main.tf").write_text("resource \"null_resource\" \"x\" {}\n", encoding="utf-8")
    (repo4 / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    res4 = resolve_test_command(repo4)
    assert res4.runner_profile_id == "pytest"
    assert res4.command == [sys.executable, "-m", "pytest", "-q"]
    assert res4.also_matched == ["terraform"]
    assert res4.ambiguity_resolved_by == "precedence"


def test_evidence_records_precedence_tie_breaking(tmp_path):
    """When precedence breaks a tie, also_matched and ambiguity_resolved_by appear in command_identity and evidence."""
    repo = tmp_path / "tie_break_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)

    # Setup layout: main.tf + pytest.ini (pytest wins over terraform)
    (repo / "main.tf").write_text("resource \"null_resource\" \"x\" {}\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    subgoal = MagicMock()
    subgoal.id = "sg-tie"
    subgoal.expects_tests = True

    ev = ws.evaluate_tests(subgoal, test_targets=["tests/test_sample.py"])
    assert ev is not None
    assert ev.mech_status == MechStatus.PASS

    # Verify command_identity and evidence records precedence tie-breaking
    assert ev.command_identity["profile_id"] == "pytest"
    assert ev.command_identity["also_matched"] == ["terraform"]
    assert ev.command_identity["ambiguity_resolved_by"] == "precedence"


def test_ambiguity_returns_no_command_for_unresolved_new_profiles(tmp_path):
    """When multiple runner profiles match outside old cascade (precedence 100), return NO_COMMAND with candidate IDs."""
    # 1. vitest + jest
    repo1 = tmp_path / "ambig_js"
    repo1.mkdir()
    (repo1 / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (repo1 / "jest.config.js").write_text("module.exports = {}\n", encoding="utf-8")

    res1 = resolve_test_command(repo1)
    assert res1.command is None
    assert res1.source == "ambiguous"
    assert "Ambiguous runner profiles detected:" in res1.reason
    assert "jest" in res1.reason
    assert "vitest" in res1.reason

    # 2. go.mod + jest.config.js (precedence 2 and 100)
    repo2 = tmp_path / "ambig_go_jest"
    repo2.mkdir()
    (repo2 / "go.mod").write_text("module test\n", encoding="utf-8")
    (repo2 / "jest.config.js").write_text("module.exports = {}\n", encoding="utf-8")

    res2 = resolve_test_command(repo2)
    assert res2.command is None
    assert res2.source == "ambiguous"
    assert "go-test" in res2.reason
    assert "jest" in res2.reason


def test_zero_matches_returns_no_command(tmp_path):
    """When zero profiles match, return NO_COMMAND."""
    res = resolve_test_command(tmp_path)
    assert res.command is None
    assert res.ecosystem is None
    assert res.source == "none"
    assert res.reason == "No test framework or test files detected"


# ==============================================================================
# 7. Differential Testing Over Layouts Table
# ==============================================================================

def _old_cascade_resolver(wt: Path) -> Tuple[Optional[str], Optional[List[str]]]:
    """Reference implementation of the old cascade resolver for differential testing."""
    pkg_json_path = wt / "package.json"
    if pkg_json_path.exists():
        try:
            pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pkg_data, dict):
                scripts = pkg_data.get("scripts")
                if isinstance(scripts, dict) and "test" in scripts:
                    if (wt / "yarn.lock").exists():
                        return "yarn", ["yarn", "test"]
                    elif (wt / "pnpm-lock.yaml").exists():
                        return "pnpm", ["pnpm", "test"]
                    else:
                        return "npm", ["npm", "test"]
        except Exception:
            return "package_json", None

    if (wt / "go.mod").exists():
        return "go", ["go", "test", "./..."]

    if (wt / "Cargo.toml").exists():
        return "cargo", ["cargo", "test"]

    if (wt / "pom.xml").exists():
        mvn_cmd = "mvn"
        if sys.platform == "win32" and (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        elif (wt / "mvnw").exists():
            mvn_cmd = "./mvnw"
        elif (wt / "mvnw.cmd").exists():
            mvn_cmd = str(wt / "mvnw.cmd")
        return "maven", [mvn_cmd, "test"]

    if (wt / "build.gradle").exists() or (wt / "build.gradle.kts").exists():
        gradle_cmd = "gradle"
        if sys.platform == "win32" and (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        elif (wt / "gradlew").exists():
            gradle_cmd = "./gradlew"
        elif (wt / "gradlew.bat").exists():
            gradle_cmd = str(wt / "gradlew.bat")
        return "gradle", [gradle_cmd, "test"]

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
        return "pytest", [sys.executable, "-m", "pytest", "-q"]

    has_tf = False
    try:
        has_tf = any(wt.glob("*.tf")) or any(p for p in wt.rglob("*.tf") if ".terraform" not in p.parts)
    except Exception:
        pass
    if has_tf:
        return "terraform", ["terraform", "validate"]

    has_ansible = (
        (wt / "ansible.cfg").exists()
        or (wt / "playbooks").is_dir()
        or any(wt.glob("playbook*.yml"))
        or any(wt.glob("playbook*.yaml"))
    )
    if has_ansible:
        return "ansible", ["ansible-lint"]

    if (wt / "Chart.yaml").exists():
        return "helm", ["helm", "lint", "."],

    return None, None


@pytest.mark.parametrize(
    "layout_name, file_map",
    [
        # Single markers
        ("single_go", {"go.mod": "module test\n"}),
        ("single_cargo", {"Cargo.toml": "[package]\nname = \"test\"\n"}),
        ("single_maven", {"pom.xml": "<project></project>\n"}),
        ("single_gradle", {"build.gradle": "// gradle\n"}),
        ("single_pytest", {"pytest.ini": "[pytest]\n"}),
        ("single_terraform", {"main.tf": "resource \"x\" \"y\" {}\n"}),
        ("single_ansible", {"ansible.cfg": "[defaults]\n"}),
        ("single_helm", {"Chart.yaml": "apiVersion: v2\nname: test\nversion: 0.1.0\n"}),

        # Package.json variants
        ("pkg_with_test_script", {"package.json": json.dumps({"scripts": {"test": "npm test"}})}),
        ("pkg_without_test_script", {"package.json": json.dumps({"name": "only-lib"})}),
        ("go_plus_pkg_with_test", {
            "go.mod": "module test\n",
            "package.json": json.dumps({"scripts": {"test": "npm test"}}),
        }),
        ("go_plus_pkg_without_test", {
            "go.mod": "module test\n",
            "package.json": json.dumps({"name": "only-lib"}),
        }),
        ("python_plus_pkg_with_test", {
            "pytest.ini": "[pytest]\n",
            "package.json": json.dumps({"scripts": {"test": "npm test"}}),
        }),
        ("python_plus_pkg_without_test", {
            "pytest.ini": "[pytest]\n",
            "package.json": json.dumps({"name": "only-lib"}),
        }),

        # Polyglot pairs (the four layouts resolved by old cascade)
        ("poly_go_cargo", {
            "go.mod": "module test\n",
            "Cargo.toml": "[package]\nname = \"test\"\n",
        }),
        ("poly_pytest_go", {
            "go.mod": "module test\n",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
        }),
        ("poly_mvn_gradle", {
            "pom.xml": "<project></project>\n",
            "build.gradle": "// gradle\n",
        }),
        ("poly_tf_pytest", {
            "main.tf": "resource \"x\" \"y\" {}\n",
            "pytest.ini": "[pytest]\n",
        }),
    ],
)
def test_differential_resolution_across_layouts_table(tmp_path, layout_name, file_map):
    """Differential test over table of layouts: registry returns the same command as old cascade resolver."""
    repo = tmp_path / layout_name
    repo.mkdir()
    for filename, content in file_map.items():
        (repo / filename).write_text(content, encoding="utf-8")

    old_eco, old_cmd = _old_cascade_resolver(repo)
    new_res = resolve_test_command(repo)

    assert new_res.command == old_cmd, (
        f"Layout '{layout_name}': command mismatch. Old: {old_cmd}, New: {new_res.command}"
    )
    if old_eco is not None:
        assert new_res.ecosystem == old_eco


# ==============================================================================
# 8. Seven Intentional New-Coverage Cases
# ==============================================================================

@pytest.mark.parametrize(
    "case_name, marker_file, expected_profile_id, expected_cmd_token",
    [
        ("jest config only", "jest.config.js", "jest", "jest"),
        ("vitest config only", "vitest.config.ts", "vitest", "vitest"),
        (".NET", "App.csproj", "dotnet-test", "dotnet"),
        ("Elixir", "mix.exs", "mix-test", "mix"),
        ("Ruby", "Rakefile", "ruby-rake", "rake"),
        ("PHP", "phpunit.xml", "phpunit", "phpunit"),
        ("Deno", "deno.json", "deno-test", "deno"),
    ],
)
def test_seven_intentional_new_coverage_cases(tmp_path, case_name, marker_file, expected_profile_id, expected_cmd_token):
    """The 7 intentional new-coverage cases are resolved by the registry while old cascade returned None."""
    repo = tmp_path / f"new_{expected_profile_id}"
    repo.mkdir()
    (repo / marker_file).write_text("# marker\n", encoding="utf-8")

    # Old resolver returned None
    old_eco, old_cmd = _old_cascade_resolver(repo)
    assert old_cmd is None, f"Old resolver unexpectedly matched {case_name}"

    # New resolver resolves to designated profile
    new_res = resolve_test_command(repo)
    assert new_res.runner_profile_id == expected_profile_id
    assert new_res.command is not None
    assert any(expected_cmd_token in arg for arg in new_res.command)


# ==============================================================================
# 9. Package.json Script Precedence & Profile ID Extraction
# ==============================================================================

def test_package_json_precedence_over_other_markers(tmp_path):
    """package.json test script takes precedence over other markers (e.g. pytest.ini)."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    res = resolve_test_command(tmp_path)
    assert res.ecosystem == "npm"
    assert res.command == ["npm", "test"]
    assert res.source == "package.json"
    assert res.runner_profile_id == "jest"
    assert res.reason == "jest"


@pytest.mark.parametrize(
    "script_content, expected_profile_id",
    [
        ("bun test", "bun-test"),
        ("vitest run", "vitest"),
        ("jest --coverage", "jest"),
        ("mocha tests/", "mocha"),
        ("pytest", "pytest"),
        ("node --test", "node-native-test"),
        ("deno test", "deno-test"),
        ("go test ./...", "go-test"),
        ("cargo test", "cargo-test"),
        ("bash run_tests.sh", "node-script-unknown"),
    ],
)
def test_derive_runner_profile_id_from_package_json(tmp_path, script_content, expected_profile_id):
    """package.json test script text is inspected to derive runner_profile_id."""
    repo = tmp_path / f"repo_{expected_profile_id}"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": script_content}}), encoding="utf-8")

    res = resolve_test_command(repo)
    assert res.runner_profile_id == expected_profile_id
    assert res.reason == script_content


# ==============================================================================
# 10. Command Identity and Base Cache Invalidation on Version Bump
# ==============================================================================

def _create_git_repo(tmp_path):
    repo = tmp_path / "git_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_1.py").write_text("def test_1(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_command_identity_includes_registry_version_and_profile_id(tmp_path):
    """Command identity contains registry_version and runner_profile_id."""
    repo = _create_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo)

    subgoal = MagicMock()
    subgoal.id = "sg-1"
    subgoal.expects_tests = True

    ev = ws.evaluate_tests(subgoal, test_targets=["tests/test_1.py"])
    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.command_identity["registry_version"] == "1.0.0"
    assert ev.command_identity["profile_id"] == "pytest"


def test_registry_version_bump_causes_cache_miss(tmp_path):
    """A change in registry_version invalidates the base-test cache (cache miss)."""
    repo = _create_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo)

    subgoal = MagicMock()
    subgoal.id = "sg-cache"
    subgoal.expects_tests = True

    # Make candidate fail so evaluate_tests invokes base run and caches it
    (repo / "tests" / "test_1.py").write_text("def test_1(): assert False\n", encoding="utf-8")

    # Pre-populate base cache under version 1.0.0
    ev1 = ws.evaluate_tests(subgoal, test_targets=["tests/test_1.py"], attempt=1)
    assert len(ws._base_test_cache) == 1

    # Extract original cache key
    orig_cache_key = list(ws._base_test_cache.keys())[0]
    assert orig_cache_key[-1] == "1.0.0"  # Last element is registry_version

    # Now simulate a bump in registry version to 2.0.0
    mock_reg = MagicMock()
    mock_reg.registry_version = "2.0.0"
    mock_res = ResolvedTestCommand(
        ecosystem="pytest",
        command=[sys.executable, "-m", "pytest", "-q"],
        source="pytest.ini",
        runner_profile_id="pytest",
        registry_version="2.0.0",
    )

    with patch("jev.runner_registry.load_registry", return_value=mock_reg), \
         patch("jev.workspace.resolve_test_command", return_value=mock_res):
        ev2 = ws.evaluate_tests(subgoal, test_targets=["tests/test_1.py"], attempt=2)
        # Because registry_version changed, cache missed and a new cache key was added!
        assert len(ws._base_test_cache) == 2
        new_cache_key = [k for k in ws._base_test_cache.keys() if k != orig_cache_key][0]
        assert new_cache_key[-1] == "2.0.0"
