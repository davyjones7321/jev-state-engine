import ast
import inspect
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import JevEngine, node_gate, node_verify, route_gate
from jev.evidence import evidence_level, format_evidence_summary
from jev.ledger import NullRecorder, SqliteLedger
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
    TargetConfidence,
)
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)


# ==============================================================================
# 1. Old ledger fixture loading & legacy rows never verified
# ==============================================================================

def test_old_ledger_fixture_loads_and_legacy_rows_never_verified(tmp_path):
    """Old ledger fixture loads additively, and legacy rows never become verified."""
    sql_file = Path("tests/fixtures/ledger_pre_step1.sql")
    assert sql_file.exists(), "tests/fixtures/ledger_pre_step1.sql must exist"

    db_path = tmp_path / "legacy_fixture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.close()

    # Open with SqliteLedger -> schema migrations run additively
    ledger = SqliteLedger(db_path=db_path)

    # Legacy attempt must return None for mech evidence
    legacy_ev = ledger.get_attempt_mech_evidence("run-pre-1", "sg-1", 1)
    assert legacy_ev is None, "Legacy rows must have NULL mech fields and return None"

    # Direct DB inspection confirms columns exist and are NULL
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM attempts WHERE run_id = 'run-pre-1'").fetchone()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(attempts)").fetchall()]
    conn.close()

    assert "mech_status" in cols
    assert "mech_reason" in cols
    assert "mech_unverified_kind" in cols
    assert "mech_mechanically_unverified" in cols

    assert row["mech_status"] is None
    assert row["mech_reason"] is None
    assert row["mech_unverified_kind"] is None
    assert row["mech_mechanically_unverified"] is None


# ==============================================================================
# 2. Evidence round-trips & TEST_SURFACE_CHANGED persistence
# ==============================================================================

def test_evidence_round_trip_with_test_surface_changed(tmp_path):
    """TEST_SURFACE_CHANGED persists, round-trips through ledger, and preserves unverified status."""
    db_path = tmp_path / "test_surface.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "run-tsc-1"
    subgoal_id = "sg-tsc-1"

    ledger.record_run_start(run_id=run_id, thread_id="t1", ticket="tsc test")

    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TEST_SURFACE_CHANGED,
        test_surface_changed=True,
        unverified_kind="untested",
        language="python",
        runner_profile_id="pytest",
        registry_version="1.0.0",
    )
    assert ev.mechanically_unverified is True
    assert ev.unverified_kind == "untested"

    ledger.record_attempt(
        run_id=run_id,
        subgoal_id=subgoal_id,
        tier="mechanical",
        outcome="unmeasured",
        mech_evidence=ev,
    )

    loaded_ev = ledger.get_attempt_mech_evidence(run_id, subgoal_id, 1)
    assert loaded_ev is not None
    assert loaded_ev.mech_status == MechStatus.UNMEASURED
    assert loaded_ev.reason == MechReason.TEST_SURFACE_CHANGED
    assert loaded_ev.test_surface_changed is True
    assert loaded_ev.mechanically_unverified is True
    assert loaded_ev.unverified_kind == "untested"
    assert loaded_ev.language == "python"
    assert loaded_ev.runner_profile_id == "pytest"


def test_evidence_round_trip_exit_only(tmp_path):
    """Exit-only evidence round-trips and preserves unverified_kind='exit_only'."""
    db_path = tmp_path / "exit_only.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "run-eo-1"
    subgoal_id = "sg-eo-1"

    ledger.record_run_start(run_id=run_id, thread_id="t2", ticket="exit only test")

    ev = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.EXIT_ONLY,
        language="python",
        runner_profile_id="pytest",
    )
    assert ev.full_suite_verified is False
    assert ev.mechanically_unverified is True
    assert ev.unverified_kind == "exit_only"

    ledger.record_attempt(
        run_id=run_id,
        subgoal_id=subgoal_id,
        tier="mechanical",
        outcome="passed",
        mech_evidence=ev,
    )

    loaded_ev = ledger.get_attempt_mech_evidence(run_id, subgoal_id, 1)
    assert loaded_ev is not None
    assert loaded_ev.mech_status == MechStatus.PASS
    assert loaded_ev.evidence_level == EvidenceLevel.EXIT_ONLY
    assert loaded_ev.full_suite_verified is False
    assert loaded_ev.mechanically_unverified is True
    assert loaded_ev.unverified_kind == "exit_only"


# ==============================================================================
# 3. Model invariants: UNMEASURED requires reason, exit_only never full-suite
# ==============================================================================

def test_unmeasured_requires_reason_and_forces_unverified():
    """UNMEASURED requires a reason and forces mechanically_unverified=True."""
    with pytest.raises(ValueError, match="requires a reason"):
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED)

    with pytest.raises(ValueError, match="cannot be False"):
        MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.TIMEOUT,
            mechanically_unverified=False,
        )

    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TEST_SURFACE_CHANGED,
    )
    assert ev.mechanically_unverified is True


def test_exit_only_never_full_suite():
    """exit_only forces full_suite_verified=False and mechanically_unverified=True on PASS."""
    with pytest.raises(ValueError, match="forces full_suite_verified=False"):
        MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=EvidenceLevel.EXIT_ONLY,
            full_suite_verified=True,
        )

    with pytest.raises(ValueError, match="forces mechanically_unverified=True"):
        MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=EvidenceLevel.EXIT_ONLY,
            mechanically_unverified=False,
        )

    ev = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.EXIT_ONLY,
    )
    assert ev.full_suite_verified is False
    assert ev.mechanically_unverified is True


# ==============================================================================
# 4. Execution path can set TEST_SURFACE_CHANGED in node_gate
# ==============================================================================

def test_execution_path_sets_test_surface_changed(tmp_path):
    """When a check fails with test_surface_changed, node_gate sets TEST_SURFACE_CHANGED."""
    workspace = MagicMock()
    workspace.get_staged_diff.return_value = "+some diff\n"
    workspace.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False,
        failed_check="test_surface_changed",
        detail="Shared test fixtures were modified.",
        checks_run=["build", "compile", "tests"],
    )

    ledger = SqliteLedger(db_path=tmp_path / "tsc_gate.db")
    run_id = "run-tsc-gate"
    ledger.record_run_start(run_id=run_id, thread_id="t3", ticket="tsc gate")

    subgoal = Subgoal(id="sg-tsc", description="modify shared test helpers", scope=["conftest.py"])
    state = {
        "ticket": "tsc gate",
        "run_id": run_id,
        "plan_queue": [subgoal],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }

    result_state = node_gate(state, workspace=workspace, recorder=ledger)

    # UNMEASURED does not strike!
    assert result_state.get("mechanical_strike_count") == 0
    assert result_state.get("gate_status") == "unmeasured"

    traj = result_state.get("trajectory", [])
    assert len(traj) > 0
    mech_ev_dict = traj[-1].get("mech_evidence")
    assert mech_ev_dict is not None
    assert mech_ev_dict["reason"] == "TEST_SURFACE_CHANGED"
    assert mech_ev_dict["test_surface_changed"] is True


# ==============================================================================
# 5. Final report lists both untested and exit-only subgoals (with kinds)
# ==============================================================================

def test_node_gate_records_untested_and_exit_only_subgoals(tmp_path):
    """Both exit-only passes and untested passes are listed in mechanically_unverified_subgoals."""
    db_path = tmp_path / "unv_report.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "run-unv-kinds"
    ledger.record_run_start(run_id=run_id, thread_id="t4", ticket="unv kinds test")

    gatekeeper = MagicMock()
    gatekeeper.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    workspace = MagicMock()
    workspace.get_staged_diff.return_value = "+some diff\n"
    workspace.worktree_dir = Path("/tmp/mock_repo")
    workspace.repo_dir = Path("/tmp/mock_repo")
    workspace.commit_subgoal = MagicMock()
    workspace._get_head_commit = MagicMock(return_value="commit-123")

    # --- Subgoal 1: Exit-only pass ---
    sg1 = Subgoal(id="sg-eo", description="exit only pass", expects_tests=True)
    ev_eo = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.EXIT_ONLY,
    )
    workspace.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=True,
        detail="passed exit only",
        mech_evidence=ev_eo,
    )

    state1 = {
        "run_id": run_id,
        "current_subgoal": sg1,
        "plan_queue": [sg1],
        "trajectory": [],
        "committed_subgoals": [],
        "mechanically_unverified_subgoals": [],
    }
    res1 = node_gate(state1, workspace=workspace, gatekeeper=gatekeeper, recorder=ledger)
    assert res1["mechanically_unverified_subgoals"] == ["sg-eo"]
    assert res1.get("mechanically_unverified_kinds", {}).get("sg-eo") == "exit_only"

    # --- Subgoal 2: Untested pass (expects_tests=False) ---
    sg2 = Subgoal(id="sg-untested", description="untested pass", expects_tests=False)
    workspace.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=True,
        detail="docs only, no tests",
        mech_evidence=None,
    )

    res1["current_subgoal"] = sg2
    res1["plan_queue"] = [sg2]
    res2 = node_gate(res1, workspace=workspace, gatekeeper=gatekeeper, recorder=ledger)
    assert "sg-eo" in res2["mechanically_unverified_subgoals"]
    assert "sg-untested" in res2["mechanically_unverified_subgoals"]
    assert res2.get("mechanically_unverified_kinds", {}).get("sg-untested") == "untested"

    # Check that ledger recorded the kind for each attempt
    att_eo = ledger.get_attempt_mech_evidence(run_id, "sg-eo", 1)
    assert att_eo is not None
    assert att_eo.unverified_kind == "exit_only"

    # Verify trajectory preserved kinds
    gate_entries = [e for e in res2["trajectory"] if e.get("node") == "gate" and e.get("gate_status") == "passed"]
    assert len(gate_entries) == 2
    assert gate_entries[0]["unverified_kind"] == "exit_only"
    assert gate_entries[1]["unverified_kind"] == "untested"


# ==============================================================================
# 6. No string-replace workaround for FULL_SUITE_PASSED
# ==============================================================================

def test_no_string_replacement_of_full_suite_passed():
    """engine.py must not contain string replacement workaround for FULL_SUITE_PASSED."""
    engine_src = Path("src/jev/engine.py").read_text(encoding="utf-8")
    assert '.replace("FULL_SUITE_PASSED"' not in engine_src, (
        "String-replace workaround for FULL_SUITE_PASSED must be removed"
    )
    assert ".replace('FULL_SUITE_PASSED'" not in engine_src, (
        "String-replace workaround for FULL_SUITE_PASSED must be removed"
    )


# ==============================================================================
# 7. Grep-based test: Gate does not read legacy classification strings for decisions
# ==============================================================================

def test_grep_gate_does_not_read_legacy_classification_for_decisions():
    """Grep/AST test proving node_gate and route_gate do not make decisions based on legacy classification."""
    engine_path = Path("src/jev/engine.py")
    tree = ast.parse(engine_path.read_text(encoding="utf-8"), filename=str(engine_path))

    target_funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("node_gate", "route_gate"):
            target_funcs[node.name] = node

    assert "node_gate" in target_funcs, "node_gate must exist"
    assert "route_gate" in target_funcs, "route_gate must exist"

    legacy_names = {
        "test_classification",
        "baseline_failures",
        "new_failures",
        "full_suite_clean",
        "baseline_failing_ids",
        "new_failing_ids",
    }
    legacy_strings = {
        "FULL_SUITE_PASSED",
        "REGRESSION_FREE_WITH_BASELINE_FAILURES",
        "REGRESSION_DETECTED",
        "FAILED_BY_DIFF",
    }

    # Inspect all If-statements in route_gate: none must reference legacy names or strings
    for node in ast.walk(target_funcs["route_gate"]):
        if isinstance(node, ast.If):
            for subnode in ast.walk(node.test):
                if isinstance(subnode, ast.Constant) and isinstance(subnode.value, str):
                    assert subnode.value not in legacy_names, (
                        f"route_gate decision reads legacy field {subnode.value}"
                    )
                    assert subnode.value not in legacy_strings, (
                        f"route_gate decision reads legacy classification string {subnode.value}"
                    )

    # In node_gate: verify that strike increments and commit calls are never guarded by legacy fields
    for node in ast.walk(target_funcs["node_gate"]):
        if isinstance(node, ast.If):
            # Check if this If-statement's body increments mechanical_strike_count or semantic_strike_count
            body_src = ast.unparse(node.body)
            has_strike_or_commit = (
                "mechanical_strike_count" in body_src
                or "semantic_strike_count" in body_src
                or "commit_subgoal" in body_src
                or "merge_subgoal_worktree" in body_src
            )
            if has_strike_or_commit:
                test_src = ast.unparse(node.test)
                for leg_name in legacy_names:
                    assert leg_name not in test_src, (
                        f"node_gate decision guarding strike/commit reads legacy field: {leg_name} in '{test_src}'"
                    )
                for leg_str in legacy_strings:
                    assert leg_str not in test_src, (
                        f"node_gate decision guarding strike/commit reads legacy string: {leg_str} in '{test_src}'"
                    )


# ==============================================================================
# 8. Report line replaces misleading test_policy=auto with real cause
# ==============================================================================

def test_main_report_line_replaces_misleading_auto_policy(capsys):
    """main() replaces misleading test_policy=auto with real cause."""
    from main import main
    import sys

    # Simulate running main with args where test_policy is auto but expects_tests=false
    # We can invoke main with mocks
    with patch("main.JevEngine") as mock_engine_cls, \
         patch("main.SqliteLedger") as mock_ledger_cls:

        mock_engine = MagicMock()
        mock_engine.execute.return_value = {
            "status": "completed",
            "run_id": "test-run",
            "committed_subgoals": [Subgoal(id="sg-1", expects_tests=False)],
            "mechanically_unverified_subgoals": ["sg-1"],
            "mechanically_unverified_kinds": {"sg-1": "untested"},
        }
        mock_engine_cls.return_value = mock_engine

        mock_recorder = MagicMock()
        mock_recorder.get_run_evidence_level.return_value = "compile-only"
        mock_recorder.get_run_dependency_delta.return_value = None
        mock_ledger_cls.return_value = mock_recorder

        test_args = [
            "main.py",
            "--test-policy", "auto",
            "test ticket",
        ]
        with patch.object(sys, "argv", test_args):
            exit_code = main()
            assert exit_code == 0

        captured = capsys.readouterr()
        # Must NOT contain the misleading "no tests ran: test_policy=auto"
        assert "no tests ran: test_policy=auto" not in captured.out
        # Must contain real cause: expects_tests=false
        assert "Evidence: compile-only (no tests ran: expects_tests=false)" in captured.out
        # Must list mechanically unverified subgoals
        assert "Mechanically unverified subgoals: 1 (sg-1)" in captured.out

