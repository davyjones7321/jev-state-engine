from unittest.mock import MagicMock

from jev.engine import JevEngine, _material_diff_sha
from jev.models import MechanicalCheckResult, Subgoal, ValidationVerdict


class _Workspace:
    def __init__(self, result):
        self.result = result
        self.rollback_count = 0
        self.commit_count = 0

    def get_staged_diff(self):
        return "diff --git a/foo.py b/foo.py\n+value = 1\n"

    def run_mechanical_checks(self, subgoal):
        return self.result

    def rollback_subgoal(self):
        self.rollback_count += 1

    def commit_subgoal(self):
        self.commit_count += 1


def _state(**overrides):
    state = {
        "ticket": "Add feature",
        "current_subgoal": Subgoal(description="change foo", scope=["foo.py"]),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    state.update(overrides)
    return state


def _engine(workspace, memory=None, **kwargs):
    gatekeeper = MagicMock()
    gatekeeper.validate_subgoal.return_value = ValidationVerdict(valid=True)
    gatekeeper.escalate_deadlock = MagicMock()
    return JevEngine(
        workspace=workspace,
        gatekeeper=gatekeeper,
        db_path=None,
        memory=memory,
        **kwargs,
    )


def test_engine_memory_records_completed_failed_and_passed_attempts():
    memory = MagicMock()
    engine = _engine(_Workspace(MechanicalCheckResult(passed=False, failed_check="tests", detail="failed")), memory)

    failed = engine.node_gate(_state())
    assert failed["mechanical_strike_count"] == 1
    memory.write.assert_called_once()
    assert memory.write.call_args.kwargs["outcome"] == "mechanical_failure"

    memory.reset_mock()
    passing_engine = _engine(_Workspace(MechanicalCheckResult(passed=True)), memory)
    passed = passing_engine.node_gate(_state())
    assert passed["gate_status"] == "passed"
    memory.write.assert_called_once()
    assert memory.write.call_args.kwargs["outcome"] == "passed"


def test_engine_replanning_is_only_invoked_for_retry_dead_end_and_preserves_committed():
    planner = MagicMock(return_value=[{"description": "new work", "scope": ["new.py"]}])
    validator = MagicMock()
    engine = _engine(
        _Workspace(MechanicalCheckResult(passed=False, failed_check="tests", detail="failed")),
        replan_enabled=True,
        replan_planner=planner,
        replan_validator=validator,
    )
    committed = Subgoal(description="already done", scope=["done.py"], status="committed")
    state = _state(
        mechanical_strike_count=2,
        retry_intel="on",
        failed_attempts=[{
            "attempt_no": 1,
            "failure_class": "tests",
            "failed_check": "tests",
            "diff_sha": "old",
            "retry_metadata": {
                "failure_class": "tests",
                "read_evidence": [],
                "hypothesis": "same",
                "material_diff_sha": _material_diff_sha("diff --git a/foo.py b/foo.py\n+value = 1\n"),
                "validation_results": [],
            },
            "failure_evidence": {"failure_class": "tests"},
        }],
        committed_subgoals=[committed],
        plan_queue=[Subgoal(description="remaining", scope=["old.py"])],
        trajectory=[{"node": "implement", "hypothesis": "same", "tool_calls": []}],
    )

    result = engine.node_gate(state)

    assert planner.call_count == 1
    validator.assert_called_once()
    assert result["gate_status"] == "replanned"
    assert result["status"] is None
    assert result["committed_subgoals"] == [committed]
    assert result["plan_queue"][0].description == "new work"


def test_memory_and_replanning_failures_cannot_change_gate_authority():
    memory = MagicMock()
    memory.write.side_effect = RuntimeError("memory offline")
    engine = _engine(
        _Workspace(MechanicalCheckResult(passed=False, failed_check="tests", detail="failed")),
        memory,
        replan_enabled=True,
        replan_planner=MagicMock(side_effect=RuntimeError("planner offline")),
        replan_validator=MagicMock(),
    )

    state = _state(
        retry_intel="on",
        failed_attempts=[{"attempt_no": 1, "failure_class": "tests", "failed_check": "tests",
                         "diff_sha": "old", "retry_metadata": {"failure_class": "tests",
                         "read_evidence": [], "hypothesis": "same", "material_diff_sha": _material_diff_sha("diff --git a/foo.py b/foo.py\n+value = 1\n"),
                         "validation_results": []}}],
        trajectory=[{"node": "implement", "hypothesis": "same", "tool_calls": []}],
    )
    result = engine.node_gate(state)

    assert result["gate_status"] == "retry_dead_end"
    assert result["status"] == "escalated"
    assert result["semantic_strike_count"] == 0
