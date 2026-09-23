from unittest.mock import MagicMock

import pytest

from jev.engine import node_gate
from jev.models import (
    MechanicalCheckResult,
    Subgoal,
    ValidationVerdict,
)


class FakeWorkspace:
    def __init__(self, mechanical_result=None, staged_diff="diff --git a/foo.py b/foo.py\n+x = 1\n"):
        self.mechanical_result = mechanical_result or MechanicalCheckResult(passed=True)
        self.staged_diff = staged_diff
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


class FakeGatekeeper:
    def __init__(self, verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95)
        self.validate_subgoal = MagicMock(side_effect=self._validate_subgoal)
        self.escalate_deadlock = MagicMock()

    def _validate_subgoal(self, subgoal, diff, mechanical_detail=""):
        return self.verdict


@pytest.fixture
def base_state():
    return {
        "ticket": "Implement feature X",
        "current_subgoal": Subgoal(description="Add feature X", scope=["foo.py"], expects_tests=True),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }


# 13. test_mechanical_failure_increments_only_mechanical_counter
def test_mechanical_failure_increments_only_mechanical_counter(base_state):
    """Fake Workspace returns a failing MechanicalCheckResult; assert mechanical_strike_count += 1, semantic_strike_count unchanged, fake Gatekeeper's validate_subgoal was never called."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="SyntaxError")
    )
    gatekeeper = FakeGatekeeper()

    result_state = node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)

    assert result_state["mechanical_strike_count"] == 1
    assert result_state["semantic_strike_count"] == 0
    assert gatekeeper.validate_subgoal.call_count == 0


# 14. test_mechanical_failure_triggers_rollback
def test_mechanical_failure_triggers_rollback(base_state):
    """Assert rollback_subgoal() was called exactly once."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="tests", detail="Tests failed")
    )
    gatekeeper = FakeGatekeeper()

    node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)

    assert workspace.rollback_call_count == 1
    assert workspace.commit_call_count == 0


# 15. test_tier0_pass_invokes_tier1
def test_tier0_pass_invokes_tier1(base_state):
    """Fake Workspace returns a passing MechanicalCheckResult; assert gatekeeper.validate_subgoal() was called."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True)
    )
    gatekeeper = FakeGatekeeper(
        verdict=ValidationVerdict(valid=True, probability=0.98)
    )

    node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)

    assert gatekeeper.validate_subgoal.call_count == 1


# 16. test_semantic_rejection_increments_only_semantic_counter
def test_semantic_rejection_increments_only_semantic_counter(base_state):
    """Tier 0 passes, fake Gatekeeper returns Invalid; assert semantic_strike_count += 1, mechanical_strike_count unchanged."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True)
    )
    gatekeeper = FakeGatekeeper(
        verdict=ValidationVerdict(valid=False, probability=0.15, reason="Scope violation / missing logic")
    )

    result_state = node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)

    assert result_state["semantic_strike_count"] == 1
    assert result_state["mechanical_strike_count"] == 0
    assert workspace.rollback_call_count == 1
    assert workspace.commit_call_count == 0


# 17. test_successful_commit_resets_both_counters
def test_successful_commit_resets_both_counters(base_state):
    """Start with nonzero counters from a prior subgoal, Tier 0 and Tier 1 both pass; assert both counters reset to 0 and commit_subgoal() was called."""
    base_state["mechanical_strike_count"] = 2
    base_state["semantic_strike_count"] = 2

    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True)
    )
    gatekeeper = FakeGatekeeper(
        verdict=ValidationVerdict(valid=True, probability=0.99)
    )

    result_state = node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)

    assert result_state["mechanical_strike_count"] == 0
    assert result_state["semantic_strike_count"] == 0
    assert workspace.commit_call_count == 1
    assert workspace.rollback_call_count == 0


# 18. test_mechanical_escalation_fires_at_three_independent_of_semantic
def test_mechanical_escalation_fires_at_three_independent_of_semantic(base_state):
    """Drive mechanical_strike_count to 3 via repeated Tier 0 failures while semantic_strike_count stays at 0 throughout; assert escalate_deadlock(triggering_tier="mechanical") fires at exactly 3, not before."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="Build error")
    )
    gatekeeper = FakeGatekeeper()

    # Strike 1
    state1 = node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)
    assert state1["mechanical_strike_count"] == 1
    assert state1["semantic_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 0

    # Strike 2
    state2 = node_gate(state1, workspace=workspace, gatekeeper=gatekeeper)
    assert state2["mechanical_strike_count"] == 2
    assert state2["semantic_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 0

    # Strike 3
    state3 = node_gate(state2, workspace=workspace, gatekeeper=gatekeeper)
    assert state3["mechanical_strike_count"] == 3
    assert state3["semantic_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 1
    gatekeeper.escalate_deadlock.assert_called_once_with(
        trajectory=state3.get("trajectory", []),
        triggering_tier="mechanical"
    )


# 19. test_semantic_escalation_fires_at_three_independent_of_mechanical
def test_semantic_escalation_fires_at_three_independent_of_mechanical(base_state):
    """Mirror of 18 for the semantic counter."""
    workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True)
    )
    gatekeeper = FakeGatekeeper(
        verdict=ValidationVerdict(valid=False, probability=0.1, reason="Logic incorrect")
    )

    # Strike 1
    state1 = node_gate(base_state, workspace=workspace, gatekeeper=gatekeeper)
    assert state1["semantic_strike_count"] == 1
    assert state1["mechanical_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 0

    # Strike 2
    state2 = node_gate(state1, workspace=workspace, gatekeeper=gatekeeper)
    assert state2["semantic_strike_count"] == 2
    assert state2["mechanical_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 0

    # Strike 3
    state3 = node_gate(state2, workspace=workspace, gatekeeper=gatekeeper)
    assert state3["semantic_strike_count"] == 3
    assert state3["mechanical_strike_count"] == 0
    assert gatekeeper.escalate_deadlock.call_count == 1
    gatekeeper.escalate_deadlock.assert_called_once_with(
        trajectory=state3.get("trajectory", []),
        triggering_tier="semantic"
    )


# 20. test_interleaved_strikes_do_not_cross_contaminate
def test_interleaved_strikes_do_not_cross_contaminate(base_state):
    """2 mechanical failures, then 2 semantic rejections, then 1 more mechanical failure (3rd mechanical strike); assert escalation fires on the mechanical counter reaching 3, and that the semantic counter sitting at 2 never contributed to it."""
    mech_fail_workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=False, failed_check="build", detail="Build error")
    )
    mech_pass_workspace = FakeWorkspace(
        mechanical_result=MechanicalCheckResult(passed=True)
    )

    gatekeeper_reject = FakeGatekeeper(
        verdict=ValidationVerdict(valid=False, probability=0.1, reason="Rejected")
    )

    state = base_state

    # 2 mechanical failures
    state = node_gate(state, workspace=mech_fail_workspace, gatekeeper=gatekeeper_reject)
    assert state["mechanical_strike_count"] == 1
    assert state["semantic_strike_count"] == 0
    assert gatekeeper_reject.escalate_deadlock.call_count == 0

    state = node_gate(state, workspace=mech_fail_workspace, gatekeeper=gatekeeper_reject)
    assert state["mechanical_strike_count"] == 2
    assert state["semantic_strike_count"] == 0
    assert gatekeeper_reject.escalate_deadlock.call_count == 0

    # 2 semantic rejections
    state = node_gate(state, workspace=mech_pass_workspace, gatekeeper=gatekeeper_reject)
    assert state["mechanical_strike_count"] == 2
    assert state["semantic_strike_count"] == 1
    assert gatekeeper_reject.escalate_deadlock.call_count == 0

    state = node_gate(state, workspace=mech_pass_workspace, gatekeeper=gatekeeper_reject)
    assert state["mechanical_strike_count"] == 2
    assert state["semantic_strike_count"] == 2
    assert gatekeeper_reject.escalate_deadlock.call_count == 0

    # 1 more mechanical failure (3rd mechanical strike)
    state = node_gate(state, workspace=mech_fail_workspace, gatekeeper=gatekeeper_reject)
    assert state["mechanical_strike_count"] == 3
    assert state["semantic_strike_count"] == 2
    assert gatekeeper_reject.escalate_deadlock.call_count == 1
    gatekeeper_reject.escalate_deadlock.assert_called_once_with(
        trajectory=state.get("trajectory", []),
        triggering_tier="mechanical"
    )
