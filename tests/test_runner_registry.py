"""tests/test_runner_registry.py

Tests for Phase 2A runner-profile registry, Pydantic schema validation,
pure marker detection, ambiguity handling, regex safety against ReDoS,
command identity, and cache key versioning.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
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
# 6. Ambiguity Handling
# ==============================================================================

def test_ambiguity_returns_no_command_with_candidate_ids(tmp_path):
    """When multiple runner profiles match without precedence, return NO_COMMAND with candidates listed."""
    # Both go.mod and Cargo.toml present
    (tmp_path / "go.mod").write_text("module test\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"test\"\n", encoding="utf-8")

    res = resolve_test_command(tmp_path)
    assert res.command is None
    assert res.ecosystem is None
    assert res.source == "ambiguous"
    assert "Ambiguous runner profiles detected:" in res.reason
    assert "cargo-test" in res.reason
    assert "go-test" in res.reason


def test_ambiguity_js_runners(tmp_path):
    """Ambiguity between vitest and jest without package.json returns NO_COMMAND."""
    (tmp_path / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "jest.config.js").write_text("module.exports = {}\n", encoding="utf-8")

    res = resolve_test_command(tmp_path)
    assert res.command is None
    assert res.source == "ambiguous"
    assert "jest" in res.reason
    assert "vitest" in res.reason


def test_zero_matches_returns_no_command(tmp_path):
    """When zero profiles match, return NO_COMMAND."""
    res = resolve_test_command(tmp_path)
    assert res.command is None
    assert res.ecosystem is None
    assert res.source == "none"
    assert res.reason == "No test framework or test files detected"


# ==============================================================================
# 7. Package.json Script Precedence & Profile ID Extraction
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
# 8. Command Identity and Base Cache Invalidation on Version Bump
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
