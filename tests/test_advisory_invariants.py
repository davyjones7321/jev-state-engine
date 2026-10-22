from unittest.mock import MagicMock
import pytest

from jev.engine import node_gate
from jev.advisory import AdvisoryErrorCode, AdvisoryResult, RepairBrief
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

    def run_mechanical_checks(self, subgoal, **kwargs):
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
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, **kw: self.verdict)
        self.escalate_deadlock = MagicMock()


class FakeAdvisor:
    def __init__(self, result=None):
        self.result = result or AdvisoryResult(
            ok=True, brief=RepairBrief(summary="Advisor Suggestion", cause="Hypothesis", actions=["Step 1"])
        )
        self.calls = []

    def request_repair_brief(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.result


def test_advisor_on_vs_off_invariants_pass():
    """Gate 8 Invariant: Passing subgoal produces identical gate status and state with advisor on or off."""
    subgoal = Subgoal(description="Add logic", scope=["foo.py"], expects_tests=False)
    state_off = {
        "ticket": "Implement feature",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "retry_intel": "off",
    }
    state_on = dict(state_off)

    ws_off = FakeWorkspace()
    gk_off = FakeGatekeeper(verdict=ValidationVerdict(valid=True, probability=0.95))
    res_off = node_gate(state_off, workspace=ws_off, gatekeeper=gk_off, advisory=None)

    ws_on = FakeWorkspace()
    gk_on = FakeGatekeeper(verdict=ValidationVerdict(valid=True, probability=0.95))
    advisor = FakeAdvisor()
    res_on = node_gate(state_on, workspace=ws_on, gatekeeper=gk_on, advisory=advisor)

    assert res_off["gate_status"] == res_on["gate_status"] == "passed"
    assert res_off["mechanical_strike_count"] == res_on["mechanical_strike_count"] == 0
    assert res_off["semantic_strike_count"] == res_on["semantic_strike_count"] == 0
    assert ws_off.commit_call_count == ws_on.commit_call_count == 1
    assert ws_off.rollback_call_count == ws_on.rollback_call_count == 0
    assert gk_off.validate_subgoal.call_count == gk_on.validate_subgoal.call_count == 1


def test_advisor_on_vs_off_invariants_mechanical_failure():
    """Gate 8 Invariant: Mechanical failure produces identical strike counts and verdicts whether advisor is present, timing out, or failing."""
    subgoal = Subgoal(description="Add logic", scope=["foo.py"], expects_tests=False)
    fail_mech = MechanicalCheckResult(passed=False, failed_check="build", detail="SyntaxError")

    state_off = {
        "ticket": "Implement feature",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "retry_intel": "off",
    }
    state_on = dict(state_off)
    state_err = dict(state_off)

    ws_off = FakeWorkspace(mechanical_result=fail_mech)
    gk_off = FakeGatekeeper()
    res_off = node_gate(state_off, workspace=ws_off, gatekeeper=gk_off, advisory=None)

    ws_on = FakeWorkspace(mechanical_result=fail_mech)
    gk_on = FakeGatekeeper()
    advisor_ok = FakeAdvisor()
    res_on = node_gate(state_on, workspace=ws_on, gatekeeper=gk_on, advisory=advisor_ok)

    ws_err = FakeWorkspace(mechanical_result=fail_mech)
    gk_err = FakeGatekeeper()
    advisor_err = FakeAdvisor(result=AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.TIMEOUT, error_message="timeout"))
    res_err = node_gate(state_err, workspace=ws_err, gatekeeper=gk_err, advisory=advisor_err)

    for res in [res_off, res_on, res_err]:
        assert res["gate_status"] == "mechanical_failure"
        assert res["mechanical_strike_count"] == 1
        assert res["semantic_strike_count"] == 0

    assert ws_off.rollback_call_count == ws_on.rollback_call_count == ws_err.rollback_call_count == 1
    assert gk_off.validate_subgoal.call_count == gk_on.validate_subgoal.call_count == gk_err.validate_subgoal.call_count == 0


def test_advisor_on_vs_off_invariants_semantic_failure():
    """Gate 8 Invariant: Semantic rejection produces identical strike counts and verdicts regardless of advisor guidance."""
    subgoal = Subgoal(description="Add logic", scope=["foo.py"], expects_tests=False)
    semantic_rej = ValidationVerdict(valid=False, probability=0.1, reason="Missing logic")

    state_off = {
        "ticket": "Implement feature",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "retry_intel": "off",
    }
    state_on = dict(state_off)

    ws_off = FakeWorkspace()
    gk_off = FakeGatekeeper(verdict=semantic_rej)
    res_off = node_gate(state_off, workspace=ws_off, gatekeeper=gk_off, advisory=None)

    ws_on = FakeWorkspace()
    gk_on = FakeGatekeeper(verdict=semantic_rej)
    # Even if advisor claims it's fine, Jev's semantic verdict is the sole authority!
    advisor = FakeAdvisor(result=AdvisoryResult(ok=True, brief=RepairBrief(summary="Looks good to me", cause="unknown", actions=["proceed"])))
    res_on = node_gate(state_on, workspace=ws_on, gatekeeper=gk_on, advisory=advisor)

    assert res_off["gate_status"] == res_on["gate_status"] == "semantic_failure"
    assert res_off["mechanical_strike_count"] == res_on["mechanical_strike_count"] == 0
    assert res_off["semantic_strike_count"] == res_on["semantic_strike_count"] == 1
    assert ws_off.rollback_call_count == ws_on.rollback_call_count == 1
    assert gk_off.validate_subgoal.call_count == gk_on.validate_subgoal.call_count == 1
