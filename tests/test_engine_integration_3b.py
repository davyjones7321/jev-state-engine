import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)
from jev.workspace import Workspace
from jev.engine import JevEngine, node_gate, node_implement, node_verify, route_gate
from jev.ledger import SqliteLedger


def _create_real_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True, capture_output=True)

    (repo_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()
    return repo_dir, base_commit


# 1. UNMEASURED does not change mechanical_strike_count
def test_unmeasured_does_not_change_mechanical_strike_count(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    wt_path = ws.create_subgoal_worktree("sg-1", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")

    subgoal = Subgoal(id="sg-1", description="subgoal 1", scope=["file.py"], expects_tests=True)
    unmeasured_ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TOOLING_ERROR,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    unmeasured_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="Tooling error during test run.",
        mech_evidence=unmeasured_ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=unmeasured_res)

    state: State = {
        "ticket": "Ticket 1",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt_path),
        "current_worktree_branch": "jev-subgoal-sg-1",
        "integration_branch": "main",
    }

    gk = MagicMock()
    out = node_gate(state, workspace=ws, gatekeeper=gk)

    assert out.get("mechanical_strike_count") == 0
    assert out.get("gate_status") == "unmeasured"
    assert out.get("unmeasured_retry_count") == 1
    assert "Verification could not attribute this failure to your change" in out.get("last_feedback", "")
    assert route_gate(out) == "implement"


# 2. 2 unmeasured retries then verification_inconclusive escalation with strikes still 0
def test_two_unmeasured_retries_then_verification_inconclusive_escalation_strikes_zero(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    subgoal = Subgoal(id="sg-retry", description="retry subgoal", scope=["file.py"], expects_tests=True)

    unmeasured_ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.FLAKY,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    unmeasured_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="Flaky run detected.",
        mech_evidence=unmeasured_ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=unmeasured_res)
    gk = MagicMock()

    # Attempt 1 -> retry 1
    wt1 = ws.create_subgoal_worktree("sg-retry-1", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")
    state: State = {
        "ticket": "Ticket retry",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt1),
        "current_worktree_branch": "jev-subgoal-sg-retry-1",
        "integration_branch": "main",
    }
    out1 = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out1.get("mechanical_strike_count") == 0
    assert out1.get("gate_status") == "unmeasured"
    assert out1.get("unmeasured_retry_count") == 1
    assert out1.get("status") != "escalated"
    assert route_gate(out1) == "implement"

    # Attempt 2 -> retry 2
    wt2 = ws.create_subgoal_worktree("sg-retry-2", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 2\n")
    out1["current_worktree_path"] = str(wt2)
    out1["current_worktree_branch"] = "jev-subgoal-sg-retry-2"
    out2 = node_gate(out1, workspace=ws, gatekeeper=gk)
    assert out2.get("mechanical_strike_count") == 0
    assert out2.get("gate_status") == "unmeasured"
    assert out2.get("unmeasured_retry_count") == 2
    assert out2.get("status") != "escalated"
    assert route_gate(out2) == "implement"

    # Attempt 3 -> escalation
    wt3 = ws.create_subgoal_worktree("sg-retry-3", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 3\n")
    out2["current_worktree_path"] = str(wt3)
    out2["current_worktree_branch"] = "jev-subgoal-sg-retry-3"
    out3 = node_gate(out2, workspace=ws, gatekeeper=gk)
    assert out3.get("mechanical_strike_count") == 0
    assert out3.get("gate_status") == "verification_inconclusive"
    assert out3.get("unmeasured_retry_count") == 3
    assert out3.get("status") == "escalated"
    assert route_gate(out3) == "escalate"
    assert any(entry.get("gate_status") == "verification_inconclusive" for entry in out3.get("trajectory", []))


# 3. FAIL adds exactly one strike
def test_fail_adds_exactly_one_strike(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    wt = ws.create_subgoal_worktree("sg-fail", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")
    subgoal = Subgoal(id="sg-fail", description="fail subgoal", scope=["file.py"], expects_tests=True)

    fail_ev = MechanicalEvidence(
        mech_status=MechStatus.FAIL,
        evidence_level=EvidenceLevel.EXIT_ONLY,
        mechanically_unverified=True,
    )
    fail_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests",
        detail="Candidate tests failed on diff.",
        mech_evidence=fail_ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=fail_res)
    gk = MagicMock()

    state: State = {
        "ticket": "Ticket fail",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt),
        "current_worktree_branch": "jev-subgoal-sg-fail",
        "integration_branch": "main",
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out.get("mechanical_strike_count") == 1
    assert out.get("gate_status") == "mechanical_failure"


# 4. PASS exit_only reaches Jev with the standard block and without the words "full suite"
def test_pass_exit_only_reaches_jev_with_standard_block_without_full_suite(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    wt = ws.create_subgoal_worktree("sg-pass", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")
    subgoal = Subgoal(id="sg-pass", description="passing subgoal", scope=["file.py"], expects_tests=True)

    pass_ev = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.EXIT_ONLY,
        mechanically_unverified=True,
        comparison=Comparison.NOT_COMPARED,
        base_status=None,
    )
    pass_res = MechanicalCheckResult(
        passed=True,
        detail="Targeted tests passed with exit code 0.",
        mech_evidence=pass_ev,
        test_classification="PASSED_EXIT_ONLY",
        full_suite_clean=None,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": True}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=pass_res)

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    state: State = {
        "ticket": "Ticket pass",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt),
        "current_worktree_branch": "jev-subgoal-sg-pass",
        "integration_branch": "main",
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out.get("gate_status") == "passed"

    assert gk.validate_subgoal.called
    kwargs = gk.validate_subgoal.call_args.kwargs
    mech_detail = kwargs.get("mechanical_detail", "")

    # Standard block assertions
    assert "Mechanical Evidence:" in mech_detail
    assert "mechanical_status: PASS" in mech_detail
    assert "evidence_level: exit_only" in mech_detail
    assert "mechanically_unverified: True" in mech_detail
    assert "not a full-suite verification" in mech_detail

    # Must NEVER contain 'full suite' or 'FULL_SUITE_PASSED'
    assert "full suite" not in mech_detail.lower()
    assert "FULL_SUITE_PASSED" not in mech_detail


# 5. env_not_ready adds no strike
def test_env_not_ready_adds_no_strike_and_records_evidence(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    wt = ws.create_subgoal_worktree("sg-env", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")
    subgoal = Subgoal(id="sg-env", description="env subgoal", scope=["file.py"], expects_tests=True)

    env_ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.MISSING_TOOL,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    env_res = MechanicalCheckResult(
        passed=False,
        failed_check="env_not_ready",
        detail="Environment not ready: tool missing.",
        mech_evidence=env_ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=env_res)
    gk = MagicMock()

    state: State = {
        "ticket": "Ticket env",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt),
        "current_worktree_branch": "jev-subgoal-sg-env",
        "integration_branch": "main",
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out.get("status") == "escalated"
    assert out.get("gate_status") == "env_not_ready"
    assert out.get("mechanical_strike_count") == 0

    traj_entry = out.get("trajectory", [])[-1]
    assert traj_entry.get("gate_status") == "env_not_ready"
    assert traj_entry.get("mech_evidence") is not None
    assert str(traj_entry["mech_evidence"]["reason"]).lower() in ("missing_tool", "mechreason.missing_tool")


# 6. ledger row has the mech_* fields
def test_ledger_row_has_mech_fields(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ledger = SqliteLedger(repo_dir=repo_dir)
    ws = Workspace(repo_dir=repo_dir)
    wt = ws.create_subgoal_worktree("sg-ledger", base_ref=base_commit)
    ws.stage_file_mutation("file.py", "x = 1\n")
    subgoal = Subgoal(id="sg-ledger", description="ledger subgoal", scope=["file.py"], expects_tests=True)

    pass_ev = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.EXIT_ONLY,
        mechanically_unverified=True,
        comparison=Comparison.NOT_COMPARED,
        base_status=None,
    )
    pass_res = MechanicalCheckResult(
        passed=True,
        detail="Tests passed exit only.",
        mech_evidence=pass_ev,
        test_classification="PASSED_EXIT_ONLY",
        full_suite_clean=None,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": True}},
    )
    ws.run_mechanical_checks = MagicMock(return_value=pass_res)

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    state: State = {
        "run_id": "run-test-123",
        "ticket": "Ticket ledger",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "current_worktree_path": str(wt),
        "current_worktree_branch": "jev-subgoal-sg-ledger",
        "integration_branch": "main",
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert out.get("gate_status") == "passed"

    with ledger._get_connection() as conn:
        row = conn.execute(
            "SELECT mech_status, mech_evidence_level, mech_mechanically_unverified, mech_comparison FROM attempts WHERE subgoal_id = 'sg-ledger'"
        ).fetchone()

    assert row is not None
    assert row[0] == "PASS"
    assert row[1] == "exit_only"
    assert row[2] == 1  # mechanically_unverified = True
    assert row[3] == "NOT_COMPARED"


# 7. node_verify lists the unverified subgoals and never says FULL_SUITE_PASSED for exit_only
def test_node_verify_lists_unverified_subgoals_and_never_says_full_suite_passed_for_exit_only(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    ws.get_cumulative_diff = MagicMock(return_value="diff --git a/app.py b/app.py\n+y = 2\n")
    ws.check_compile = MagicMock(return_value={"outcome": "PASSED"})
    ws.run_tests = MagicMock(return_value=TestOutcome.PASSED)
    ws.last_test_run = {
        "ran": True,
        "exit_code": 0,
        "outcome": "PASSED",
        "output_tail": "passed 1 test",
    }

    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.95)

    # State with 2 committed subgoals, one was exit_only (unverified), one was counted (verified)
    state: State = {
        "ticket": "Ticket verify",
        "base_commit": base_commit,
        "integration_branch": "main",
        "test_policy": "auto",
        "mechanically_unverified_subgoals": ["sg-unv-1"],
        "trajectory": [
            {
                "node": "gate",
                "gate_status": "passed",
                "subgoal": {"id": "sg-unv-1"},
                "mech_evidence": {"mechanically_unverified": True},
            },
            {
                "node": "gate",
                "gate_status": "passed",
                "subgoal": {"id": "sg-ver-2"},
                "mech_evidence": {"mechanically_unverified": False},
            },
        ],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    # Classification must NEVER say FULL_SUITE_PASSED for exit_only
    assert res.get("test_classification") != "FULL_SUITE_PASSED"
    assert res.get("test_classification") == "PASSED_EXIT_ONLY"
    assert res.get("full_suite_clean") is None

    # Telemetry and state list mechanically unverified subgoals
    assert res.get("mechanically_unverified_subgoals") == ["sg-unv-1"]
    verify_entry = res.get("trajectory", [])[-1]
    assert verify_entry.get("mechanically_unverified_subgoals") == ["sg-unv-1"]


# 8. main builds the Workspace with strict_provenance True
def test_main_builds_workspace_with_strict_provenance_true():
    with patch("main.Workspace") as mock_ws_class, \
         patch("main.Gatekeeper"), \
         patch("main.JevEngine") as mock_engine_class:
        mock_engine = MagicMock()
        mock_engine.execute.return_value = {
            "status": "completed",
            "gate_status": "verified",
            "mechanically_unverified_subgoals": ["sg-test"],
        }
        mock_engine_class.return_value = mock_engine

        from main import main
        # Run main with positional ticket and --no-ledger
        with patch("sys.argv", ["main.py", "test ticket", "--no-ledger"]):
            exit_code = main()

        assert exit_code == 0
        assert mock_ws_class.called
        kwargs = mock_ws_class.call_args.kwargs
        assert kwargs.get("strict_provenance") is True


# 9. no call to run_baseline_tests happens in a run (assert via a spy)
def test_no_call_to_run_baseline_tests_happens_in_run(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir, strict_provenance=True)
    ws.run_baseline_tests = MagicMock(wraps=ws.run_baseline_tests)

    subgoal = Subgoal(id="sg-no-base", description="no base call", scope=["foo.py"], expects_tests=True)
    state: State = {
        "ticket": "Ticket no baseline",
        "base_commit": base_commit,
        "integration_branch": "main",
        "current_subgoal": subgoal,
        "test_policy": "auto",
        "current_worktree_path": None,
        "trajectory": [],
    }

    # Run node_implement (llm=None backwards compatibility)
    node_implement(state, workspace=ws, llm=None)

    # run_baseline_tests must not be called anywhere in node_implement
    assert ws.run_baseline_tests.call_count == 0
