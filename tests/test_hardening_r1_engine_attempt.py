"""tests/test_hardening_r1_engine_attempt.py

Verification of attempt derivation inside node_gate for Phase R1.
Asserts:
1. The attempt kwarg passed to workspace.run_mechanical_checks is 1, 2, 3 for:
   - FAIL, FAIL, FAIL
   - UNMEASURED x3
   - FAIL, UNMEASURED, FAIL
   - UNMEASURED, FAIL, UNMEASURED
2. Two calls with the same state (a true duplicate request) do not skip the run silently.
"""

from unittest.mock import MagicMock
import pytest

from jev.engine import node_gate
from jev.mech_evidence import MechanicalEvidence, MechReason, MechStatus
from jev.models import State, Subgoal
from jev.workspace import MechanicalCheckResult


def create_gate_state(subgoal_id="sg1") -> State:
    sg = Subgoal(id=subgoal_id, description="test subgoal", scope=["src/a.py"], expects_tests=True)
    state: State = {
        "ticket": "Implement feature",
        "current_subgoal": sg,
        "plan_queue": [sg],
        "trajectory": [],
        "failed_attempts": [],
        "mechanical_strike_count": 0,
        "unmeasured_retry_count": 0,
        "semantic_strike_count": 0,
        "thread_id": "thread-test-attempt",
        "test_policy": "auto",
        "retry_intel": "heuristic",
    }
    return state


def setup_mock_workspace(outcomes):
    ws = MagicMock()
    diff_idx = [0]

    def fake_diff(*args, **kwargs):
        diff_idx[0] += 1
        return f"diff --git a/file{diff_idx[0]}.py b/file{diff_idx[0]}.py\n+line{diff_idx[0]}\n"

    ws.get_staged_diff.side_effect = fake_diff
    ws.events = []
    ws.last_test_run = None
    ws.last_compile_run = None
    ws.run_id = "test-run-attempt"

    attempts_recorded = []
    outcome_iter = iter(outcomes)

    def fake_run_mech(subgoal, **kwargs):
        attempts_recorded.append(kwargs.get("attempt"))
        out = next(outcome_iter)
        if out == "FAIL":
            return MechanicalCheckResult(
                passed=False,
                failed_check="tests",
                detail="pytest test failure",
                test_runner_outcome={"outcome": "FAILED", "ran": True},
            )
        elif out == "UNMEASURED":
            mech_ev = MechanicalEvidence(
                mech_status=MechStatus.UNMEASURED,
                reason=MechReason.TIMEOUT,
            )
            return MechanicalCheckResult(
                passed=False,
                failed_check="tests_unmeasured",
                detail="test command timed out",
                mech_evidence=mech_ev,
                test_runner_outcome={"outcome": "TIMEOUT", "ran": True},
            )
        else:
            return MechanicalCheckResult(passed=True, failed_check=None)

    ws.run_mechanical_checks.side_effect = fake_run_mech
    return ws, attempts_recorded


@pytest.mark.parametrize(
    "sequence",
    [
        ["FAIL", "FAIL", "FAIL"],
        ["UNMEASURED", "UNMEASURED", "UNMEASURED"],
        ["FAIL", "UNMEASURED", "FAIL"],
        ["UNMEASURED", "FAIL", "UNMEASURED"],
    ],
    ids=["fail_fail_fail", "unmeasured_x3", "fail_unmeasured_fail", "unmeasured_fail_unmeasured"],
)
def test_node_gate_attempt_derivation_sequence(sequence):
    """Calling node_gate 3 times in a row passes attempt 1, 2, 3 to run_mechanical_checks."""
    state = create_gate_state()
    ws, attempts_recorded = setup_mock_workspace(sequence)

    for _ in range(3):
        state = node_gate(state, workspace=ws)

    assert attempts_recorded == [1, 2, 3], f"Expected attempts [1, 2, 3], got {attempts_recorded}"


def test_node_gate_duplicate_request_same_state_does_not_skip_run():
    """Two calls to node_gate with the same initial state execute run_mechanical_checks and do not skip silently."""
    state1 = create_gate_state()
    # Deep copy state for a second identical call
    state2 = create_gate_state()

    ws, attempts_recorded = setup_mock_workspace(["FAIL", "FAIL"])

    # Call 1
    node_gate(state1, workspace=ws)
    # Call 2 with identical duplicate state
    node_gate(state2, workspace=ws)

    assert ws.run_mechanical_checks.call_count == 2
    assert attempts_recorded == [1, 1], f"Expected [1, 1] for duplicate state requests, got {attempts_recorded}"
