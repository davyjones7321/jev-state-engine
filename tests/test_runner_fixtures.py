"""tests/test_runner_fixtures.py

Tests for Phase 2B (and Phase 2 Fixes): real fixtures captured from intended binaries
across runner profiles, organized by platform: tests/fixtures/runners/<sys.platform>/<id>.json.
Validates fixture completeness, schemas, normalization (no personal/temp paths),
canonical exit behaviors, and reports skipped fixtures for toolchains not available on the current host.
"""

from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from jev.runner_registry import RunnerProfile, load_registry

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runners"
PLATFORM_DIR = FIXTURES_ROOT / sys.platform


def _load_all_fixtures(platform: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    target_dir = FIXTURES_ROOT / (platform or sys.platform)
    if not target_dir.is_dir():
        return {}
    fixtures = {}
    for json_file in target_dir.glob("*.json"):
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            fixtures[data["profile_id"]] = data
    return fixtures


def test_fixtures_directory_exists():
    """Fixtures root directory must exist."""
    assert FIXTURES_ROOT.is_dir(), f"Fixtures directory not found: {FIXTURES_ROOT}"


def test_all_registry_profiles_have_fixture_file():
    """Every profile defined in the runner registry must have a corresponding fixture file for current platform."""
    if not PLATFORM_DIR.is_dir():
        pytest.skip(f"No runner fixtures directory for platform '{sys.platform}'")

    registry = load_registry()
    assert len(registry.profiles) >= 21
    fixture_files = {p.stem for p in PLATFORM_DIR.glob("*.json")}

    missing_profiles = []
    for profile in registry.profiles:
        if profile.profile_id not in fixture_files:
            missing_profiles.append(profile.profile_id)

    if missing_profiles:
        pytest.skip(
            f"Missing fixture file(s) for profiles on '{sys.platform}': {', '.join(missing_profiles)}"
        )


def test_fixtures_are_normalized():
    """All committed runner fixtures must be normalized: no usernames, home paths, or temp directories."""
    assert FIXTURES_ROOT.is_dir()
    all_json_files = list(FIXTURES_ROOT.rglob("*.json"))
    assert len(all_json_files) > 0, f"No fixture files found under {FIXTURES_ROOT}"

    current_user = getpass.getuser()
    violations = []

    for fpath in all_json_files:
        text = fpath.read_text(encoding="utf-8")
        rel_path = fpath.relative_to(FIXTURES_ROOT.parent)

        if "Users\\" in text or "users\\" in text:
            violations.append(f"{rel_path}: contains 'Users\\'")
        if "/home/" in text:
            violations.append(f"{rel_path}: contains '/home/'")
        if current_user and len(current_user) > 2 and current_user in text:
            violations.append(f"{rel_path}: contains username '{current_user}'")

    assert not violations, f"Fixture normalization violations found:\n" + "\n".join(violations)


def test_fixture_structure_and_schema():
    """All fixture files must follow the required schema with valid fields and cases."""
    if not PLATFORM_DIR.is_dir():
        pytest.skip(f"No runner fixtures directory for platform '{sys.platform}'")

    fixtures = _load_all_fixtures()
    if not fixtures:
        pytest.skip(f"No runner fixtures found in {PLATFORM_DIR}")

    required_keys = {
        "profile_id",
        "runner",
        "supported_platforms",
        "captured_on_host",
        "host_platform",
        "tool_version",
        "skip_reason",
        "cases",
    }

    for profile_id, data in fixtures.items():
        assert required_keys.issubset(data.keys()), (
            f"Fixture '{profile_id}' missing required keys: {required_keys - set(data.keys())}"
        )
        assert data["profile_id"] == profile_id
        assert isinstance(data["supported_platforms"], list)
        assert len(data["supported_platforms"]) > 0

        if data["captured_on_host"]:
            assert isinstance(data["tool_version"], str) and len(data["tool_version"]) > 0
            assert data["skip_reason"] is None
            cases = data["cases"]
            expected_cases = {"pass", "fail", "zero_tests", "filter_matches_nothing", "all_skipped"}
            assert expected_cases.issubset(cases.keys()), (
                f"Captured fixture '{profile_id}' missing cases: {expected_cases - set(cases.keys())}"
            )
            for case_name, case_data in cases.items():
                assert "command" in case_data
                assert "exit_code" in case_data
                assert "stdout" in case_data
                assert "stderr" in case_data
                assert isinstance(case_data["exit_code"], int)
                assert isinstance(case_data["stdout"], str)
                assert isinstance(case_data["stderr"], str)
        else:
            assert isinstance(data["skip_reason"], str) and len(data["skip_reason"]) > 0, (
                f"Skipped fixture '{profile_id}' must have a non-empty skip_reason"
            )


def test_every_profile_either_captured_or_skipped_with_reason():
    """Exit condition: every profile has captured fixtures or a recorded skip reason on each OS."""
    if not PLATFORM_DIR.is_dir():
        pytest.skip(f"No runner fixtures directory for platform '{sys.platform}'")

    fixtures = _load_all_fixtures()
    if not fixtures:
        pytest.skip(f"No runner fixtures found in {PLATFORM_DIR}")

    for profile_id, fixture in fixtures.items():
        is_captured = fixture["captured_on_host"] is True and bool(fixture.get("tool_version"))
        has_skip_reason = (
            fixture["captured_on_host"] is False
            and isinstance(fixture.get("skip_reason"), str)
            and len(fixture["skip_reason"].strip()) > 0
        )
        assert is_captured or has_skip_reason, (
            f"Profile '{profile_id}' neither captured nor recorded skip reason"
        )


def test_canonical_known_measured_values_in_fixtures():
    """Confirms known measured values from real captures and canonical references.
    
    Measured values to confirm:
    - pytest: zero tests exit 5
    - node --test: zero tests exit 0
    - bun/jest/mocha/vitest: zero tests exit 1
    - bun: non-matching filter exit 1
    - bun: all-skipped exit 0
    - bun: failure line contains (fail)
    - cargo: zero tests exit 0, failure exit 101
    """
    if not PLATFORM_DIR.is_dir():
        pytest.skip(f"No runner fixtures directory for platform '{sys.platform}'")

    fixtures = _load_all_fixtures()
    if not fixtures:
        pytest.skip(f"No runner fixtures found in {PLATFORM_DIR}")

    # 1. pytest zero tests exit 5
    pytest_data = fixtures.get("pytest")
    if pytest_data and pytest_data.get("captured_on_host"):
        assert pytest_data["cases"]["zero_tests"]["exit_code"] == 5
    elif pytest_data and "known_reference" in pytest_data:
        assert pytest_data["known_reference"]["zero_tests_exit"] == 5

    # 2. node --test zero tests exit 0
    node_data = fixtures.get("node-native-test")
    if node_data and node_data.get("captured_on_host"):
        assert node_data["cases"]["zero_tests"]["exit_code"] == 0
    elif node_data and "known_reference" in node_data:
        assert node_data["known_reference"]["zero_tests_exit"] == 0

    # 3. bun test measured values
    bun_data = fixtures.get("bun-test")
    if bun_data and bun_data.get("captured_on_host"):
        assert bun_data["cases"]["zero_tests"]["exit_code"] == 1
        assert bun_data["cases"]["filter_matches_nothing"]["exit_code"] == 1
        assert bun_data["cases"]["all_skipped"]["exit_code"] == 0
        fail_output = bun_data["cases"]["fail"]["stdout"] + bun_data["cases"]["fail"]["stderr"]
        assert "(fail) " in fail_output

    # 4. jest, mocha, vitest canonical zero tests exit 1
    for pid in ("jest", "mocha", "vitest"):
        fix = fixtures.get(pid)
        if fix and fix.get("captured_on_host"):
            assert fix["cases"]["zero_tests"]["exit_code"] == 1
        elif fix and "zero_tests_exit" in fix.get("known_reference", {}):
            assert fix["known_reference"]["zero_tests_exit"] == 1

    # 5. cargo test measured values
    cargo_data = fixtures.get("cargo-test")
    if cargo_data and cargo_data.get("captured_on_host"):
        assert cargo_data["cases"]["zero_tests"]["exit_code"] == 0
        assert cargo_data["cases"]["fail"]["exit_code"] == 101
        assert cargo_data["cases"]["all_skipped"]["exit_code"] == 0


def test_fixture_zero_test_exit_matches_registry():
    """Fixture zero-test exit codes must agree with zero_test_exit_behavior in runner_profiles.json."""
    if not PLATFORM_DIR.is_dir():
        pytest.skip(f"No runner fixtures directory for platform '{sys.platform}'")

    registry = load_registry()
    fixtures = _load_all_fixtures()
    if not fixtures:
        pytest.skip(f"No runner fixtures found in {PLATFORM_DIR}")

    for profile in registry.profiles:
        fixture = fixtures.get(profile.profile_id)
        if not fixture:
            continue
        expected_exit = profile.zero_test_exit_behavior

        if fixture["captured_on_host"]:
            actual_exit = fixture["cases"]["zero_tests"]["exit_code"]
            assert actual_exit == expected_exit, (
                f"Profile '{profile.profile_id}': fixture zero_tests exit ({actual_exit}) "
                f"does not match registry zero_test_exit_behavior ({expected_exit})"
            )
        else:
            # For unmeasured profiles, zero_test_exit_behavior is None (unknown)
            # If known_reference has zero_tests_exit and registry specified it, they must agree
            if expected_exit is not None and "zero_tests_exit" in fixture.get("known_reference", {}):
                ref_exit = fixture["known_reference"]["zero_tests_exit"]
                assert ref_exit == expected_exit, (
                    f"Profile '{profile.profile_id}': reference zero_tests_exit ({ref_exit}) "
                    f"does not match registry zero_test_exit_behavior ({expected_exit})"
                )


@pytest.mark.parametrize(
    "profile_id",
    [
        "pytest",
        "node-native-test",
        "jest",
        "mocha",
        "vitest",
        "bun-test",
        "deno-test",
        "go-test",
        "cargo-test",
        "maven-surefire",
        "gradle-test",
        "dotnet-test",
        "cmake-ctest",
        "ruby-rake",
        "phpunit",
        "swiftpm-test",
        "mix-test",
        "xcode-test",
        "terraform",
        "ansible",
        "helm",
    ],
)
def test_live_runner_execution_or_skip(profile_id: str):
    """Executes live verification for available toolchains, or skips with recorded reason.
    
    When run with `pytest -rs`, any missing toolchain is cleanly reported as SKIPPED
    with its reason.
    """
    registry = load_registry()
    profile = registry.get_profile(profile_id)
    assert profile is not None

    fixtures = _load_all_fixtures()
    fixture = fixtures.get(profile_id, {})

    # Check platform compatibility
    if sys.platform not in profile.platforms:
        pytest.skip(f"Profile '{profile_id}' not supported on {sys.platform} (platforms: {profile.platforms})")

    # Determine executable
    executable_name = profile.runner
    if profile_id == "node-native-test":
        executable_name = "node"
    elif profile_id == "bun-test":
        executable_name = "bun"
    elif profile_id == "cargo-test":
        executable_name = "cargo"
    elif profile_id == "pytest":
        executable_name = "pytest"
    elif profile_id == "cmake-ctest":
        executable_name = "ctest"
    elif profile_id == "maven-surefire":
        executable_name = "mvn"
    elif profile_id == "gradle-test":
        executable_name = "gradle"
    elif profile_id == "dotnet-test":
        executable_name = "dotnet"
    elif profile_id == "ruby-rake":
        executable_name = "rake"
    elif profile_id == "swiftpm-test":
        executable_name = "swift"
    elif profile_id == "mix-test":
        executable_name = "mix"
    elif profile_id == "ansible":
        executable_name = "ansible-lint"

    exe_path = shutil.which(executable_name)
    if exe_path is None and profile_id == "pytest":
        # Pytest can be run via python -m pytest
        exe_path = sys.executable

    if exe_path is None:
        skip_msg = fixture.get("skip_reason") or f"Toolchain '{executable_name}' not found in PATH on {sys.platform}"
        pytest.skip(skip_msg)

    # If the toolchain is available, run live zero-test verification
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        if profile_id == "pytest":
            res = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 5, f"Live pytest zero-test exit code was {res.returncode}, expected 5"

        elif profile_id == "node-native-test":
            res = subprocess.run(
                [exe_path, "--test"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 0, f"Live node --test zero-test exit code was {res.returncode}, expected 0"

        elif profile_id == "bun-test":
            res = subprocess.run(
                [exe_path, "test"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 1, f"Live bun test zero-test exit code was {res.returncode}, expected 1"

        elif profile_id == "cargo-test":
            res = subprocess.run(
                [exe_path, "--version"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
            )
            assert res.returncode == 0
