import json

from jev.engine import normalize_jev_feedback, node_gate
from jev.models import MechanicalCheckResult, Subgoal, ValidationVerdict


def test_normalize_structured_feedback_and_typed_questions():
    feedback = normalize_jev_feedback({
        "verdict": False,
        "confidence": 0.82,
        "diagnostics": ["missing branch"],
        "questions": [{"question": "What should happen on empty input?"}],
        "unknowns": ["expected fallback behavior"],
    })

    assert feedback.verdict is False
    assert feedback.confidence == 0.82
    assert feedback.diagnostics == ["missing branch"]
    assert feedback.questions == ["What should happen on empty input?"]
    assert feedback.unknowns == ["expected fallback behavior"]


def test_normalize_malformed_and_legacy_feedback_falls_back_without_throwing():
    malformed = normalize_jev_feedback('{"verdict":')
    legacy = normalize_jev_feedback(json.dumps({"valid": True, "probability": 0.9, "reason": "ok"}))

    assert malformed.verdict is None
    assert malformed.diagnostics == ['{"verdict":']
    assert legacy.verdict is True
    assert legacy.confidence == 0.9
    assert legacy.diagnostics == ["ok"]


def test_semantic_gate_authority_and_retry_evidence_are_preserved():
    state = {
        "current_subgoal": Subgoal(description="Implement feature", scope=["feature.py"]),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "failed_attempts": [],
    }
    workspace = type("Workspace", (), {
        "get_staged_diff": lambda self: "diff --git a/feature.py b/feature.py\n+value = 1\n",
        "run_mechanical_checks": lambda self, subgoal: MechanicalCheckResult(passed=True),
        "rollback_subgoal": lambda self: None,
    })()
    gatekeeper = type("Gatekeeper", (), {
        "validate_subgoal": lambda self, *args, **kwargs: ValidationVerdict(
            valid=False, probability=0.2, reason="missing behavior"
        ),
    })()

    result = node_gate(state, workspace=workspace, gatekeeper=gatekeeper)

    assert result["gate_status"] == "semantic_failure"
    assert result["semantic_strike_count"] == 1
    assert result["mechanical_strike_count"] == 0
    feedback = result["failed_attempts"][0]["failure_evidence"]["jev_feedback"]
    assert feedback["verdict"] is False
    assert feedback["confidence"] == 0.2
    assert feedback["diagnostics"] == ["missing behavior"]
