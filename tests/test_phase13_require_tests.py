import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
from jev.mech_evidence import (
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)
from jev.engine import JevEngine, node_gate, node_verify


@pytest.mark.parametrize("reason", [
    MechReason.NO_COMMAND,
    MechReason.BASELINE_UNAVAILABLE,
    MechReason.FLAKY,
    MechReason.TOOLING_ERROR,
    MechReason.NO_TEST_EXECUTION_SIGNAL,
    MechReason.ENGINE_WORKER_PATH_COLLISION,
])
def test_require_tests_off_unmeasured_jev_approves_merges(reason):
    """Under require_tests off (default), an UNMEASURED candidate reaches Jev Gatekeeper.
    If Jev approves (valid=True), the subgoal merges, strikes remain 0, and the subgoal
    is recorded in mechanically_unverified_subgoals with unv_kind 'unmeasured'.
    """
    subgoal = Subgoal(id="sg-1", description="subgoal 1", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=reason,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail=f"Unmeasured failure: {reason.value}",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={
            "build": {"ran": True, "passed": True},
            "compile": {"ran": True, "passed": True},
            "tests": {"ran": True, "passed": False},
        },
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+candidate diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.merge_subgoal_worktree = MagicMock()
    ws.get_branch_commit.return_value = "sha-commit-1"

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    state: State = {
        "ticket": "Ticket rollout",
        "current_subgoal": subgoal,
        "require_tests": "off",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-1",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "committed_subgoals": [],
        "mechanically_unverified_subgoals": [],
        "mechanically_unverified_kinds": {},
        "trajectory": [],
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)

    assert out.get("gate_status") == "passed"
    assert out.get("mechanical_strike_count") == 0
    assert out.get("semantic_strike_count") == 0
    ws.merge_subgoal_worktree.assert_called_once()
    assert any(sg.id == "sg-1" for sg in out.get("committed_subgoals", []))
    assert "sg-1" in out.get("mechanically_unverified_subgoals", [])
    assert out.get("mechanically_unverified_kinds", {}).get("sg-1") == "unmeasured"


def test_require_tests_off_unmeasured_jev_rejects():
    """Under require_tests off, an UNMEASURED candidate reaches Jev Gatekeeper.
    If Jev rejects (valid=False), semantic_strike increments, mechanical_strike is 0,
    the candidate is not merged, and the worktree is discarded.
    """
    subgoal = Subgoal(id="sg-rej", description="subgoal to reject", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TOOLING_ERROR,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="Tooling error",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={
            "build": {"ran": True, "passed": True},
            "compile": {"ran": True, "passed": True},
            "tests": {"ran": True, "passed": False},
        },
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+candidate diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.merge_subgoal_worktree = MagicMock()
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=False, reason="Bad implementation")

    state: State = {
        "ticket": "Ticket rollout",
        "current_subgoal": subgoal,
        "require_tests": "off",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-rej",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "committed_subgoals": [],
        "mechanically_unverified_subgoals": [],
        "mechanically_unverified_kinds": {},
        "trajectory": [],
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)

    assert out.get("gate_status") == "semantic_failure"
    assert out.get("semantic_strike_count") == 1
    assert out.get("mechanical_strike_count") == 0
    ws.merge_subgoal_worktree.assert_not_called()
    ws.discard_subgoal_worktree.assert_called_once()
    assert out.get("current_worktree_path") is None


def test_require_tests_on_unmeasured_retries_and_escalates():
    """Under require_tests on, UNMEASURED candidates do not reach Jev.
    Attempts 1 and 2 produce gate_status 'unmeasured' with unmeasured_retry_count incrementing.
    Attempt 3 escalates with gate_status 'verification_inconclusive' and gatekeeper is never called.
    """
    subgoal = Subgoal(id="sg-retry", description="subgoal retried", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TIMEOUT,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="Test execution timed out",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={
            "build": {"ran": True, "passed": True},
            "compile": {"ran": True, "passed": True},
            "tests": {"ran": True, "passed": False},
        },
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+candidate diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()

    # Attempt 1 -> unmeasured_retry_count = 1
    state1: State = {
        "ticket": "Ticket strict",
        "current_subgoal": subgoal,
        "require_tests": "on",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-retry",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }
    out1 = node_gate(state1, workspace=ws, gatekeeper=gk)
    assert out1.get("gate_status") == "unmeasured"
    assert out1.get("unmeasured_retry_count") == 1
    gk.validate_subgoal.assert_not_called()

    # Attempt 2 -> unmeasured_retry_count = 2
    state2: State = {
        **out1,
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-retry",
    }
    out2 = node_gate(state2, workspace=ws, gatekeeper=gk)
    assert out2.get("gate_status") == "unmeasured"
    assert out2.get("unmeasured_retry_count") == 2
    gk.validate_subgoal.assert_not_called()

    # Attempt 3 -> escalation verification_inconclusive
    state3: State = {
        **out2,
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-retry",
    }
    out3 = node_gate(state3, workspace=ws, gatekeeper=gk)
    assert out3.get("status") == "escalated"
    assert out3.get("gate_status") == "verification_inconclusive"
    assert out3.get("unmeasured_retry_count") == 3
    gk.validate_subgoal.assert_not_called()


def test_missing_require_tests_key_behaves_as_strict():
    """When state['require_tests'] is missing or None, node_gate treats it as strict
    ('require_tests_strict or require_tests is None').
    This maintains backward compatibility with older test suites and direct node_gate
    invocations that do not populate rollout toggles in raw state dictionaries.
    """
    subgoal = Subgoal(id="sg-none", description="subgoal with missing require_tests", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.NO_COMMAND,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="No command configured",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={
            "build": {"ran": True, "passed": True},
            "compile": {"ran": True, "passed": True},
            "tests": {"ran": True, "passed": False},
        },
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()

    # state lacks "require_tests"
    state: State = {
        "ticket": "Ticket legacy",
        "current_subgoal": subgoal,
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-none",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }
    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out.get("gate_status") == "unmeasured"
    assert out.get("unmeasured_retry_count") == 1
    gk.validate_subgoal.assert_not_called()


@pytest.mark.parametrize("reason", [MechReason.MISSING_TOOL, MechReason.ENVIRONMENT_UNAVAILABLE])
@pytest.mark.parametrize("require_tests", ["off", "on"])
def test_missing_tool_and_environment_unavailable_escalate_immediately(reason, require_tests):
    """MISSING_TOOL and ENVIRONMENT_UNAVAILABLE result in failed_check='env_not_ready',
    which escalates immediately without consuming worker strikes and without calling Gatekeeper,
    regardless of whether require_tests is 'off' or 'on'.
    """
    subgoal = Subgoal(id="sg-env", description="env missing subgoal", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=reason,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="env_not_ready",
        detail=f"Environment unavailable: {reason.value}",
        mech_evidence=ev,
        checks_run=["build", "compile"],
        checks={"build": {"ran": True, "passed": False}},
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()

    state: State = {
        "ticket": "Ticket env check",
        "current_subgoal": subgoal,
        "require_tests": require_tests,
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-env",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out.get("status") == "escalated"
    assert out.get("gate_status") == "env_not_ready"
    assert out.get("mechanical_strike_count") == 0
    assert out.get("semantic_strike_count") == 0
    gk.validate_subgoal.assert_not_called()


def test_node_verify_require_tests_off_lets_ticket_verify_and_keeps_telemetry():
    """node_verify with require_tests off lets a ticket verify without tests (or when tests
    are unmeasured/failed) and preserves the mechanically_unverified_subgoals telemetry.
    """
    ws = MagicMock()
    ws.get_impacted_test_files.return_value = []
    ws.get_test_files.return_value = []
    ws.get_staged_diff.return_value = ""
    ws.get_cumulative_diff.return_value = ""
    ws.check_compile.return_value = None
    ws.run_tests.return_value = None
    ws.last_test_run = None
    ws.last_compile_run = None
    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.98)

    state: State = {
        "ticket": "Ticket verify off",
        "require_tests": "off",
        "integration_branch": "main",
        "committed_subgoals": [Subgoal(id="sg-unv", description="unverified sg", expects_tests=False)],
        "mechanically_unverified_subgoals": ["sg-unv"],
        "trajectory": [],
    }

    out = node_verify(state, workspace=ws, gatekeeper=gk)
    assert out.get("gate_status") == "verified"
    assert out.get("status") == "completed"
    assert "sg-unv" in out.get("mechanically_unverified_subgoals", [])


def test_node_verify_require_tests_on_escalates_when_tests_unmeasured_or_not_passed():
    """node_verify with strict require_tests on escalates with verification_failed
    if tests did not run or outcome is not PASSED.
    """
    ws = MagicMock()
    ws.run_post_merge_verification.return_value = {
        "success": False,
        "outcome": TestOutcome.FAILED.value,
        "tests_ran": False,
        "output": "no tests ran",
    }
    gk = MagicMock()

    state: State = {
        "ticket": "Ticket verify on",
        "require_tests": "on",
        "integration_branch": "main",
        "committed_subgoals": [],
        "mechanically_unverified_subgoals": [],
        "trajectory": [],
    }

    out = node_verify(state, workspace=ws, gatekeeper=gk)
    assert out.get("gate_status") == "verification_failed"
    assert out.get("status") == "escalated"
    assert "Verification failed: strict --require-tests is enabled" in out.get("last_feedback", "")


def test_jev_engine_defaults_to_require_tests_off():
    """JevEngine initializes with require_tests='off' by default, and creates
    initial_state with require_tests='off'.
    """
    ws = MagicMock()
    ws.repo_dir = Path("/tmp/mock_repo")
    engine = JevEngine(workspace=ws, gatekeeper=MagicMock())
    assert engine.require_tests == "off"

    with patch.object(engine.app, "invoke") as mock_invoke:
        mock_invoke.return_value = {"trajectory": []}
        engine.execute("Ticket test", "thread-1")
        initial_state = mock_invoke.call_args[0][0]
        assert initial_state.get("require_tests") == "off"


# --- Step 2 Tests: Sensitive UNMEASURED outcomes must not merge on Jev's word alone ---

@pytest.mark.parametrize("reason", [
    MechReason.TEST_SURFACE_CHANGED,
    MechReason.COMMAND_IDENTITY_CHANGED,
])
def test_declared_test_or_recipe_edit_requires_human_approval_when_require_tests_off(reason):
    """(Step 2a) A declared test/recipe edit (mech_ev.verifier_modified=True) must NOT be merged
    by Jev when require_tests is off. It must escalate with gate_status='human_approval_required',
    status='escalated', tear down worktree, and never call gatekeeper to validate or merge.
    """
    subgoal = Subgoal(id="sg-sensitive", description="declared test modification", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=reason,
        evidence_level=EvidenceLevel.NONE,
        verifier_modified=True,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="test_surface_changed" if reason == MechReason.TEST_SURFACE_CHANGED else "tests",
        detail=f"Declared test change: {reason.value}",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+modified test code\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.merge_subgoal_worktree = MagicMock()
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    state: State = {
        "ticket": "Ticket sensitive edit",
        "current_subgoal": subgoal,
        "require_tests": "off",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-sg-sensitive",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "committed_subgoals": [],
        "mechanically_unverified_subgoals": [],
        "trajectory": [],
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)

    assert out.get("status") == "escalated"
    assert out.get("gate_status") == "human_approval_required"
    assert "human approval required" in out.get("last_feedback", "").lower()
    gk.validate_subgoal.assert_not_called()
    ws.merge_subgoal_worktree.assert_not_called()
    ws.discard_subgoal_worktree.assert_called_once()
    assert out.get("current_worktree_path") is None


def test_timeout_takes_strict_retry_path_even_when_require_tests_off():
    """(Step 2b) TIMEOUT must not merge on Jev's word alone under require_tests off;
    it must take the strict retry path (retries up to 3, then verification_inconclusive).
    """
    subgoal = Subgoal(id="sg-timeout", description="timeout subgoal", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TIMEOUT,
        evidence_level=EvidenceLevel.NONE,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="tests_unmeasured",
        detail="Command timed out after 30s",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.merge_subgoal_worktree = MagicMock()
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True)

    # Attempt 1: retry with gate_status='unmeasured'
    state1: State = {
        "ticket": "Ticket timeout",
        "current_subgoal": subgoal,
        "require_tests": "off",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-timeout",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }
    out1 = node_gate(state1, workspace=ws, gatekeeper=gk)
    assert out1.get("gate_status") == "unmeasured"
    assert out1.get("unmeasured_retry_count") == 1
    gk.validate_subgoal.assert_not_called()
    ws.merge_subgoal_worktree.assert_not_called()

    # Attempt 2
    state2: State = {**out1, "current_worktree_path": "/tmp/mock_wt", "current_worktree_branch": "jev-subgoal-timeout"}
    out2 = node_gate(state2, workspace=ws, gatekeeper=gk)
    assert out2.get("gate_status") == "unmeasured"
    assert out2.get("unmeasured_retry_count") == 2
    gk.validate_subgoal.assert_not_called()

    # Attempt 3: escalates with verification_inconclusive
    state3: State = {**out2, "current_worktree_path": "/tmp/mock_wt", "current_worktree_branch": "jev-subgoal-timeout"}
    out3 = node_gate(state3, workspace=ws, gatekeeper=gk)
    assert out3.get("status") == "escalated"
    assert out3.get("gate_status") == "verification_inconclusive"
    assert out3.get("unmeasured_retry_count") == 3
    gk.validate_subgoal.assert_not_called()
    ws.merge_subgoal_worktree.assert_not_called()


def test_undeclared_shared_test_surface_takes_strict_retry_path_even_when_require_tests_off():
    """(Step 2b) TEST_SURFACE_CHANGED without verifier_modified (e.g. undeclared shared
    test fixture change) must take the strict retry path even when require_tests is off.
    """
    subgoal = Subgoal(id="sg-shared", description="shared test fixture change", expects_tests=True)
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TEST_SURFACE_CHANGED,
        evidence_level=EvidenceLevel.NONE,
        verifier_modified=False,
        test_surface_changed=True,
        mechanically_unverified=True,
    )
    mech_res = MechanicalCheckResult(
        passed=False,
        failed_check="test_surface_changed",
        detail="Shared test surface modified without declaration",
        mech_evidence=ev,
        checks_run=["build", "compile", "tests"],
        checks={"build": {"ran": True, "passed": True}, "compile": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
    )

    ws = MagicMock()
    ws.get_staged_diff.return_value = "+diff\n"
    ws.worktree_dir = Path("/tmp/mock_wt")
    ws.repo_dir = Path("/tmp/mock_repo")
    ws.run_mechanical_checks.return_value = mech_res
    ws.merge_subgoal_worktree = MagicMock()
    ws.discard_subgoal_worktree = MagicMock()

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True)

    state1: State = {
        "ticket": "Ticket shared surface",
        "current_subgoal": subgoal,
        "require_tests": "off",
        "current_worktree_path": "/tmp/mock_wt",
        "current_worktree_branch": "jev-subgoal-shared",
        "integration_branch": "main",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
    }
    out1 = node_gate(state1, workspace=ws, gatekeeper=gk)
    assert out1.get("gate_status") == "unmeasured"
    assert out1.get("unmeasured_retry_count") == 1
    gk.validate_subgoal.assert_not_called()
    ws.merge_subgoal_worktree.assert_not_called()

