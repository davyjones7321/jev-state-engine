import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from jev.engine import (
    JevEngine,
    SqliteSaver,
    node_gate,
    node_implement,
    node_investigate,
    node_plan,
    node_verify,
    route_gate,
)
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
import main


class FakeWorkspace:
    def __init__(
        self,
        mechanical_result=None,
        staged_diff="+x = 1\n",
        test_outcome=TestOutcome.PASSED,
    ):
        self.mechanical_result = mechanical_result or MechanicalCheckResult(passed=True)
        self.staged_diff = staged_diff
        self.test_outcome = test_outcome
        self.rollback_call_count = 0
        self.commit_call_count = 0

    def run_mechanical_checks(self, subgoal, base_commit=None):
        return self.mechanical_result

    def get_staged_diff(self):
        return self.staged_diff

    def get_cumulative_diff(self, base_ref=None):
        return self.staged_diff

    def rollback_subgoal(self):
        self.rollback_call_count += 1

    def commit_subgoal(self):
        self.commit_call_count += 1

    def run_read_tool(self, cmd, args=None):
        return "M file.py"

    def run_tests(self):
        if not hasattr(self, "last_test_run") or self.last_test_run is None:
            self.last_test_run = {
                "ran": True,
                "command": ["pytest"],
                "exit_code": 0 if self.test_outcome == TestOutcome.PASSED else 1,
                "outcome": getattr(self.test_outcome, "value", str(self.test_outcome)),
                "output_tail": "passed" if self.test_outcome == TestOutcome.PASSED else "failed",
            }
        return self.test_outcome

    def resolve_in_worktree(self, path):
        raw = str(path).replace("\\", "/")
        if ".." in raw.split("/") or raw.startswith("/"):
            raise ValueError(f"Path '{path}' traverses outside worktree.")
        return Path(path)

    def stage_file_mutation(self, path, content):
        pass

    def create_verify_worktree(self, integration_branch: str):
        return None

    def discard_verify_worktree(self, verify_wt_path):
        pass

    def fast_forward_main(self, integration_branch: str, base_commit: Optional[str] = None):
        pass

    def delete_integration_branch(self, branch_name: str):
        pass


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95)
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.99)
        self.validate_subgoal = MagicMock(side_effect=self._validate_subgoal)
        self.verify_ticket = MagicMock(side_effect=self._verify_ticket)
        self.escalate_deadlock = MagicMock()

    def _validate_subgoal(self, subgoal, diff, mechanical_detail=""):
        return self.verdict

    def _verify_ticket(self, ticket, final_diff, test_output):
        return self.verify_verdict


# 1. Verify a mechanical failure increments only mechanical_strike_count
def test_mechanical_failure_increments_only_mechanical_strike_count():
    state: State = {
        "ticket": "T1",
        "current_subgoal": Subgoal(description="Sub 1", scope=["a.py"], expects_tests=True),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    ws = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="Build failed")
    )
    gk = FakeGatekeeper()

    new_state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert new_state["mechanical_strike_count"] == 1
    assert new_state["semantic_strike_count"] == 0
    assert gk.validate_subgoal.call_count == 0


# 2. Verify a Jev rejection increments only semantic_strike_count
def test_jev_rejection_increments_only_semantic_strike_count():
    state: State = {
        "ticket": "T2",
        "current_subgoal": Subgoal(description="Sub 2", scope=["b.py"], expects_tests=True),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    ws = FakeWorkspace(mechanical_result=MechanicalCheckResult(passed=True))
    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=False, probability=0.2, reason="Scope violation"))

    new_state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert new_state["mechanical_strike_count"] == 0
    assert new_state["semantic_strike_count"] == 1
    assert ws.rollback_call_count == 1
    assert ws.commit_call_count == 0


# 3. Verify a successful commit resets both counters
def test_successful_commit_resets_both_counters():
    state: State = {
        "ticket": "T3",
        "current_subgoal": Subgoal(description="Sub 3", scope=["c.py"], expects_tests=True),
        "mechanical_strike_count": 2,
        "semantic_strike_count": 2,
        "trajectory": [],
    }
    ws = FakeWorkspace(mechanical_result=MechanicalCheckResult(passed=True))
    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=True, probability=0.98))

    new_state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert new_state["mechanical_strike_count"] == 0
    assert new_state["semantic_strike_count"] == 0
    assert ws.commit_call_count == 1


# 4. Verify escalation fires independently at 3 strikes on either counter
def test_escalation_fires_independently():
    # Test mechanical escalation at 3
    state_mech: State = {
        "ticket": "T4",
        "current_subgoal": Subgoal(description="Sub 4", scope=["d.py"], expects_tests=True),
        "mechanical_strike_count": 2,
        "semantic_strike_count": 1,
        "trajectory": [],
    }
    ws_fail = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="Err")
    )
    gk_mech = FakeGatekeeper()
    out_mech = node_gate(state_mech, workspace=ws_fail, gatekeeper=gk_mech)
    assert out_mech["mechanical_strike_count"] == 3
    assert out_mech["semantic_strike_count"] == 1
    gk_mech.escalate_deadlock.assert_called_once_with(
        trajectory=out_mech["trajectory"],
        triggering_tier="mechanical",
    )

    # Test semantic escalation at 3
    state_sem: State = {
        "ticket": "T4b",
        "current_subgoal": Subgoal(description="Sub 4b", scope=["e.py"], expects_tests=True),
        "mechanical_strike_count": 1,
        "semantic_strike_count": 2,
        "trajectory": [],
    }
    ws_pass = FakeWorkspace(mechanical_result=MechanicalCheckResult(passed=True))
    gk_sem = FakeGatekeeper(verdict=ValidationVerdict(valid=False, probability=0.1, reason="No"))
    out_sem = node_gate(state_sem, workspace=ws_pass, gatekeeper=gk_sem)
    assert out_sem["mechanical_strike_count"] == 1
    assert out_sem["semantic_strike_count"] == 3
    gk_sem.escalate_deadlock.assert_called_once_with(
        trajectory=out_sem["trajectory"],
        triggering_tier="semantic",
    )


# 5. Test SQLite checkpointer persistence
def test_sqlite_saver_persistence(tmp_path):
    db_file = tmp_path / "test_checkpoints.db"
    saver = SqliteSaver.from_conn_string(str(db_file))

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "checkpoints" in tables
    assert "checkpoint_writes" in tables

    # Test put and get_tuple
    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    checkpoint_data = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-09-22T00:00:00Z",
        "channel_values": {
            "mechanical_strike_count": 2,
            "semantic_strike_count": 1,
        },
        "channel_versions": {},
        "versions_seen": {},
    }
    metadata = {"source": "test"}

    ret_config = saver.put(config, checkpoint_data, metadata, {})
    assert ret_config["configurable"]["checkpoint_id"] == "cp-1"

    tup = saver.get_tuple(ret_config)
    assert tup is not None
    assert tup.checkpoint["channel_values"]["mechanical_strike_count"] == 2
    assert tup.checkpoint["channel_values"]["semantic_strike_count"] == 1

    # Test second checkpoint and retrieval of latest (without specifying checkpoint_id)
    checkpoint_data_2 = {
        "v": 1,
        "id": "cp-2",
        "ts": "2026-09-22T00:01:00Z",
        "channel_values": {
            "mechanical_strike_count": 0,
            "semantic_strike_count": 0,
        },
        "channel_versions": {},
        "versions_seen": {},
    }
    saver.put(config, checkpoint_data_2, {"source": "test2"}, {})
    latest_tup = saver.get_tuple({"configurable": {"thread_id": "thread-1"}})
    assert latest_tup is not None
    assert latest_tup.config["configurable"]["checkpoint_id"] == "cp-2"
    assert latest_tup.checkpoint["channel_values"]["mechanical_strike_count"] == 0

    # Test list
    listed = list(saver.list({"configurable": {"thread_id": "thread-1"}}))
    assert len(listed) == 2
    assert listed[0].config["configurable"]["checkpoint_id"] == "cp-2"
    assert listed[1].config["configurable"]["checkpoint_id"] == "cp-1"


# 6. Test FSM nodes: investigate, plan, implement, verify
def test_fsm_node_lifecycle():
    ws = FakeWorkspace()
    gk = FakeGatekeeper()

    # Investigate
    state: State = {
        "ticket": "Implement feature Y",
        "trajectory": [],
        "integration_branch": "jev-ticket-lifecycle",
    }
    state = node_investigate(state, workspace=ws)
    assert "Workspace status" in state.get("last_feedback", "")
    assert len(state["trajectory"]) == 1

    # Plan
    state = node_plan(state, workspace=ws)
    assert len(state["plan_queue"]) == 1
    assert isinstance(state["plan_queue"][0], Subgoal)
    assert state["plan_queue"][0].description == "Implement feature Y"

    # Implement
    state = node_implement(state, workspace=ws)
    assert state["current_subgoal"] is not None
    assert state["current_subgoal"].description == "Implement feature Y"
    assert len(state["plan_queue"]) == 0

    # Verify
    state = node_verify(state, workspace=ws, gatekeeper=gk)
    assert state["status"] == "completed"
    assert state["gate_status"] == "verified"
    assert gk.verify_ticket.call_count == 1


# 7. Test JevEngine execute full DAG execution (happy path)
def test_jev_engine_execute_success(tmp_path):
    db_file = tmp_path / "engine_run.db"
    ws = FakeWorkspace(mechanical_result=MechanicalCheckResult(passed=True))
    gk = FakeGatekeeper(
        verdict=ValidationVerdict(valid=True, probability=0.96),
        verify_verdict=ValidationVerdict(valid=True, probability=0.99),
    )

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        db_path=str(db_file),
    )

    final_state = engine.execute(ticket="Build calculator add()", thread_id="calc-1")
    assert final_state["status"] == "completed"
    assert final_state["gate_status"] == "verified"
    assert final_state["mechanical_strike_count"] == 0
    assert final_state["semantic_strike_count"] == 0
    assert ws.commit_call_count >= 1


# 8. Test JevEngine execute escalation on 3 strikes
def test_jev_engine_execute_escalation(tmp_path):
    db_file = tmp_path / "engine_escalate.db"
    ws = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="Broken syntax")
    )
    gk = FakeGatekeeper()

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        db_path=str(db_file),
    )

    final_state = engine.execute(ticket="Impossible build", thread_id="fail-1")
    assert final_state["status"] == "escalated"
    assert final_state["mechanical_strike_count"] == 3
    gk.escalate_deadlock.assert_called_once_with(
        trajectory=final_state["trajectory"],
        triggering_tier="mechanical",
        integration_branch="jev-ticket-fail-1",
    )


# 9. Test CLI main entrypoint
def test_main_cli_success(tmp_path, monkeypatch):
    db_file = tmp_path / "cli.db"
    monkeypatch.setenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
    monkeypatch.setenv("JEV_API_KEY", "test_key")

    with patch("main.Workspace") as mock_ws_cls, patch("main.Gatekeeper") as mock_gk_cls, patch("main.JevEngine") as mock_eng_cls:
        mock_ws = FakeWorkspace()
        mock_gk = FakeGatekeeper()
        mock_eng = MagicMock()
        mock_eng.execute.return_value = {"status": "completed", "gate_status": "verified"}

        mock_ws_cls.return_value = mock_ws
        mock_gk_cls.return_value = mock_gk
        mock_eng_cls.return_value = mock_eng

        exit_code = main.main(["Fix login bug", "--thread-id", "main-thread", "--db-path", str(db_file)])
        assert exit_code == 0
        mock_eng.execute.assert_called_once_with(ticket="Fix login bug", thread_id="main-thread")


# 10. Test SQLite checkpointer list(None) retrieves checkpoints across threads and namespaces
def test_sqlite_saver_list_all_threads(tmp_path):
    db_file = tmp_path / "test_list_all.db"
    saver = SqliteSaver.from_conn_string(str(db_file))

    # Add checkpoints across multiple threads and namespaces
    cfg1 = {"configurable": {"thread_id": "thread-A", "checkpoint_ns": ""}}
    saver.put(cfg1, {"v": 1, "id": "cp-A1", "ts": "2026-09-22T00:00:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}, {"source": "testA"}, {})

    cfg2 = {"configurable": {"thread_id": "thread-B", "checkpoint_ns": "ns1"}}
    saver.put(cfg2, {"v": 1, "id": "cp-B1", "ts": "2026-09-22T00:01:00Z", "channel_values": {}, "channel_versions": {}, "versions_seen": {}}, {"source": "testB"}, {})

    # list(None) should yield both checkpoints
    all_tuples = list(saver.list(None))
    assert len(all_tuples) == 2
    ids = {t.config["configurable"]["checkpoint_id"] for t in all_tuples}
    assert ids == {"cp-A1", "cp-B1"}

    # list({}) should also yield both checkpoints
    all_tuples_empty_dict = list(saver.list({}))
    assert len(all_tuples_empty_dict) == 2

    # list with metadata filter
    filtered = list(saver.list(None, filter={"source": "testA"}))
    assert len(filtered) == 1
    assert filtered[0].config["configurable"]["checkpoint_id"] == "cp-A1"


# 11. Test JevEngine execute escalation on 3 semantic strikes in full DAG
def test_jev_engine_execute_semantic_escalation(tmp_path):
    db_file = tmp_path / "engine_sem_escalate.db"
    ws = FakeWorkspace(mechanical_result=MechanicalCheckResult(passed=True))
    gk = FakeGatekeeper(
        verdict=ValidationVerdict(valid=False, probability=0.1, reason="Persistent semantic rejection")
    )

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        db_path=str(db_file),
    )

    final_state = engine.execute(ticket="Semantic failure ticket", thread_id="sem-fail-1")
    assert final_state["status"] == "escalated"
    assert final_state["semantic_strike_count"] == 3
    assert final_state["mechanical_strike_count"] == 0
    gk.escalate_deadlock.assert_called_once_with(
        trajectory=final_state["trajectory"],
        triggering_tier="semantic",
        integration_branch="jev-ticket-sem-fail-1",
    )


# 12. Test JevEngine execute escalation on verification failure
def test_jev_engine_execute_verification_failure(tmp_path):
    db_file = tmp_path / "engine_verify_fail.db"
    ws = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True),
        test_outcome=TestOutcome.FAILED,
    )
    gk = FakeGatekeeper(
        verdict=ValidationVerdict(valid=True, probability=0.95),
        verify_verdict=ValidationVerdict(valid=False, probability=0.05, reason="Final tests failed"),
    )

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        db_path=str(db_file),
    )

    final_state = engine.execute(ticket="Verify failure ticket", thread_id="ver-fail-1")
    assert final_state["status"] == "escalated"
    assert final_state["gate_status"] == "verification_failed"
    gk.escalate_deadlock.assert_called_once_with(
        trajectory=final_state["trajectory"],
        triggering_tier="verification",
        integration_branch="jev-ticket-ver-fail-1",
    )


# 13. Test CLI returns exit code 1 on escalation
def test_main_cli_escalation(tmp_path, monkeypatch):
    db_file = tmp_path / "cli_esc.db"
    monkeypatch.setenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
    monkeypatch.setenv("JEV_API_KEY", "test_key")

    with patch("main.Workspace") as mock_ws_cls, patch("main.Gatekeeper") as mock_gk_cls, patch("main.JevEngine") as mock_eng_cls:
        mock_ws = FakeWorkspace()
        mock_gk = FakeGatekeeper()
        mock_eng = MagicMock()
        mock_eng.execute.return_value = {"status": "escalated", "gate_status": "mechanical_failure"}

        mock_ws_cls.return_value = mock_ws
        mock_gk_cls.return_value = mock_gk
        mock_eng_cls.return_value = mock_eng

        exit_code = main.main(["Impossible bug", "--db-path", str(db_file)])
        assert exit_code == 1


# 14. Test route_gate branches
def test_route_gate_branches():
    # Strikes >= 3 escalate
    assert route_gate({"mechanical_strike_count": 3, "semantic_strike_count": 0}) == "escalate"
    assert route_gate({"mechanical_strike_count": 0, "semantic_strike_count": 3}) == "escalate"

    # Passed with remaining subgoals routes to implement
    state_next = {
        "gate_status": "passed",
        "plan_queue": [Subgoal(description="Sub 2")],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }
    assert route_gate(state_next) == "implement"

    # Passed with empty plan queue routes to verify
    state_done = {
        "gate_status": "passed",
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }
    assert route_gate(state_done) == "verify"

    # Failure with strikes < 3 retries implement
    state_retry = {
        "gate_status": "mechanical_failure",
        "mechanical_strike_count": 1,
        "semantic_strike_count": 0,
    }
    assert route_gate(state_retry) == "implement"


# 15. Test node_verify uses cumulative diff and context for untested pass
def test_node_verify_uses_cumulative_diff_and_untested_context():
    ws = MagicMock()
    ws.run_tests.return_value = TestOutcome.NO_TESTS_COLLECTED
    ws.get_cumulative_diff.return_value = "diff --git a/doc.ts b/doc.ts\n+/** docstring */"
    ws.get_staged_diff.return_value = ""

    gk = FakeGatekeeper(verify_verdict=ValidationVerdict(valid=True, probability=0.98))

    state = {
        "ticket": "Add docstring to func",
        "integration_branch": "jev-ticket-test",
        "trajectory": [
            {
                "node": "plan",
                "subgoals": [
                    {"description": "Add docstring", "scope": ["doc.ts"], "expects_tests": False}
                ],
            }
        ],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "completed"
    assert res["gate_status"] == "verified"
    gk.verify_ticket.assert_called_once()
    args, kwargs = gk.verify_ticket.call_args
    ticket_arg, diff_arg, test_out_arg = args
    assert ticket_arg == "Add docstring to func"
    assert diff_arg == "diff --git a/doc.ts b/doc.ts\n+/** docstring */"
    assert "Untested pass" in test_out_arg


# 16. Test node_verify escalation on failure
def test_node_verify_failure_escalates():
    ws = MagicMock()
    ws.run_tests.return_value = TestOutcome.PASSED
    ws.get_cumulative_diff.return_value = ""
    ws.get_staged_diff.return_value = ""

    gk = FakeGatekeeper(
        verify_verdict=ValidationVerdict(valid=False, probability=0.1, reason="Incomplete diff")
    )

    state = {
        "ticket": "Fix issue",
        "integration_branch": "jev-ticket-test",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "verification_failed"
    assert res["gate_status"] == "verification_failed"
    assert res["last_feedback"] == "Incomplete diff"
    gk.escalate_deadlock.assert_called_once()
    assert gk.escalate_deadlock.call_args[1]["triggering_tier"] == "verification"


# 17. Test node_verify catches unexpected exceptions and escalates
def test_node_verify_catches_exception_and_escalates():
    """Assert unexpected exception in node_verify (e.g. UnicodeDecodeError) is caught, recorded in feedback and trajectory, and escalated."""
    ws = MagicMock()
    ws.run_tests.side_effect = UnicodeDecodeError("cp1252", b"\xff", 0, 1, "invalid start byte")

    gk = MagicMock()
    state = {
        "ticket": "Fix issue",
        "integration_branch": "jev-ticket-test",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "escalated"
    assert res["gate_status"] == "verification_failed"
    assert "Verification error:" in res["last_feedback"]
    assert "UnicodeDecodeError" in res["last_feedback"] or "invalid start byte" in res["last_feedback"]
    assert len(res["trajectory"]) == 1
    assert res["trajectory"][0]["error_type"] == "verification_exception"
    gk.escalate_deadlock.assert_called_once()
    assert gk.escalate_deadlock.call_args[1]["triggering_tier"] == "verification"


# 18. Test node_verify forwards investigation_notes to gatekeeper.verify_ticket
def test_node_verify_passes_investigation_notes_to_gatekeeper():
    """Assert node_verify forwards state['investigation_notes'] to gatekeeper.verify_ticket."""
    ws = MagicMock()
    ws.run_tests.return_value = TestOutcome.PASSED
    ws.last_test_run = {"ran": True, "command": ["pytest"], "exit_code": 0, "outcome": "PASSED"}
    ws.get_cumulative_diff.return_value = "+x = 1\n"
    ws.get_staged_diff.return_value = "+x = 1\n"

    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.98)

    state = {
        "ticket": "Fix issue",
        "integration_branch": "jev-ticket-test",
        "investigation_notes": "Architecture notes for ticket",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "completed"
    assert res["gate_status"] == "verified"
    gk.verify_ticket.assert_called_once()
    _, kwargs = gk.verify_ticket.call_args
    assert kwargs.get("investigation_notes") == "Architecture notes for ticket"


# 19. Test node_verify records probability in trajectory and feedback on rejection
def test_node_verify_records_probability_in_trajectory_and_feedback():
    """Assert node_verify logs confidence score in feedback and trajectory."""
    ws = MagicMock()
    ws.run_tests.return_value = TestOutcome.NO_TESTS_COLLECTED
    ws.get_cumulative_diff.return_value = "+x = 1\n"
    ws.get_staged_diff.return_value = "+x = 1\n"

    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=False, probability=0.33, reason=None)

    state = {
        "ticket": "Add feature",
        "integration_branch": "jev-ticket-test",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "verification_failed"
    assert res["gate_status"] == "verification_failed"
    assert "0.33" in res["last_feedback"]
    assert len(res["trajectory"]) == 1
    assert res["trajectory"][0]["probability"] == 0.33


# 20. Test node_verify requires integration_branch and escalates if missing
def test_node_verify_requires_integration_branch_missing_escalates():
    """Assert node_verify without integration_branch fails and escalates without running verification on main."""
    ws = MagicMock()
    gk = MagicMock()
    state = {
        "ticket": "Test ticket",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)
    assert res["status"] == "escalated"
    assert res["gate_status"] == "verification_failed"
    assert "integration_branch is required" in res["last_feedback"]
    gk.escalate_deadlock.assert_called_once()


# 21. Test restart warning prints once, to stderr only
def test_restart_thread_warning_printed_once_to_stderr(tmp_path, capsys):
    ws = FakeWorkspace()
    gk = FakeGatekeeper()
    db_file = tmp_path / "restart_warn.db"

    engine = JevEngine(workspace=ws, gatekeeper=gk, db_path=str(db_file))
    # First execution creates checkpoints under thread
    engine.execute(ticket="Task 1", thread_id="reused-thread")
    capsys.readouterr()  # discard first run output

    # Second execution with same thread_id must print warning once, to stderr only
    engine.execute(ticket="Task 2", thread_id="reused-thread")
    captured = capsys.readouterr()

    warning_text = "Restarting thread from scratch and deleting prior integration branch (no resume)."
    assert warning_text in captured.err
    assert warning_text not in captured.out
    assert captured.err.count(warning_text) == 1


# 22. Test checkpoint Subgoal deserialization without warning and in strict msgpack mode
def test_checkpoint_subgoal_deserialization_no_warning_and_strict_msgpack(tmp_path, caplog, monkeypatch):
    import logging
    from jev.models import Subgoal
    from jev.engine import SqliteSaver
    import langgraph.checkpoint.serde._msgpack as _msgpack

    db_file = tmp_path / "serde_test.db"
    saver = SqliteSaver.from_conn_string(str(db_file))

    sg = Subgoal(id="sg-1", description="Test deserialization", scope=["foo.py"])
    config = {"configurable": {"thread_id": "thread-serde", "checkpoint_ns": ""}}
    checkpoint_data = {
        "v": 1,
        "id": "cp-serde-1",
        "ts": "2026-09-22T00:00:00Z",
        "channel_values": {
            "current_subgoal": sg,
            "plan_queue": [sg],
        },
        "channel_versions": {},
        "versions_seen": {},
    }

    saver.put(config, checkpoint_data, {"source": "test"}, {})

    # 1. Permissive loading: ensure no "Deserializing unregistered type" warning is logged
    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        caplog.clear()
        tup = saver.get_tuple(config)
        assert tup is not None
        loaded_sg = tup.checkpoint["channel_values"]["current_subgoal"]
        assert isinstance(loaded_sg, Subgoal)
        assert loaded_sg.id == "sg-1"
        assert "Deserializing unregistered type" not in caplog.text

    # 2. Strict loading with LANGGRAPH_STRICT_MSGPACK=true
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    monkeypatch.setattr(_msgpack, "STRICT_MSGPACK_ENABLED", True)
    saver_strict = SqliteSaver.from_conn_string(str(db_file))
    tup_strict = saver_strict.get_tuple(config)
    assert tup_strict is not None
    loaded_strict_sg = tup_strict.checkpoint["channel_values"]["current_subgoal"]
    assert isinstance(loaded_strict_sg, Subgoal)
    assert loaded_strict_sg.id == "sg-1"

    # 3. Subprocess verification with LANGGRAPH_STRICT_MSGPACK=true
    import subprocess
    env = os.environ.copy()
    env["LANGGRAPH_STRICT_MSGPACK"] = "true"
    sub_cmd = (
        f"import sys; sys.path.insert(0, 'src'); "
        f"from jev.engine import SqliteSaver; from jev.models import Subgoal; "
        f"saver = SqliteSaver.from_conn_string(r'{db_file}'); "
        f"tup = saver.get_tuple({config}); "
        f"assert isinstance(tup.checkpoint['channel_values']['current_subgoal'], Subgoal); "
        f"print('STRICT_OK')"
    )
    repo_root = Path(__file__).resolve().parent.parent
    sub_res = subprocess.run(
        [sys.executable, "-c", sub_cmd],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert sub_res.returncode == 0
    assert "STRICT_OK" in sub_res.stdout



