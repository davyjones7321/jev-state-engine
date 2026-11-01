import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple, Union
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import JevEngine, node_gate, node_implement
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)
from jev.models import CompileOutcome, Subgoal, TestOutcome
from jev.process_runner import run_command
from jev.workspace import ResolvedTestCommand, Workspace
from main import build_parser


def _create_real_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Initializes a real git repo with an initial commit."""
    repo_dir = tmp_path / f"repo_{time.time_ns()}"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo_dir / ".gitignore").write_text(".jev-worktrees/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    (repo_dir / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return repo_dir, res.stdout.strip()


def _create_fake_runner(tmp_path: Path) -> Path:
    """Creates a scripted fake test runner executable in python."""
    runner_script = tmp_path / "fake_runner_p9.py"
    runner_script.write_text(
        "import sys, os, time, json\n"
        "from pathlib import Path\n"
        "config_file = Path(os.environ.get('FAKE_RUNNER_CONFIG', 'fake_runner_config.json'))\n"
        "cfg = {}\n"
        "if config_file.exists():\n"
        "    try:\n"
        "        cfg = json.loads(config_file.read_text(encoding='utf-8'))\n"
        "    except Exception:\n"
        "        pass\n"
        "cwd_name = Path.cwd().name\n"
        "is_base = cwd_name.startswith('base-')\n"
        "mode = cfg.get('base_mode' if is_base else 'candidate_mode', cfg.get('mode', 'pass'))\n"
        "sleep_sec = cfg.get('base_sleep' if is_base else 'candidate_sleep', cfg.get('sleep', 0))\n"
        "if sleep_sec:\n"
        "    time.sleep(sleep_sec)\n"
        "if mode == 'pass':\n"
        "    sys.stdout.write('FAKE RUNNER PASSED\\n')\n"
        "    sys.exit(0)\n"
        "elif mode == 'fail_clean':\n"
        "    sys.stdout.write('FAILED tests/test_foo.py::test_fail\\n')\n"
        "    sys.exit(1)\n"
        "elif mode == 'flaky_rerun':\n"
        "    marker = Path(cfg.get('marker_file', 'flaky.marker'))\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        sys.stdout.write('FAILED tests/test_foo.py::test_flake\\n')\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write('FAKE RUNNER PASSED\\n')\n"
        "    sys.exit(0)\n"
        "elif mode == 'flaky_fingerprint':\n"
        "    marker = Path(cfg.get('marker_file', 'flaky_fp.marker'))\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        sys.stdout.write('FAILED Alpha error in test\\n')\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write('FAILED Beta error in test\\n')\n"
        "    sys.exit(1)\n"
        "elif mode == 'flaky_base':\n"
        "    marker = Path(cfg.get('marker_file', 'flaky_base.marker'))\n"
        "    if is_base:\n"
        "        if not marker.exists():\n"
        "            marker.write_text('1', encoding='utf-8')\n"
        "            sys.stdout.write('FAILED Base test failure attempt 1\\n')\n"
        "            sys.exit(1)\n"
        "        sys.stdout.write('FAKE RUNNER PASSED on attempt 2\\n')\n"
        "        sys.exit(0)\n"
        "    else:\n"
        "        sys.stdout.write('FAILED candidate failure\\n')\n"
        "        sys.exit(1)\n"
        "else:\n"
        "    sys.stdout.write(f'Unknown mode: {mode}\\n')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    return runner_script


def _set_runner_config(config_path: Path, monkeypatch, **kwargs) -> None:
    config_path.write_text(json.dumps(kwargs), encoding="utf-8")
    monkeypatch.setenv("FAKE_RUNNER_CONFIG", str(config_path))


# ==============================================================================
# 1. CLI Flags in main.py
# ==============================================================================

def test_main_cli_flags_configuration():
    parser = build_parser()
    # Test default values
    args = parser.parse_args([])
    assert args.subgoal_timeout == 600
    assert args.ticket_timeout == 2700
    assert args.process_cleanup_grace_period == 2.0

    # Test custom values
    args_custom = parser.parse_args([
        "--subgoal-timeout", "300",
        "--ticket-timeout", "1800",
        "--process-cleanup-grace-period", "5.5",
    ])
    assert args_custom.subgoal_timeout == 300
    assert args_custom.ticket_timeout == 1800
    assert args_custom.process_cleanup_grace_period == 5.5


# ==============================================================================
# 2. Engine and Workspace Parameter Validation
# ==============================================================================

def test_engine_timeout_validation():
    with pytest.raises(ValueError, match="subgoal_timeout"):
        JevEngine(subgoal_timeout=0)
    with pytest.raises(ValueError, match="subgoal_timeout"):
        JevEngine(subgoal_timeout=-10)
    with pytest.raises(ValueError, match="ticket_timeout"):
        JevEngine(ticket_timeout=0)
    with pytest.raises(ValueError, match="process_cleanup_grace_period"):
        JevEngine(process_cleanup_grace_period=0.0)

    engine = JevEngine(
        subgoal_timeout=450,
        ticket_timeout=1500,
        process_cleanup_grace_period=3.5,
    )
    assert engine.subgoal_timeout == 450
    assert engine.ticket_timeout == 1500
    assert engine.process_cleanup_grace_period == 3.5


def test_workspace_init_parameters():
    ws = Workspace(
        process_cleanup_grace_period=4.0,
        candidate_rerun_mode="same_worktree",
    )
    assert ws.process_cleanup_grace_period == 4.0
    assert ws.candidate_rerun_mode == "same_worktree"


# ==============================================================================
# 3. Wall-Clock Caps (Subgoal and Ticket) in node_gate
# ==============================================================================

def test_subgoal_wall_clock_timeout_cap(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    subgoal = Subgoal(id="sg-timeout-check", description="Test subgoal timeout", scope=["src/a.py"])

    # Simulate subgoal running for 10 seconds with cap at 5 seconds
    state = {
        "ticket": "Timeout test ticket",
        "thread_id": "thread-subgoal-timeout",
        "current_subgoal": subgoal,
        "subgoal_start_time": time.time() - 10.0,
        "subgoal_timeout": 5,
        "ticket_start_time": time.time(),
        "ticket_timeout": 2700,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    mock_gatekeeper = MagicMock()
    result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

    assert result_state["gate_status"] == "verification_inconclusive"
    assert result_state["status"] == "escalated"
    assert result_state["current_subgoal"].status == "failed"
    # Invariant: No budget exhaustion ever costs a strike
    assert result_state["mechanical_strike_count"] == 0
    assert result_state["semantic_strike_count"] == 0


def test_ticket_wall_clock_timeout_cap(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    subgoal = Subgoal(id="sg-ticket-timeout", description="Test ticket timeout", scope=["src/a.py"])

    # Simulate ticket running for 3000 seconds with cap at 2700 seconds
    state = {
        "ticket": "Ticket timeout task",
        "thread_id": "thread-ticket-timeout",
        "current_subgoal": subgoal,
        "subgoal_start_time": time.time(),
        "subgoal_timeout": 600,
        "ticket_start_time": time.time() - 3000.0,
        "ticket_timeout": 2700,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    mock_gatekeeper = MagicMock()
    result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

    assert result_state["gate_status"] == "verification_inconclusive"
    assert result_state["status"] == "escalated"
    assert result_state["current_subgoal"].status == "failed"
    # Invariant: No budget exhaustion ever costs a strike
    assert result_state["mechanical_strike_count"] == 0
    assert result_state["semantic_strike_count"] == 0


# ==============================================================================
# 4. Caps in Workspace: Candidate Adjudication Cap & Base Cap
# ==============================================================================

def test_candidate_adjudication_cap_tooling_error(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_cand_cap.py").write_text("def test_cand_cap(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_cand_cap"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-cand-cap", base_ref=head_commit)
    subgoal = Subgoal(id="sg-cand-cap", description="Candidate cap test", scope=["tests/test_cand_cap.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_cand_cap.py", "def test_cand_cap(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        # When 2 candidate runs for this attempt are exhausted
        ws._candidate_run_counts["sg-cand-cap#1"] = 2
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_cand_cap.py"], attempt=1)
        assert ev.mech_status == MechStatus.UNMEASURED
        assert ev.reason == MechReason.TOOLING_ERROR
        assert ev.mechanically_unverified is True


def test_base_cap_exhaustion_baseline_unavailable(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_base_cap.py").write_text("def test_base_cap(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_base_cap"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-base-cap", base_ref=head_commit)
    subgoal = Subgoal(id="sg-base-cap", description="Base cap test", scope=["tests/test_base_cap.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_base_cap.py", "def test_base_cap(): assert False\n")

    # Base test fails without recognized parser IDs, exhausting retries
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="fail_empty")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_base_cap.py"], attempt=1)
        # When base runs are exhausted without definitive comparison, returns BASELINE_UNAVAILABLE
        assert ev.mech_status == MechStatus.UNMEASURED
        assert ev.reason == MechReason.BASELINE_UNAVAILABLE
        assert ev.mechanically_unverified is True


def test_flaky_base_is_flaky(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"
    marker_file = tmp_path / "flaky_base.marker"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_flaky_base.py").write_text("def test_flaky_base(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_flaky_base"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-flaky-base", base_ref=head_commit)
    subgoal = Subgoal(id="sg-flaky-base", description="Flaky base test", scope=["tests/test_flaky_base.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_flaky_base.py", "def test_flaky_base(): assert False\n")

    _set_runner_config(config_path, monkeypatch, mode="flaky_base", marker_file=str(marker_file))
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_flaky_base.py"], attempt=1)
        assert ev.mech_status == MechStatus.UNMEASURED
        assert ev.reason == MechReason.FLAKY
        assert ev.mechanically_unverified is True


# ==============================================================================
# 5. Process Timeout Kills Descendants
# ==============================================================================

def test_timeout_kills_descendants():
    expected_cleanup = "job_object" if sys.platform == "win32" else "process_group"

    # Spawn grandchild sleeping for 60s, then child sleeps for 60s
    spawn_grandchild_code = """
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
"""

    res = run_command(
        [sys.executable, "-c", spawn_grandchild_code],
        timeout=1.0,
    )
    assert res.timed_out is True
    assert res.termination_reason == "timeout"
    assert res.cleanup_method == expected_cleanup
    assert res.descendants_remaining == 0


# ==============================================================================
# 6. Candidate Rerun Recording: Fresh-Worktree vs Same-Worktree
# ==============================================================================

def test_candidate_rerun_fresh_worktree_recorded(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_fresh_wt.py").write_text("def test_fresh_wt(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_fresh_wt"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir, candidate_rerun_mode="fresh_worktree")
    ws.create_subgoal_worktree("sg-fresh-wt", base_ref=head_commit)
    subgoal = Subgoal(id="sg-fresh-wt", description="Fresh worktree rerun test", scope=["tests/test_fresh_wt.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_fresh_wt.py", "def test_fresh_wt(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_fresh_wt.py"], attempt=1)
        assert ws.last_test_run is not None
        assert ws.last_test_run.get("rerun_mode") in ("fresh_worktree", "same_worktree")
        if ev.attribution_note:
            assert "rerun_mode:" in ev.attribution_note


def test_candidate_rerun_same_worktree_recorded(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_same_wt.py").write_text("def test_same_wt(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_same_wt"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir, candidate_rerun_mode="same_worktree")
    ws.create_subgoal_worktree("sg-same-wt", base_ref=head_commit)
    subgoal = Subgoal(id="sg-same-wt", description="Same worktree rerun test", scope=["tests/test_same_wt.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_same_wt.py", "def test_same_wt(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_same_wt.py"], attempt=1, rerun_mode="same_worktree")
        assert ws.last_test_run is not None
        assert ws.last_test_run.get("rerun_mode") == "same_worktree"
        assert ws.last_test_run.get("rerun_context") == "same_worktree_cleaned"
        if ev.attribution_note:
            assert "rerun_mode:same_worktree" in ev.attribution_note


# ==============================================================================
# 7. Repeated Stable Fingerprints Recorded for Diagnosis Only
# ==============================================================================

def test_repeated_stable_fingerprints_recorded_for_diagnosis_only(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_fp_stable.py").write_text("def test_fp_stable(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_fp_stable"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir, candidate_rerun_mode="same_worktree")
    ws.create_subgoal_worktree("sg-fp-stable", base_ref=head_commit)
    subgoal = Subgoal(id="sg-fp-stable", description="Stable fingerprint test", scope=["tests/test_fp_stable.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_fp_stable.py", "def test_fp_stable(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_fp_stable.py"], attempt=1)
        assert ws.last_test_run is not None
        assert "stable_fingerprint" in ws.last_test_run
        assert ev.attribution_note is not None
        assert "stable_fingerprint:" in ev.attribution_note


# ==============================================================================
# 8. No Budget Exhaustion Ever Costs a Strike
# ==============================================================================

def test_no_budget_exhaustion_ever_costs_a_strike(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    for budget_exhaustion_ev in [
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.TIMEOUT),
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.TOOLING_ERROR),
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.BASELINE_UNAVAILABLE),
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.FLAKY),
    ]:
        subgoal = Subgoal(id=f"sg-{budget_exhaustion_ev.reason.value}", description="Budget exhaustion test", scope=["src/x.py"])
        state = {
            "ticket": "Budget strike check",
            "thread_id": f"thread-strike-{budget_exhaustion_ev.reason.value}",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "semantic_strike_count": 0,
            "unmeasured_retry_count": 0,
            "trajectory": [],
        }

        with patch.object(ws, "run_mechanical_checks") as mock_checks, patch.object(ws, "get_staged_diff", return_value="dummy diff"):
            from jev.workspace import MechanicalCheckResult
            mock_checks.return_value = MechanicalCheckResult(
                passed=False,
                failed_check="tests",
                detail=f"Budget exhausted: {budget_exhaustion_ev.reason.value}",
                checks_run=["build", "suppression", "unresolved_import", "compile", "tests"],
                checks={"tests": {"ran": True, "passed": False}},
                test_runner_outcome={"mech_evidence": budget_exhaustion_ev},
                mech_evidence=budget_exhaustion_ev,
            )

            res_state = node_gate(state, workspace=ws, gatekeeper=MagicMock())
            # Worker must NOT receive any mechanical strikes on budget exhaustion
            assert res_state["mechanical_strike_count"] == 0
            assert res_state["semantic_strike_count"] == 0
