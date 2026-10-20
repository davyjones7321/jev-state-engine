from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jev.models import Subgoal
from jev.replanning import replan_remaining_queue, should_replan


def _state(**overrides):
    state = {
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "replan_count": 0,
        "plan_queue": [Subgoal(id="queued", description="old work", scope=["src/old.py"])],
        "committed_subgoals": [Subgoal(id="done", description="committed", scope=["src/done.py"], status="committed")],
    }
    state.update(overrides)
    return state


def test_no_replan_before_retry_threshold():
    state = _state(mechanical_strike_count=1)
    called = False

    def planner(_evidence, _remaining):
        nonlocal called
        called = True
        return []

    result = replan_remaining_queue(
        state, {"failure_class": "compile"}, planner, validate_scope=lambda _: None
    )
    assert result.replanned is False
    assert result.reason == "evidence_threshold_not_reached"
    assert called is False


def test_dead_end_allows_one_replan_and_prunes_only_remaining_queue():
    state = _state(failed_attempts=[{}, {}])
    result = replan_remaining_queue(
        state,
        {"failure_class": "duplicate_diff", "dead_end": True},
        lambda _evidence, _remaining: [{"id": "replacement", "description": "new work", "scope": ["src/new.py"]}],
        validate_scope=lambda _: None,
    )
    assert result.replanned is True
    assert state["plan_queue"][0].id == "replacement"
    assert state["pruned_subgoals"][0].status == "pruned"
    assert state["committed_subgoals"][0].status == "committed"
    assert state["replan_count"] == 1


def test_replan_count_cap_is_enforced():
    state = _state(mechanical_strike_count=2, replan_count=1)
    result = replan_remaining_queue(
        state, {"failure_class": "tests"}, lambda *_: [], validate_scope=lambda _: None
    )
    assert result.replanned is False
    assert result.reason == "replan_budget_exhausted"


def test_scope_validation_rejects_replacement_without_mutating_queue():
    state = _state(mechanical_strike_count=2)

    def validate_scope(subgoals):
        raise ValueError(f"ungrounded: {subgoals[0].scope[0]}")

    result = replan_remaining_queue(
        state,
        {"failure_class": "semantic_rejection"},
        lambda *_: [{"description": "escape scope", "scope": ["src/unrelated.py"]}],
        validate_scope=validate_scope,
    )
    assert result.replanned is False
    assert result.reason == "invalid_replan_output"
    assert "ungrounded" in (result.error or "")
    assert state["plan_queue"][0].id == "queued"


def test_malformed_planner_output_is_rejected():
    state = _state(mechanical_strike_count=2)
    result = replan_remaining_queue(
        state, {"failure_class": "compile"}, lambda *_: "not-json-plan", validate_scope=lambda _: None
    )
    assert result.replanned is False
    assert result.reason == "invalid_replan_output"
    assert state["replan_count"] == 0


def test_replan_subgoal_count_is_bounded():
    state = _state(mechanical_strike_count=2)
    too_many = [
        {"description": f"work {i}", "scope": [f"src/{i}.py"]}
        for i in range(5)
    ]
    result = replan_remaining_queue(
        state, {"failure_class": "queue_shape"}, lambda *_: too_many, validate_scope=lambda _: None
    )
    assert result.replanned is False
    assert result.reason == "invalid_replan_output"
    assert "cap" in (result.error or "")


def test_should_replan_requires_failure_evidence_for_threshold_trigger():
    assert should_replan(_state(mechanical_strike_count=2), {}) is False
    assert should_replan(_state(mechanical_strike_count=2), {"failure_class": "tests"}) is True
