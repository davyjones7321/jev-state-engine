"""tests/test_preflight.py

Tests for Phase 4: Preflight and support profile.
Verifies all 10 checks, statuses IN_PROFILE, OUTSIDE_PROFILE, PROFILE_UNKNOWN,
missing tool handling, Xcode/macOS handling, ambiguous profile handling,
watch mode outside profile, and rule that preflight failure adds no strike.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import node_gate
from jev.mech_evidence import MechReason, MechStatus
from jev.models import State, Subgoal, TestOutcome, ValidationVerdict
from jev.preflight import (
    IN_PROFILE,
    OUTSIDE_PROFILE,
    PROFILE_UNKNOWN,
    PreflightResult,
    PreflightStatus,
    run_preflight,
)
from jev.runner_registry import RunnerProfile, load_registry
from jev.workspace import ResolvedTestCommand, Workspace


def _create_git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    head_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head_commit


# 1. In-profile successful preflight
def test_preflight_in_profile_success(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    (repo_dir / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    ws = Workspace(repo_dir=repo_dir, test_timeout=30)
    cmd = [sys.executable, "-m", "pytest", "-q"]
    resolved = ResolvedTestCommand("pytest", cmd, source="pytest.ini", runner_profile_id="pytest")

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
        profile="pytest",
        test_timeout=30,
    )

    assert result.status == PreflightStatus.IN_PROFILE
    assert result.passed is True
    assert result.reason is None
    assert result.evidence is None
    assert result.is_in_profile is True
    assert result.checks["git_and_disposable_worktree"]["passed"] is True
    assert result.checks["host_platform"]["passed"] is True
    assert result.checks["profile_platform_fit"]["passed"] is True
    assert result.checks["executable_available"]["passed"] is True
    assert result.checks["non_interactive_shape"]["passed"] is True
    assert result.checks["watch_daemon_indicators"]["passed"] is True
    assert result.checks["timeout_and_process_tree"]["passed"] is True


# 2. Missing tool -> OUTSIDE_PROFILE with UNMEASURED(MISSING_TOOL)
def test_preflight_missing_tool_outside_profile(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    missing_cmd = ["nonexistent_tool_binary_xyz_12345", "test"]
    resolved = ResolvedTestCommand("custom", missing_cmd, source="custom", runner_profile_id=None)

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=missing_cmd,
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.passed is False
    assert result.reason == MechReason.MISSING_TOOL
    assert result.is_outside_profile is True
    assert "Executable 'nonexistent_tool_binary_xyz_12345' not found." in result.detail

    # Evidence verification
    ev = result.evidence
    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.MISSING_TOOL
    assert ev.mechanically_unverified is True
    assert ev.full_suite_verified is False


# 3. Xcode/macOS -> OUTSIDE_PROFILE with ENVIRONMENT_UNAVAILABLE
def test_preflight_xcode_environment_unavailable(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    reg = load_registry()
    xcode_profile = reg.get_profile("xcode-test")
    assert xcode_profile is not None
    assert xcode_profile.platforms == ["darwin"]

    cmd = ["xcodebuild", "test"]
    resolved = ResolvedTestCommand("xcodebuild", cmd, source="xcodeproj", runner_profile_id="xcode-test")

    # When evaluated on Windows or Linux (simulate with host_platform="win32" or "linux")
    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        profile=xcode_profile,
        host_platform="win32",
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.passed is False
    assert result.reason == MechReason.ENVIRONMENT_UNAVAILABLE
    assert "Profile 'xcode-test' requires macOS/Xcode" in result.detail
    assert result.evidence is not None
    assert result.evidence.mech_status == MechStatus.UNMEASURED
    assert result.evidence.reason == MechReason.ENVIRONMENT_UNAVAILABLE
    assert result.evidence.mechanically_unverified is True


# 4. Ambiguous profile -> PROFILE_UNKNOWN with UNMEASURED(NO_COMMAND)
def test_preflight_ambiguous_profile(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    resolved = ResolvedTestCommand(
        ecosystem=None,
        command=None,
        source="ambiguous",
        reason="Ambiguous runner profiles detected: cargo-test, pytest",
        also_matched=["cargo-test", "pytest"],
    )

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
    )

    assert result.status == PreflightStatus.PROFILE_UNKNOWN
    assert result.passed is False
    assert result.reason == MechReason.NO_COMMAND
    assert result.is_profile_unknown is True
    assert "Ambiguous runner profiles detected" in result.detail

    assert result.evidence is not None
    assert result.evidence.mech_status == MechStatus.UNMEASURED
    assert result.evidence.reason == MechReason.NO_COMMAND
    assert result.evidence.mechanically_unverified is True


# 5. Watch mode outside profile -> OUTSIDE_PROFILE with TOOLING_ERROR
@pytest.mark.parametrize(
    "watch_flag",
    ["--watch", "--watchAll", "-w", "--watch-all", "--watch=true", "--watchAll=1"],
)
def test_preflight_watch_mode_outside_profile(tmp_path, watch_flag):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    cmd = [sys.executable, "-m", "pytest", watch_flag]
    resolved = ResolvedTestCommand("pytest", cmd, source="pytest.ini", runner_profile_id="pytest")

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
        profile="pytest",
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.passed is False
    assert result.reason == MechReason.TOOLING_ERROR
    assert "Watch" in result.detail or "watch" in result.detail
    assert result.evidence is not None
    assert result.evidence.mech_status == MechStatus.UNMEASURED
    assert result.evidence.reason == MechReason.TOOLING_ERROR
    assert result.evidence.mechanically_unverified is True


# 6. Interactive command shape outside profile
def test_preflight_interactive_command_outside_profile(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    cmd = [sys.executable, "-i", "test.py"]
    resolved = ResolvedTestCommand("pytest", cmd, source="custom")

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.reason == MechReason.TOOLING_ERROR
    assert "interactive" in result.detail.lower()


# 7. Declared external services outside profile
def test_preflight_declared_external_services(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    fake_config = MagicMock()
    fake_config.network_assumptions = "external-api-access"
    fake_config.external_services = ["postgres", "redis"]

    cmd = [sys.executable, "-m", "pytest"]
    resolved = ResolvedTestCommand("pytest", cmd, source="repo_config", runner_profile_id="pytest")

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
        repo_config=fake_config,
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.reason == MechReason.ENVIRONMENT_UNAVAILABLE
    assert "External network" in result.detail


# 8. Unrepresentable target outside profile
def test_preflight_target_representability(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    # Target file that does not exist in worktree
    cmd = [sys.executable, "-m", "pytest", "-q"]
    resolved = ResolvedTestCommand("pytest", cmd, source="pytest.ini", runner_profile_id="pytest")

    result = run_preflight(
        repo_dir=repo_dir,
        worktree_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
        profile="pytest",
        test_targets=["tests/nonexistent_test.py"],
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert "does not exist" in result.detail


# 9. Invalid timeout outside profile
def test_preflight_invalid_timeout(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    cmd = [sys.executable, "-m", "pytest"]
    resolved = ResolvedTestCommand("pytest", cmd, source="pytest.ini", runner_profile_id="pytest")

    result = run_preflight(
        repo_dir=repo_dir,
        resolved_cmd=resolved,
        command=cmd,
        test_timeout=-5,
    )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.reason == MechReason.TOOLING_ERROR
    assert "Test timeout must be positive" in result.detail


# 10. Stops before candidate or base run in Workspace.evaluate_tests
def test_preflight_stops_before_candidate_or_base_run(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_missing.py").write_text("def test_m(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=repo_dir, check=True, capture_output=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-preflight-stop", base_ref=commit)
    subgoal = Subgoal(id="sg-preflight-stop", description="Missing tool check", scope=["tests/test_missing.py"], expects_tests=True)

    fake_cmd = ResolvedTestCommand("custom", ["nonexistent_executable_12345"], source="custom")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd), \
         patch.object(Workspace, "_run_test_subprocess") as mock_sub:
        ev = ws.evaluate_tests(subgoal, base_commit=commit, test_targets=["tests/test_missing.py"])

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.MISSING_TOOL
    assert ev.mechanically_unverified is True
    # Crucial exit condition: subprocess was NEVER launched
    assert mock_sub.call_count == 0


# 11. Preflight failure adds NO strike in engine (missing tool)
def test_preflight_missing_tool_adds_no_strike_in_engine(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=repo_dir, check=True, capture_output=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-no-strike-tool", base_ref=commit)
    subgoal = Subgoal(id="sg-no-strike-tool", description="Test missing tool strike invariant", scope=["app.py"], expects_tests=True)
    ws.stage_file_mutation("app.py", "x = 1\n")

    state = {
        "ticket": "Test missing tool strike",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "test_policy": "always",
        "status": "pending",
        "gate_status": None,
    }

    fake_cmd = ResolvedTestCommand("custom", ["nonexistent_tool_abc"], source="custom")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        res_state = node_gate(state, workspace=ws, gatekeeper=MagicMock())

    # Invariant: Preflight failure (MISSING_TOOL) must add NO mechanical or semantic strike
    assert res_state["mechanical_strike_count"] == 0
    assert res_state["semantic_strike_count"] == 0
    assert res_state["gate_status"] == "env_not_ready"


# 12. Preflight failure adds NO strike in engine (watch mode)
def test_preflight_watch_mode_adds_no_strike_in_engine(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=repo_dir, check=True, capture_output=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-no-strike-watch", base_ref=commit)
    subgoal = Subgoal(id="sg-no-strike-watch", description="Test watch mode strike invariant", scope=["app.py"], expects_tests=True)
    ws.stage_file_mutation("app.py", "x = 1\n")

    state = {
        "ticket": "Test watch mode strike",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "test_policy": "always",
        "status": "pending",
        "gate_status": None,
    }

    watch_cmd = [sys.executable, "-m", "pytest", "--watch"]
    fake_cmd = ResolvedTestCommand("pytest", watch_cmd, source="pytest.ini", runner_profile_id="pytest")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        res_state = node_gate(state, workspace=ws, gatekeeper=MagicMock())

    # Invariant: Watch mode preflight failure (TOOLING_ERROR) must add NO mechanical or semantic strike
    assert res_state["mechanical_strike_count"] == 0
    assert res_state["semantic_strike_count"] == 0
    assert res_state["gate_status"] == "unmeasured"


# 13. Never substitute another runner when tool is missing
def test_preflight_never_substitutes_another_runner(tmp_path):
    repo_dir, head_commit = _create_git_repo(tmp_path)
    (repo_dir / "Cargo.toml").write_text('[package]\nname = "test"\nversion = "0.1.0"\n', encoding="utf-8")

    ws = Workspace(repo_dir=repo_dir)
    # cargo-test resolved from Cargo.toml, but cargo binary is missing
    reg = load_registry()
    cargo_profile = reg.get_profile("cargo-test")

    import shutil as real_shutil
    orig_which = real_shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "cargo":
            return None
        return orig_which(cmd, *args, **kwargs)

    with patch("shutil.which", side_effect=fake_which):
        cmd = ["cargo", "test"]
        resolved = ResolvedTestCommand("cargo", cmd, source="Cargo.toml", runner_profile_id="cargo-test")
        result = run_preflight(
            repo_dir=repo_dir,
            resolved_cmd=resolved,
            profile=cargo_profile,
        )

    assert result.status == PreflightStatus.OUTSIDE_PROFILE
    assert result.reason == MechReason.MISSING_TOOL
    # Profile ID remains cargo-test; no fallback to pytest or node
    assert result.profile_id == "cargo-test"
