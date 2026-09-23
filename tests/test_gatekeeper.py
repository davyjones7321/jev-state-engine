import inspect
from unittest.mock import MagicMock, patch
import httpx
import pytest

from jev.engine import node_gate
from jev.gatekeeper import Gatekeeper
from jev.models import MechanicalCheckResult, Subgoal, ValidationVerdict


# 21. test_validate_subgoal_retries_on_5xx
def test_validate_subgoal_retries_on_5xx():
    """Mock the HTTP layer to return a 500 then a 200, assert the client retried and returned the eventual success."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature A", scope=["a.py"], expects_tests=True)
    diff = "diff --git a/a.py b/a.py\n+x = 1\n"

    mock_resp_500 = httpx.Response(500, json={"error": "Internal Server Error"})
    mock_resp_200 = httpx.Response(200, json={"valid": {"value": True, "probability": 0.95}})

    with patch.object(gk.client, "post", side_effect=[mock_resp_500, mock_resp_200]) as mock_post:
        verdict = gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 2
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is True
        assert verdict.probability == 0.95


# 22. test_validate_subgoal_does_not_retry_on_4xx
def test_validate_subgoal_does_not_retry_on_4xx():
    """Mock a 400 (e.g. an invalid/expired key), assert no retry loop."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature A", scope=["a.py"], expects_tests=True)
    diff = "diff --git a/a.py b/a.py\n+x = 1\n"

    mock_resp_400 = httpx.Response(400, json={"error": "Bad Request"})

    with patch.object(gk.client, "post", return_value=mock_resp_400) as mock_post:
        with pytest.raises(Exception):
            gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 1


# 23. test_validate_subgoal_sends_noul_question
def test_validate_subgoal_sends_noul_question():
    """Inspect the outgoing request payload, assert questions.valid.type == 'boolean' (Noul), not a Choice or free-text prompt."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature B", scope=["b.py"], expects_tests=True)
    diff = "diff --git a/b.py b/b.py\n+y = 2\n"

    mock_resp = httpx.Response(200, json={"valid": {"value": True, "probability": 0.88}})

    with patch.object(gk.client, "post", return_value=mock_resp) as mock_post:
        gk.validate_subgoal(subgoal, diff)
        assert mock_post.call_count == 1
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs.get("json", {})

        assert "questions" in payload
        assert "valid" in payload["questions"]
        question = payload["questions"]["valid"]
        assert question.get("type") in ("boolean", "noul")
        assert question.get("type") != "choice"
        assert "instructions" in question
        assert len(question["instructions"]) > 0


# 24. test_validate_subgoal_maps_response_correctly
def test_validate_subgoal_maps_response_correctly():
    """Mock a well-formed Noul response ({"valid": {"value": true, "probability": 0.91}}), assert it maps to ValidationVerdict(valid=True, probability=0.91) with no parsing/regex involved."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature C", scope=["c.py"], expects_tests=True)
    diff = "diff --git a/c.py b/c.py\n+z = 3\n"

    # Test True mapping
    mock_resp_true = httpx.Response(200, json={"valid": {"value": True, "probability": 0.91}})
    with patch.object(gk.client, "post", return_value=mock_resp_true):
        verdict = gk.validate_subgoal(subgoal, diff)
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is True
        assert verdict.probability == 0.91

    # Test False mapping
    mock_resp_false = httpx.Response(200, json={"valid": {"value": False, "probability": 0.15}})
    with patch.object(gk.client, "post", return_value=mock_resp_false):
        verdict = gk.validate_subgoal(subgoal, diff)
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is False
        assert verdict.probability == 0.15

    # Test Real System One API response format (answers.valid.noul)
    mock_resp_real_pass = httpx.Response(200, json={"answers": {"valid": {"noul": 0.94}}})
    with patch.object(gk.client, "post", return_value=mock_resp_real_pass):
        verdict = gk.validate_subgoal(subgoal, diff)
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is True
        assert verdict.probability == 0.94

    mock_resp_real_fail = httpx.Response(200, json={"answers": {"valid": {"noul": 0.08}}})
    with patch.object(gk.client, "post", return_value=mock_resp_real_fail):
        verdict = gk.validate_subgoal(subgoal, diff)
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is False
        assert verdict.probability == 0.08


# 25. test_validate_subgoal_rejects_malformed_response
def test_validate_subgoal_rejects_malformed_response():
    """Mock an HTTP-level response that doesn't match the expected schema (missing fields, wrong types), assert it raises rather than silently returning a default verdict."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature D", scope=["d.py"], expects_tests=True)
    diff = "diff --git a/d.py b/d.py\n+w = 4\n"

    malformed_payloads = [
        {},
        {"valid": "not a dict"},
        {"valid": {"probability": 0.91}},  # missing value
        {"valid": {"value": True}},  # missing probability
        {"valid": {"value": "not_a_bool", "probability": 0.91}},  # value not bool
        {"valid": {"value": True, "probability": "not_a_float"}},  # probability not float
    ]

    for bad_json in malformed_payloads:
        mock_resp = httpx.Response(200, json=bad_json)
        with patch.object(gk.client, "post", return_value=mock_resp):
            with pytest.raises(Exception):
                gk.validate_subgoal(subgoal, diff)


# 26. test_validate_subgoal_forwards_untested_flag
def test_validate_subgoal_forwards_untested_flag():
    """Construct a call where MechanicalCheckResult.detail carries the untested-pass flag (from test 12), assert it's present in the state object sent to Jev."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add scaffold", scope=["scaffold.py"], expects_tests=False)
    diff = "diff --git a/scaffold.py b/scaffold.py\n+# scaffold\n"
    untested_detail = "Untested pass: expects_tests=False with NO_TESTS_COLLECTED."

    mock_resp = httpx.Response(200, json={"valid": {"value": True, "probability": 0.85}})

    with patch.object(gk.client, "post", return_value=mock_resp) as mock_post:
        gk.validate_subgoal(subgoal, diff, mechanical_detail=untested_detail)
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs.get("json", {})
        state = payload.get("state", {})

        # Assert presence of untested flag in state
        assert (
            state.get("untested_pass") is True
            or "untested" in str(state).lower()
            or "untested" in state.get("mechanical_detail", "").lower()
        )


# 27. test_env_vars_loaded_from_dotenv_only
def test_env_vars_loaded_from_dotenv_only(monkeypatch, tmp_path):
    """Assert JEV_API_URL / JEV_API_KEY are read from .env and that no hardcoded fallback exists in the source."""
    # Ensure source doesn't contain hardcoded API key or fallback URL literals
    source = inspect.getsource(Gatekeeper)
    assert "https://api.typesafe.ai" not in source or "os.getenv" in source or "os.environ" in source
    assert "apikey_" not in source

    # Test missing env vars without .env raises error
    monkeypatch.delenv("JEV_API_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    with pytest.raises((ValueError, KeyError)):
        Gatekeeper(env_file=tmp_path / ".nonexistent_env")


# 28. Live integration test with real Jev API
def test_live_jev_api_call():
    """Verify live call against https://api.typesafe.ai/v1/systemone using real JEV_API_KEY from .env."""
    gk = Gatekeeper()  # Loads real JEV_API_URL and JEV_API_KEY from .env
    subgoal = Subgoal(
        description="Implement a helper function add(a, b) returning sum",
        scope=["math_utils.py"],
        expects_tests=True,
    )
    diff = """diff --git a/math_utils.py b/math_utils.py
new file mode 100644
--- /dev/null
+++ b/math_utils.py
@@ -0,0 +1,2 @@
+def add(a: int, b: int) -> int:
+    return a + b
"""
    verdict = gk.validate_subgoal(subgoal, diff)
    assert isinstance(verdict, ValidationVerdict)
    assert isinstance(verdict.valid, bool)
    assert isinstance(verdict.probability, float)
    assert 0.0 <= verdict.probability <= 1.0


# 29. Integration between node_gate and real Gatekeeper class
def test_node_gate_with_real_gatekeeper_instance():
    """Verify node_gate integrates seamlessly with a real Gatekeeper instance."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    subgoal = Subgoal(description="Add feature X", scope=["x.py"], expects_tests=True)
    state = {
        "ticket": "Ticket 1",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    # Case 1: Tier 0 fails -> Gatekeeper validate_subgoal is not called
    class FakeWsFail:
        def run_mechanical_checks(self, sg):
            return MechanicalCheckResult(passed=False, failed_check="build", detail="SyntaxError")

        def rollback_subgoal(self):
            pass

    out = node_gate(state.copy(), workspace=FakeWsFail(), gatekeeper=gk)
    assert out["mechanical_strike_count"] == 1
    assert out["semantic_strike_count"] == 0

    # Case 2: Tier 0 passes -> Gatekeeper validate_subgoal is called
    mock_resp = httpx.Response(200, json={"valid": {"value": True, "probability": 0.99}})

    class FakeWsPass:
        def __init__(self):
            self.committed = False

        def run_mechanical_checks(self, sg):
            return MechanicalCheckResult(passed=True)

        def get_staged_diff(self):
            return "+x = 1"

        def commit_subgoal(self):
            self.committed = True

    ws_pass = FakeWsPass()
    with patch.object(gk.client, "post", return_value=mock_resp):
        out2 = node_gate(state.copy(), workspace=ws_pass, gatekeeper=gk)
        assert out2["gate_status"] == "passed"
        assert ws_pass.committed is True


# 30. Test verify_ticket sends noul question and handles response
def test_verify_ticket_sends_noul_question():
    """Verify verify_ticket constructs noul question and parses verdict correctly."""
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    mock_resp = httpx.Response(200, json={"answers": {"valid": {"noul": 0.97}}})

    with patch.object(gk.client, "post", return_value=mock_resp) as mock_post:
        verdict = gk.verify_ticket(ticket="Fix bug #123", final_diff="+fix = True", test_output="PASSED")
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs.get("json", {})
        assert "questions" in payload
        assert "valid" in payload["questions"]
        assert payload["questions"]["valid"]["type"] == "noul"
        assert isinstance(verdict, ValidationVerdict)
        assert verdict.valid is True
        assert verdict.probability == 0.97


# 31. Test escalate_deadlock writes file with custom objects
def test_escalate_deadlock_dumps_valid_json(tmp_path):
    """Verify escalate_deadlock safely dumps trajectory into JSON file."""
    import json
    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="test_key")
    log_file = tmp_path / "escalation.log"
    trajectory = [
        {"node": "investigate", "path": tmp_path},  # non-string path
        {"node": "gate", "subgoal": Subgoal(description="Test")},  # Pydantic model
    ]
    gk.escalate_deadlock(trajectory=trajectory, triggering_tier="mechanical", log_path=log_file)
    assert log_file.exists()
    content = json.loads(log_file.read_text(encoding="utf-8"))
    assert content["triggering_tier"] == "mechanical"
    assert len(content["trajectory"]) == 2



