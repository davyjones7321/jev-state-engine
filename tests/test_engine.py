import sqlite3
import sys
from pathlib import Path
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

    def run_mechanical_checks(self, subgoal):
        return self.mechanical_result

    def get_staged_diff(self):
        return self.staged_diff

    def rollback_subgoal(self):
        self.rollback_call_count += 1

    def commit_subgoal(self):
        self.commit_call_count += 1

    def run_read_tool(self, cmd, args=None):
        return "M file.py"

    def run_tests(self):
        return self.test_outcome

    def stage_file_mutation(self, path, content):
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
    state: State = {"ticket": "Implement feature Y", "trajectory": []}
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

        exit_code = main.main(["Fix login bug", "--db-path", str(db_file)])
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

