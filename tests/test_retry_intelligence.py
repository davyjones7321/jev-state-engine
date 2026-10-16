import json
import sqlite3
import pytest
import httpx
from unittest.mock import MagicMock, patch
from pathlib import Path

from jev.models import Subgoal, MechanicalCheckResult, ValidationVerdict, State, TestOutcome
from jev.engine import node_gate, node_implement, node_verify, JevEngine, _format_typed_semantic_feedback
from jev.gatekeeper import Gatekeeper
from jev.ledger import SqliteLedger, NullRecorder


# ==========================================
# A. Test output in feedback
# ==========================================

def test_feedback_contains_test_output_tail_at_gate():
    """When unit tests fail at the gate, include runner output_tail in last_feedback."""
    subgoal = Subgoal(id="sg-1", description="Implement math", scope=["math.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    output_tail = "FAILED tests/test_math.py::test_add - AssertionError: 1 + 1 != 3\n1 failed in 0.05s"
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/math.py b/math.py\n+def add(a, b): return a + b + 1\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False,
        failed_check="tests",
        detail="Unit tests failed.",
        test_runner_outcome={"ran": True, "output_tail": output_tail, "outcome": "FAILED"},
    )
    res = node_gate(state, workspace=ws, gatekeeper=MagicMock())
    assert output_tail in res["last_feedback"]
    assert "Unit tests failed" in res["last_feedback"]


def test_feedback_test_output_tail_capped_at_4000_chars():
    """When runner output is huge, output_tail is capped at 4000 characters in last_feedback."""
    subgoal = Subgoal(id="sg-1", description="Implement math", scope=["math.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    huge_tail = "x" * 6000 + "END_OF_OUTPUT"
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/math.py b/math.py\n+def add(a, b): pass\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False,
        failed_check="tests",
        detail="Unit tests failed.",
        test_runner_outcome={"ran": True, "output_tail": huge_tail, "outcome": "FAILED"},
    )
    res = node_gate(state, workspace=ws, gatekeeper=MagicMock())
    assert "END_OF_OUTPUT" in res["last_feedback"]
    # Tail part should not exceed 4000 chars
    assert len(res["last_feedback"]) < 4200
    assert len(huge_tail) == 6013
    assert huge_tail not in res["last_feedback"]


def test_feedback_contains_test_output_tail_in_verify():
    """When unit tests fail in verify, test_output sent to Jev / feedback includes runner output_tail."""
    state: State = {
        "ticket": "Implement math",
        "integration_branch": "jev-ticket-math",
        "trajectory": [],
    }
    output_tail = "FAILED tests/test_math.py - AssertionError\n1 failed"
    ws = MagicMock()
    ws.get_cumulative_diff.return_value = "diff --git a/math.py b/math.py\n"
    ws.check_compile.return_value = {"outcome": "PASSED"}
    ws.run_tests.return_value = TestOutcome.FAILED
    ws.last_test_run = {"ran": True, "output_tail": output_tail, "outcome": "FAILED"}

    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=False, reason=None, probability=0.1)

    res = node_verify(state, workspace=ws, gatekeeper=gk)
    assert res["status"] == "verification_failed"
    gk.verify_ticket.assert_called_once()
    called_test_out = gk.verify_ticket.call_args[0][2]
    assert output_tail in called_test_out
    assert output_tail in res["last_feedback"]


# ==========================================
# B. Dead-end block
# ==========================================

def test_dead_end_block_stored_in_state_up_to_2_most_recent():
    """State.failed_attempts holds at most 2 most recent failed attempts of current subgoal."""
    subgoal = Subgoal(id="sg-1", description="Implement feature", scope=["feat.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "failed_attempts": [],
        "trajectory": [],
    }
    ws = MagicMock()
    diff1 = "diff --git a/feat.py b/feat.py\n+attempt_1 = True\n"
    ws.get_staged_diff.return_value = diff1
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="compile", detail="Syntax error line 1"
    )

    # Attempt 1 fails
    state = node_gate(state, workspace=ws, gatekeeper=MagicMock())
    assert len(state["failed_attempts"]) == 1
    assert state["failed_attempts"][0]["attempt_no"] == 1
    assert state["failed_attempts"][0]["outcome"] == "mechanical_failure"
    assert state["failed_attempts"][0]["failed_check"] == "compile"
    assert "diff_sha" in state["failed_attempts"][0]
    assert "diff_excerpt" in state["failed_attempts"][0]
    assert "+attempt_1 = True" in state["failed_attempts"][0]["diff_excerpt"]

    # Attempt 2 fails
    diff2 = "diff --git a/feat.py b/feat.py\n+attempt_2 = True\n"
    ws.get_staged_diff.return_value = diff2
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="tests", detail="Unit tests failed."
    )
    state = node_gate(state, workspace=ws, gatekeeper=MagicMock())
    assert len(state["failed_attempts"]) == 2
    assert state["failed_attempts"][1]["attempt_no"] == 2
    assert state["failed_attempts"][1]["failed_check"] == "tests"

    # Attempt 3 fails -> list holds at most 2 most recent (attempts 2 and 3)
    diff3 = "diff --git a/feat.py b/feat.py\n+attempt_3 = True\n"
    ws.get_staged_diff.return_value = diff3
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="scope", detail="Scope creep"
    )
    state = node_gate(state, workspace=ws, gatekeeper=MagicMock())
    assert len(state["failed_attempts"]) == 2
    assert state["failed_attempts"][0]["attempt_no"] == 2
    assert state["failed_attempts"][1]["attempt_no"] == 3


def test_dead_end_block_cleared_on_pass_and_next_subgoal():
    """State.failed_attempts is cleared when subgoal passes or next subgoal starts."""
    subgoal1 = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    subgoal2 = Subgoal(id="sg-2", description="Subgoal 2", scope=["b.py"], expects_tests=True)
    state: State = {
        "plan_queue": [subgoal2],
        "current_subgoal": subgoal1,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "failed_attempts": [
            {"attempt_no": 1, "failed_check": "compile", "outcome": "mechanical_failure", "diff_sha": "abc", "diff_excerpt": "..."}
        ],
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/a.py b/a.py\n+pass\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    # Subgoal passes at gate
    state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert state["gate_status"] == "passed"
    assert state.get("failed_attempts") == []


def test_dead_end_block_in_implement_prompt():
    """Implement prompt contains 'Earlier attempts at this subgoal failed. Do not repeat them.' when retrying."""
    subgoal = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "gate_status": "mechanical_failure",
        "last_feedback": "Syntax error",
        "failed_attempts": [
            {
                "attempt_no": 1,
                "failed_check": "compile",
                "outcome": "mechanical_failure",
                "diff_sha": "deadbeef123",
                "diff_excerpt": "+bad_code()",
            }
        ],
        "retry_intel": "on",
    }
    ws = MagicMock()
    mock_llm = MagicMock()
    # Capture prompt passed to LLM
    node_implement(state, workspace=ws, llm=mock_llm)
    assert mock_llm.bind_tools.called
    bound = mock_llm.bind_tools.return_value
    call_messages = bound.invoke.call_args[0][0]
    prompt_str = call_messages[0].content
    assert "Earlier attempts at this subgoal failed. Do not repeat them." in prompt_str
    assert "Attempt 1" in prompt_str
    assert "bad_code" in prompt_str
    assert "Diff SHA:" not in prompt_str


def test_dead_end_block_absent_when_retry_intel_off():
    """When retry_intel is 'off', prompt does not contain dead-end block."""
    subgoal = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "gate_status": "mechanical_failure",
        "last_feedback": "Syntax error",
        "failed_attempts": [
            {
                "attempt_no": 1,
                "failed_check": "compile",
                "outcome": "mechanical_failure",
                "diff_sha": "deadbeef123",
                "diff_excerpt": "+bad_code()",
            }
        ],
        "retry_intel": "off",
    }
    ws = MagicMock()
    mock_llm = MagicMock()
    node_implement(state, workspace=ws, llm=mock_llm)
    bound = mock_llm.bind_tools.return_value
    call_messages = bound.invoke.call_args[0][0]
    prompt_str = call_messages[0].content
    assert "Earlier attempts at this subgoal failed. Do not repeat them." not in prompt_str


def test_dead_end_block_works_with_no_ledger():
    """Dead-end tracking works with recorder=None / NullRecorder."""
    subgoal = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "failed_attempts": [],
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/a.py b/a.py\n+err = True\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="compile", detail="Syntax error"
    )
    rec = NullRecorder()
    res = node_gate(state, workspace=ws, gatekeeper=MagicMock(), recorder=rec)
    assert len(res["failed_attempts"]) == 1
    assert res["failed_attempts"][0]["outcome"] == "mechanical_failure"


# ==========================================
# C. Duplicate-diff guard
# ==========================================

def test_duplicate_diff_guard_fails_immediately_without_tests_or_jev():
    """If new attempt has duplicate diff_sha of earlier failed attempt, fail immediately."""
    subgoal = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    identical_diff = "diff --git a/a.py b/a.py\n+identical = 1\n"
    import hashlib
    diff_sha = hashlib.sha256(identical_diff.encode("utf-8")).hexdigest()

    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 1,
        "semantic_strike_count": 0,
        "failed_attempts": [
            {
                "attempt_no": 1,
                "tier": "mechanical",
                "failed_check": "compile",
                "outcome": "mechanical_failure",
                "diff_sha": diff_sha,
                "diff_excerpt": identical_diff,
            }
        ],
        "retry_intel": "on",
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = identical_diff
    gk = MagicMock()
    rec = MagicMock()

    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=rec)

    # Mechanical checks and Jev should NOT be called!
    assert ws.run_mechanical_checks.call_count == 0
    assert gk.validate_subgoal.call_count == 0

    # Strike count for original tier (mechanical) incremented
    assert res["mechanical_strike_count"] == 2
    assert res["semantic_strike_count"] == 0

    # Feedback names earlier attempt number and why it failed
    assert "attempt 1" in res["last_feedback"]
    assert "compile" in res["last_feedback"]

    # Recorded in ledger with outcome="duplicate_diff"
    rec.record_attempt.assert_called_once()
    assert rec.record_attempt.call_args[1]["outcome"] == "duplicate_diff"


def test_duplicate_diff_guard_disabled_when_retry_intel_off():
    """When retry_intel is 'off', duplicate diff check is bypassed."""
    subgoal = Subgoal(id="sg-1", description="Subgoal 1", scope=["a.py"], expects_tests=True)
    identical_diff = "diff --git a/a.py b/a.py\n+identical = 1\n"
    import hashlib
    diff_sha = hashlib.sha256(identical_diff.encode("utf-8")).hexdigest()

    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 1,
        "semantic_strike_count": 0,
        "failed_attempts": [
            {
                "attempt_no": 1,
                "tier": "mechanical",
                "failed_check": "compile",
                "outcome": "mechanical_failure",
                "diff_sha": diff_sha,
                "diff_excerpt": identical_diff,
            }
        ],
        "retry_intel": "off",
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = identical_diff
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    res = node_gate(state, workspace=ws, gatekeeper=gk)
    assert ws.run_mechanical_checks.call_count == 1
    assert res["gate_status"] == "passed"


def test_mechanical_feedback_and_ledger_attempt_include_structured_retry_evidence():
    subgoal = Subgoal(id="sg-structured", description="Implement feature", scope=["feat.py"])
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "failed_attempts": [],
        "trajectory": [],
    }
    diff = "diff --git a/feat.py b/feat.py\n+bad =\n"
    ws = MagicMock()
    ws.get_staged_diff.return_value = diff
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="compile", detail="syntax error",
        compile_outcome={"new_errors": ["feat.py:1 syntax error"], "base_errors": []},
    )
    recorder = MagicMock()

    result = node_gate(state, workspace=ws, gatekeeper=MagicMock(), recorder=recorder)
    evidence = result["failed_attempts"][0]["failure_evidence"]
    assert {"failure_class", "known_facts", "unknowns", "required_inspection", "prior_attempts", "retry_constraints"} <= set(evidence)
    assert "compile" == evidence["failure_class"]
    assert "Failure evidence:" in result["last_feedback"]
    recorded = recorder.record_attempt.call_args.kwargs["failure_evidence"]
    assert recorded["required_inspection"] == evidence["required_inspection"]


def test_empty_diff_is_mechanical_and_skips_semantic_validation():
    subgoal = Subgoal(id="sg-empty", description="Implement feature", scope=["feat.py"])
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "failed_attempts": [],
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = ""
    gatekeeper = MagicMock()

    result = node_gate(state, workspace=ws, gatekeeper=gatekeeper)
    assert result["gate_status"] == "mechanical_failure"
    assert result["mechanical_strike_count"] == 1
    assert result["failed_attempts"][0]["outcome"] == "empty_diff"
    assert result["failed_attempts"][0]["failure_evidence"]["failure_class"] == "empty_diff"
    ws.run_mechanical_checks.assert_not_called()
    gatekeeper.validate_subgoal.assert_not_called()


# ==========================================
# D. Typed semantic feedback
# ==========================================

def test_extended_jev_request_payload_format():
    """Gatekeeper sends extended questions (incomplete, wrong_behavior, conflicts_with_notes, kind)."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature", scope=["feat.py"], expects_tests=True)
    diff = "diff --git a/feat.py b/feat.py\n+x = 1\n"

    mock_resp = httpx.Response(200, json={
        "answers": {
            "valid": {"noul": 0.95},
            "incomplete": {"noul": 0.05},
            "wrong_behavior": {"noul": 0.02},
            "conflicts_with_notes": {"noul": 0.01},
            "kind": {"choice": "fine", "confidence": 0.95, "probabilities": {"fine": 0.95}},
        }
    })

    with patch.object(gk.client, "post", return_value=mock_resp) as mock_post:
        verdict = gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 1
        payload = mock_post.call_args[1]["json"]
        questions = payload["questions"]
        assert "valid" in questions
        assert questions["valid"]["type"] in ("boolean", "noul")
        assert "incomplete" in questions
        assert questions["incomplete"]["type"] in ("boolean", "noul")
        assert "wrong_behavior" in questions
        assert questions["wrong_behavior"]["type"] in ("boolean", "noul")
        assert "conflicts_with_notes" in questions
        assert questions["conflicts_with_notes"]["type"] in ("boolean", "noul")
        assert "kind" in questions
        assert questions["kind"]["type"] == "choice"
        assert "criteria" in questions["kind"]
        criteria = questions["kind"]["criteria"]
        assert "incomplete" in criteria
        assert "wrong_behavior" in criteria
        assert "conflicts_with_conventions" in criteria
        assert "low_quality" in criteria
        assert "fine" in criteria


def test_parse_extra_answers_into_validation_verdict():
    """Parse choice and noul probabilities into ValidationVerdict fields."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature", scope=["feat.py"], expects_tests=True)
    diff = "diff --git a/feat.py b/feat.py\n+x = 1\n"

    mock_resp = httpx.Response(200, json={
        "answers": {
            "valid": {"noul": 0.12},
            "incomplete": {"noul": 0.85},
            "wrong_behavior": {"noul": 0.20},
            "conflicts_with_notes": {"noul": 0.10},
            "kind": {"choice": "incomplete", "confidence": 0.88, "probabilities": {"incomplete": 0.88, "fine": 0.12}},
        }
    })

    with patch.object(gk.client, "post", return_value=mock_resp):
        verdict = gk.validate_subgoal(subgoal, diff)
        assert verdict.valid is False
        assert verdict.probability == 0.12
        assert verdict.kind == "incomplete"
        assert verdict.kind_confidence == 0.88
        assert verdict.incomplete == 0.85
        assert verdict.wrong_behavior == 0.20
        assert verdict.conflicts_with_notes == 0.10


def test_typed_semantic_feedback_built_on_rejection():
    """When Jev rejects, feedback includes kind with confidence, checks > 0.5, and deterministic facts."""
    subgoal = Subgoal(id="sg-1", description="Implement auth", scope=["auth.py"], expects_tests=True)
    diff = "diff --git a/auth.py b/auth.py\n+def login(): pass\n+def logout(): pass\n-def old_func(): pass\n"
    state: State = {
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "retry_intel": "on",
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_staged_diff.return_value = diff
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)

    verdict = ValidationVerdict(
        valid=False,
        probability=0.2,
        kind="wrong_behavior",
        kind_confidence=0.82,
        incomplete=0.1,
        wrong_behavior=0.75,
        conflicts_with_notes=0.05,
    )
    gk = MagicMock()
    gk.validate_subgoal.return_value = verdict

    res = node_gate(state, workspace=ws, gatekeeper=gk)
    feedback = res["last_feedback"]
    assert "wrong_behavior" in feedback
    assert "0.82" in feedback
    assert "auth.py" in feedback
    assert "+2" in feedback or "Lines changed" in feedback or "added" in feedback.lower()


def test_4xx_extended_request_retries_once_with_valid_only():
    """If extended request returns 4xx, retry once with only valid and fall back to generic feedback."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature", scope=["feat.py"], expects_tests=True)
    diff = "diff --git a/feat.py b/feat.py\n+x = 1\n"

    resp_400 = httpx.Response(400, json={"error": "Unknown question type: choice"})
    resp_200_valid_only = httpx.Response(200, json={"answers": {"valid": {"noul": 0.15}}})

    with patch.object(gk.client, "post", side_effect=[resp_400, resp_200_valid_only]) as mock_post:
        verdict = gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 2
        # First call has extended questions
        assert "kind" in mock_post.call_args_list[0][1]["json"]["questions"]
        # Second call has only valid
        assert "kind" not in mock_post.call_args_list[1][1]["json"]["questions"]
        assert list(mock_post.call_args_list[1][1]["json"]["questions"].keys()) == ["valid"]
        assert verdict.valid is False
        assert verdict.probability == 0.15
        assert verdict.kind is None


def test_typed_semantic_feedback_fine_omits_main_issue():
    """When kind is 'fine', omit 'Main issue' and write 'No specific issue identified by the judge' plus rejection probability."""
    subgoal = Subgoal(id="sg-1", description="Implement auth", scope=["auth.py"], expects_tests=True)
    diff = "diff --git a/auth.py b/auth.py\n+x = 1\n"
    verdict = ValidationVerdict(
        valid=False,
        probability=0.15,
        kind="fine",
        kind_confidence=0.92,
    )
    feedback = _format_typed_semantic_feedback(verdict, subgoal, diff, tests_ran=True)
    assert "Main issue" not in feedback
    assert "No specific issue identified by the judge (validity probability: 0.15, pass needs 0.50)" in feedback


def test_gatekeeper_fallback_only_on_400_or_422_and_remembers():
    """Fall back to valid-only request only on 400 or 422. Do not fall back on 401, 403, 429 or 5xx.
    After one fallback, remember it on instance and send valid-only directly on later calls."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature", scope=["feat.py"], expects_tests=True)
    diff = "diff --git a/feat.py b/feat.py\n+x = 1\n"

    # 1. Fallback on 422, succeeds on fallback, and remembers valid-only mode
    resp_422 = httpx.Response(422, json={"error": "Unprocessable Entity"})
    resp_200 = httpx.Response(200, json={"answers": {"valid": {"noul": 0.85}}})

    with patch.object(gk.client, "post", side_effect=[resp_422, resp_200]) as mock_post:
        verdict = gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 2
        assert "kind" in mock_post.call_args_list[0][1]["json"]["questions"]
        assert list(mock_post.call_args_list[1][1]["json"]["questions"].keys()) == ["valid"]
        assert verdict.valid is True
        assert getattr(gk, "_valid_only_mode", False) is True

    # Subsequent call on same instance should send valid-only directly (1 call)
    with patch.object(gk.client, "post", return_value=resp_200) as mock_post:
        verdict2 = gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 1
        assert list(mock_post.call_args[1]["json"]["questions"].keys()) == ["valid"]
        assert verdict2.valid is True

    # 2. Do not fall back on 401, 403, 429
    for code in (401, 403, 429):
        fresh_gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
        resp_err = httpx.Response(code, json={"error": "Auth/Rate error"})
        with patch.object(fresh_gk.client, "post", return_value=resp_err) as mock_post:
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                fresh_gk.validate_subgoal(subgoal, diff)
            assert exc_info.value.response.status_code == code
            assert mock_post.call_count == 1  # No fallback attempted

    # Do not fall back on 5xx error
    fresh_gk_500 = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key", max_retries=1)
    resp_500 = httpx.Response(500, json={"error": "Server error"})
    with patch.object(fresh_gk_500.client, "post", return_value=resp_500) as mock_post:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            fresh_gk_500.validate_subgoal(subgoal, diff)
        assert exc_info.value.response.status_code == 500
        assert mock_post.call_count == 1  # No fallback attempted


# ==========================================
# E. Switch (--retry-intel on|off) & Migration
# ==========================================

def test_ledger_additive_migration_for_existing_db(tmp_path):
    """Opening an existing ledger without runs.retry_intel automatically adds the column."""
    db_file = tmp_path / "legacy_ledger.db"
    conn = sqlite3.connect(str(db_file))
    # Create old schema without retry_intel
    conn.execute("""
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY, thread_id TEXT, ticket TEXT,
      base_commit TEXT, final_commit TEXT, engine_commit TEXT,
      worker_model TEXT, test_policy TEXT,
      status TEXT, started_at TEXT, ended_at TEXT, notes_chars INTEGER
    )
    """)
    conn.commit()
    conn.close()

    # Open with SqliteLedger
    ledger = SqliteLedger(db_path=db_file)
    conn = sqlite3.connect(str(db_file))
    cols = [row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    conn.close()
    assert "retry_intel" in cols

    # Verify recording run start records retry_intel
    ledger.record_run_start(
        run_id="run-1",
        thread_id="t-1",
        ticket="ticket-1",
        retry_intel="off",
    )
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT retry_intel FROM runs WHERE run_id = 'run-1'").fetchone()
    conn.close()
    assert row[0] == "off"


def test_ledger_additive_migration_null_default_no_backfill(tmp_path):
    """Additive migration sets retry_intel DEFAULT NULL for existing rows without backfill."""
    db_file = tmp_path / "legacy_ledger_null.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
    CREATE TABLE runs (
      run_id TEXT PRIMARY KEY, thread_id TEXT, ticket TEXT,
      base_commit TEXT, final_commit TEXT, engine_commit TEXT,
      worker_model TEXT, test_policy TEXT,
      status TEXT, started_at TEXT, ended_at TEXT, notes_chars INTEGER
    )
    """)
    conn.execute("INSERT INTO runs (run_id, thread_id, ticket) VALUES ('run-legacy', 't-leg', 'ticket-leg')")
    conn.commit()
    conn.close()

    ledger = SqliteLedger(db_path=db_file)
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT retry_intel FROM runs WHERE run_id = 'run-legacy'").fetchone()
    conn.close()
    assert row[0] is None


# ==========================================
# F. Ledger fix (__verify__ tokens are NULL)
# ==========================================

def test_verify_attempt_row_has_null_tokens(tmp_path):
    """The __verify__ attempt row in ledger must have NULL tokens_in and tokens_out."""
    db_file = tmp_path / "verify_ledger.db"
    ledger = SqliteLedger(db_path=db_file)
    ledger.record_run_start("run-v", "thread-v", "ticket-v")

    state: State = {
        "run_id": "run-v",
        "thread_id": "thread-v",
        "ticket": "ticket-v",
        "integration_branch": "jev-int-v",
        "worker_tokens_in": 1234,
        "worker_tokens_out": 567,
        "trajectory": [],
    }
    ws = MagicMock()
    ws.get_cumulative_diff.return_value = "diff --git a/a.py b/a.py\n+v = 1\n"
    ws.check_compile.return_value = {"outcome": "PASSED"}
    ws.run_tests.return_value = TestOutcome.PASSED
    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

    node_verify(state, workspace=ws, gatekeeper=gk, recorder=ledger)

    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT tokens_in, tokens_out FROM attempts WHERE subgoal_id = '__verify__'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
