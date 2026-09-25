import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from langchain_core.messages import AIMessage, HumanMessage
from jev.engine import JevEngine, node_plan, route_plan
from jev.gatekeeper import Gatekeeper
from jev.models import State, Subgoal


class ScriptedChatModel:
    """Fake/Scripted LLM implementing LangChain's .bind_tools() and .invoke() interface."""

    def __init__(self, responses: Optional[List[Any]] = None):
        self.responses = list(responses or [])
        self.bound_tools: List[Any] = []
        self.invocations: List[List[Any]] = []

    def bind_tools(self, tools: List[Any], **kwargs: Any) -> "ScriptedChatModel":
        self.bound_tools = list(tools)
        bound = ScriptedChatModel(responses=self.responses)
        bound.bound_tools = self.bound_tools
        bound.invocations = self.invocations
        return bound

    def invoke(self, messages: List[Any], **kwargs: Any) -> Any:
        self.invocations.append(messages)
        if not self.responses:
            return AIMessage(content="[]")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp



class MockWorkspace:
    """Mock workspace for planning tests."""
    def __init__(self):
        self.repo_dir = Path(".")


# 1. Valid JSON on first try -> plan_queue populated correctly
def test_node_plan_valid_json_first_try():
    plan_data = [
        {
            "description": "Create database schema",
            "scope": ["src/db/schema.py"],
            "expects_tests": True,
        },
        {
            "description": "Add user model API endpoints",
            "scope": ["src/api/users.py", "tests/test_users.py"],
            "expects_tests": True,
        },
    ]
    fake_llm = ScriptedChatModel([json.dumps(plan_data)])
    state: State = {
        "ticket": "Implement user management",
        "investigation_notes": "Existing API in src/api.",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 1
    assert result.get("gate_status") != "planning_failed"
    assert len(result["plan_queue"]) == 2
    assert isinstance(result["plan_queue"][0], Subgoal)
    assert result["plan_queue"][0].description == "Create database schema"
    assert result["plan_queue"][0].scope == ["src/db/schema.py"]
    assert result["plan_queue"][0].expects_tests is True
    assert result["plan_queue"][1].description == "Add user model API endpoints"
    assert result["plan_queue"][1].scope == ["src/api/users.py", "tests/test_users.py"]

    # Trajectory entry
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["node"] == "plan"
    assert len(result["trajectory"][0]["subgoals"]) == 2


# 2. Malformed JSON on first try, valid on retry -> succeeds, retry prompt contains error
def test_node_plan_malformed_json_retry_succeeds():
    malformed_response = "Here is the plan:\n[ {description: not valid json..."
    valid_plan = [
        {
            "description": "Implement authentication handler",
            "scope": ["src/auth.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([malformed_response, json.dumps(valid_plan)])
    state: State = {
        "ticket": "Add auth handler",
        "investigation_notes": "Token verification needed.",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    # Verify retry prompt fed back the validation error
    retry_invocation = fake_llm.invocations[1]
    # Find human message in retry
    human_messages = [m for m in retry_invocation if isinstance(m, HumanMessage)]
    assert len(human_messages) >= 2
    retry_msg = human_messages[-1].content
    assert "your previous output failed validation because:" in retry_msg.lower()

    # Plan queue populated correctly after retry
    assert result.get("gate_status") != "planning_failed"
    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].description == "Implement authentication handler"
    assert result["plan_queue"][0].scope == ["src/auth.py"]


# 3. Malformed JSON on BOTH attempts -> hard failure path triggers
def test_node_plan_malformed_json_both_attempts_triggers_hard_failure():
    bad_resp_1 = "NOT JSON 1"
    bad_resp_2 = "NOT JSON 2"
    fake_llm = ScriptedChatModel([bad_resp_1, bad_resp_2])
    state: State = {
        "ticket": "Add payments",
        "trajectory": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("status") == "escalated"
    # plan_queue must NOT fall back to old single-fake-subgoal stub
    assert result.get("plan_queue") == []
    # Strike counters must not be reused
    assert result.get("mechanical_strike_count") == 0
    assert result.get("semantic_strike_count") == 0

    # Trajectory records planning failure
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["node"] == "plan"
    assert "error" in result["trajectory"][0]


# 4. Valid JSON but violates Subgoal schema: wrong type for scope
def test_node_plan_schema_violation_wrong_type_retries_and_succeeds():
    invalid_schema = [
        {
            "description": "Modify settings",
            "scope": "settings.py",  # Should be list, not str
            "expects_tests": True,
        }
    ]
    valid_plan = [
        {
            "description": "Modify settings",
            "scope": ["settings.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(invalid_schema), json.dumps(valid_plan)])
    state: State = {
        "ticket": "Update settings",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    retry_invocation = fake_llm.invocations[1]
    human_messages = [m for m in retry_invocation if isinstance(m, HumanMessage)]
    retry_msg = human_messages[-1].content
    assert "your previous output failed validation because:" in retry_msg.lower()

    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].scope == ["settings.py"]


# 5. Valid JSON but violates Subgoal schema: missing required description
def test_node_plan_schema_violation_missing_field_fails_both():
    missing_desc = [
        {
            "scope": ["settings.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(missing_desc), json.dumps(missing_desc)])
    state: State = {
        "ticket": "Missing desc ticket",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("plan_queue") == []


# 6. Valid JSON but empty scope (scope=[]) violates declared scope requirement
def test_node_plan_schema_violation_empty_scope_fails():
    empty_scope_plan = [
        {
            "description": "Vague task with no scope",
            "scope": [],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(empty_scope_plan), json.dumps(empty_scope_plan)])
    state: State = {
        "ticket": "Vague ticket",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("plan_queue") == []


# 7. Valid JSON but not an array/list (e.g. dict or primitive)
def test_node_plan_json_not_list_fails():
    not_list = {"description": "Single obj", "scope": ["a.py"]}
    fake_llm = ScriptedChatModel([json.dumps(not_list), json.dumps(not_list)])
    state: State = {"ticket": "Not list", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("plan_queue") == []


# 8. investigation_notes from state is actually included in the prompt sent to LLM
def test_node_plan_includes_investigation_notes_in_prompt():
    valid_plan = [{"description": "Step 1", "scope": ["core.py"], "expects_tests": True}]
    fake_llm = ScriptedChatModel([json.dumps(valid_plan)])
    notes_content = "Discovered critical dependency in core.py on line 42."
    state: State = {
        "ticket": "Fix core issue",
        "investigation_notes": notes_content,
        "trajectory": [],
    }

    node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 1
    prompt_text = fake_llm.invocations[0][0].content
    assert notes_content in prompt_text
    assert "Fix core issue" in prompt_text


# 9. llm=None backwards compatibility, matching 7a/7b pattern
def test_node_plan_llm_none_backwards_compatible():
    state: State = {
        "ticket": "Backwards compatibility ticket",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=None)

    assert result.get("gate_status") != "planning_failed"
    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].description == "Backwards compatibility ticket"
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["node"] == "plan"


# 10. route_plan routes to escalate on planning_failed and implement otherwise
def test_route_plan_branches():
    state_failed: State = {"gate_status": "planning_failed"}
    assert route_plan(state_failed) == "escalate"

    state_ok: State = {"gate_status": None, "plan_queue": [Subgoal(description="Sub 1", scope=["a.py"])]}
    assert route_plan(state_ok) == "implement"


# 11. Full JevEngine DAG escalates without calling implement when planning fails
def test_jev_engine_escalates_on_planning_failure(tmp_path):
    db_file = tmp_path / "engine_plan_fail.db"
    investigate_resp = AIMessage(
        content="Investigation done.",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Context gathered."}, "id": "inv_1"}],
    )
    fake_llm = ScriptedChatModel([investigate_resp, "BAD JSON 1", "BAD JSON 2"])
    mock_ws = MockWorkspace()
    mock_gk = MagicMock()

    engine = JevEngine(
        workspace=mock_ws,
        gatekeeper=mock_gk,
        llm=fake_llm,
        db_path=str(db_file),
    )

    final_state = engine.execute(ticket="Ticket doomed to fail planning", thread_id="plan-fail-1")

    assert final_state["status"] == "escalated"
    assert final_state["gate_status"] == "planning_failed"
    assert final_state["plan_queue"] == []
    # Verify implement was NEVER executed in trajectory
    nodes_executed = [entry.get("node") for entry in final_state.get("trajectory", [])]
    assert "implement" not in nodes_executed
    assert "plan" in nodes_executed


# 12. Planning hard failure calls gatekeeper.escalate_deadlock with triggering_tier="planning"
def test_node_plan_hard_failure_calls_escalate_deadlock():
    fake_llm = ScriptedChatModel(["NOT JSON 1", "NOT JSON 2"])
    mock_gk = MagicMock()
    state: State = {"ticket": "Deadlock ticket", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm, gatekeeper=mock_gk)

    assert result.get("gate_status") == "planning_failed"
    mock_gk.escalate_deadlock.assert_called_once_with(
        trajectory=result["trajectory"],
        triggering_tier="planning",
    )


# 13. Valid JSON enclosed in markdown code fences parses successfully
def test_node_plan_markdown_code_fences_handled():
    plan_data = [
        {
            "description": "Markdown wrapped plan",
            "scope": ["src/module.py"],
            "expects_tests": True,
        }
    ]
    markdown_wrapped = f"```json\n{json.dumps(plan_data, indent=2)}\n```"
    fake_llm = ScriptedChatModel([markdown_wrapped])
    state: State = {"ticket": "Markdown plan", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].description == "Markdown wrapped plan"
    assert result["plan_queue"][0].scope == ["src/module.py"]


# 14. Conversational preamble and postscript surrounding code block
def test_node_plan_conversational_preamble_and_postscript():
    plan_data = [
        {
            "description": "Create api service",
            "scope": ["src/service.py"],
            "expects_tests": True,
        }
    ]
    raw_response = (
        "Here is the execution plan for the ticket:\n\n"
        "```json\n"
        f"{json.dumps(plan_data, indent=2)}\n"
        "```\n\n"
        "Please let me know if you would like me to adjust any of these subgoals!"
    )
    fake_llm = ScriptedChatModel([raw_response])
    state: State = {"ticket": "Add service", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert result.get("gate_status") != "planning_failed"
    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].description == "Create api service"
    assert result["plan_queue"][0].scope == ["src/service.py"]


# 15. Raw JSON array with conversational text and uppercase ```JSON
def test_node_plan_uppercase_json_code_fence():
    plan_data = [
        {
            "description": "Uppercase fence plan",
            "scope": ["src/upper.py"],
            "expects_tests": False,
        }
    ]
    raw_response = f"```JSON \n{json.dumps(plan_data)}\n```"
    fake_llm = ScriptedChatModel([raw_response])
    state: State = {"ticket": "Upper fence", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].scope == ["src/upper.py"]
    assert result["plan_queue"][0].expects_tests is False


# 16. Conversational preamble with raw JSON array (no markdown code fence)
def test_node_plan_raw_json_array_with_preamble():
    plan_data = [
        {
            "description": "Raw array with preamble",
            "scope": ["src/raw.py"],
            "expects_tests": True,
        }
    ]
    raw_response = f"Certainly! Here is your plan: {json.dumps(plan_data)} Hope that helps."
    fake_llm = ScriptedChatModel([raw_response])
    state: State = {"ticket": "Raw array ticket", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].description == "Raw array with preamble"


# 17. Strict validation rejects non-boolean expects_tests (e.g. string "true" or integer 1)
def test_node_plan_strict_validation_rejects_non_boolean_expects_tests():
    invalid_plan = [
        {
            "description": "Non boolean test",
            "scope": ["src/test.py"],
            "expects_tests": "yes",  # String instead of boolean
        }
    ]
    valid_plan = [
        {
            "description": "Non boolean test",
            "scope": ["src/test.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(invalid_plan), json.dumps(valid_plan)])
    state: State = {"ticket": "Strict bool ticket", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    # Verify retry prompt fed back the validation error about boolean
    retry_invocation = fake_llm.invocations[1]
    human_messages = [m for m in retry_invocation if isinstance(m, HumanMessage)]
    retry_msg = human_messages[-1].content
    assert "validation because" in retry_msg.lower()
    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].expects_tests is True


# 18. Scope security: absolute paths and directory traversal are rejected
def test_node_plan_scope_security_rejects_traversal_and_absolute_paths():
    traversal_plan = [
        {
            "description": "Path traversal attack",
            "scope": ["../../etc/passwd"],
            "expects_tests": True,
        }
    ]
    absolute_plan = [
        {
            "description": "Absolute path",
            "scope": ["/var/log/syslog"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(traversal_plan), json.dumps(absolute_plan)])
    state: State = {"ticket": "Security ticket", "trajectory": []}

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    # Both attempts violate scope security -> hard failure
    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("plan_queue") == []


# 19. Node plan cleanses last_feedback and current_subgoal so investigation notes don't leak into node_implement
def test_node_plan_clears_last_feedback_and_current_subgoal():
    valid_plan = [
        {
            "description": "Clean state plan",
            "scope": ["src/clean.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(valid_plan)])
    state: State = {
        "ticket": "Clean state ticket",
        "investigation_notes": "Investigation discovered everything.",
        "last_feedback": "Investigation discovered everything.",  # Leaked from node_investigate
        "current_subgoal": Subgoal(description="Old stale subgoal", scope=["old.py"]),
        "gate_status": "passed",
        "trajectory": [],
    }

    result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm)

    assert result.get("last_feedback") is None
    assert result.get("current_subgoal") is None
    assert result.get("gate_status") is None
    assert len(result["plan_queue"]) == 1


# 20. 5 consecutive API failures during planning escalates cleanly without raising
def test_node_plan_api_failure_escalates_cleanly():
    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = Exception("Connection timeout")
    mock_gk = MagicMock()
    state: State = {
        "ticket": "API failure ticket",
        "trajectory": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    with patch("jev.engine.time.sleep") as mock_sleep:
        result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm, gatekeeper=mock_gk)

    # 1. node_plan() returns cleanly without raising.
    assert result is not None
    # 2. result["gate_status"] == "planning_failed"
    assert result.get("gate_status") == "planning_failed"
    # 3. result["status"] == "escalated"
    assert result.get("status") == "escalated"
    # 4. result["plan_queue"] == []
    assert result.get("plan_queue") == []
    assert result.get("current_subgoal") is None
    assert "Planning API failure after 5 retries: Connection timeout" in result.get("last_feedback", "")
    assert result.get("mechanical_strike_count") == 0
    assert result.get("semantic_strike_count") == 0

    # 5. The trajectory records the distinct "api_failure" error type / message.
    assert len(result["trajectory"]) == 1
    traj_entry = result["trajectory"][0]
    assert traj_entry["node"] == "plan"
    assert traj_entry.get("error_type") == "api_failure"
    assert "API failure after 5 retries: Connection timeout" in traj_entry.get("error", "")
    assert traj_entry.get("subgoals") == []

    # 6. gatekeeper.escalate_deadlock was called with triggering_tier="planning"
    mock_gk.escalate_deadlock.assert_called_once_with(
        trajectory=result["trajectory"],
        triggering_tier="planning",
    )

    # Verify 5 invocations attempted and 4 sleep retries performed
    assert fake_llm.invoke.call_count == 5
    assert mock_sleep.call_count == 4


# 21. API failure escalation handles None gatekeeper cleanly without raising
def test_node_plan_api_failure_without_gatekeeper():
    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = Exception("429 Resource Exhausted")
    state: State = {"ticket": "No gatekeeper API fail ticket", "trajectory": []}

    with patch("jev.engine.time.sleep") as mock_sleep:
        result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm, gatekeeper=None)

    assert result.get("gate_status") == "planning_failed"
    assert result.get("status") == "escalated"
    assert result.get("plan_queue") == []
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["error_type"] == "api_failure"
    assert "429 Resource Exhausted" in result["trajectory"][0]["error"]
    assert mock_sleep.call_count == 4
    # Verify 429 triggered the 25s backoff
    mock_sleep.assert_called_with(25)


# 22. Full JevEngine DAG escalates without raising when planning encounters fatal API failures
def test_jev_engine_escalates_on_planning_api_failure(tmp_path):
    db_file = tmp_path / "engine_plan_api_fail.db"
    investigate_resp = AIMessage(
        content="Investigation done.",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Context gathered."}, "id": "inv_1"}],
    )
    fake_llm = MagicMock()
    # node_investigate uses bound_llm.invoke(), node_plan uses llm.invoke()
    fake_llm.bind_tools.return_value.invoke.return_value = investigate_resp
    fake_llm.invoke.side_effect = Exception("API rate limit exceeded")

    mock_ws = MockWorkspace()
    mock_gk = MagicMock()

    engine = JevEngine(
        workspace=mock_ws,
        gatekeeper=mock_gk,
        llm=fake_llm,
        db_path=str(db_file),
    )

    with patch("jev.engine.time.sleep"):
        final_state = engine.execute(ticket="Ticket doomed to fail API in planning", thread_id="plan-api-fail-1")

    assert final_state["status"] == "escalated"
    assert final_state["gate_status"] == "planning_failed"
    assert final_state["plan_queue"] == []
    assert "Planning API failure after 5 retries" in final_state.get("last_feedback", "")
    mock_gk.escalate_deadlock.assert_called_once_with(
        trajectory=final_state["trajectory"],
        triggering_tier="planning",
    )
    nodes_executed = [entry.get("node") for entry in final_state.get("trajectory", [])]
    assert "implement" not in nodes_executed
    assert "plan" in nodes_executed


# 23. Initial validation failure followed by 5 consecutive API failures on retry escalates cleanly
def test_node_plan_api_failure_on_retry_after_validation_error():
    fake_llm = MagicMock()
    # Attempt 0 succeeds at API level but returns invalid output; attempt 1 fails 5 times with API errors
    fake_llm.invoke.side_effect = [
        AIMessage(content="INVALID NON-JSON OUTPUT"),
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
    ]
    mock_gk = MagicMock()
    state: State = {"ticket": "Validation then API fail ticket", "trajectory": []}

    with patch("jev.engine.time.sleep") as mock_sleep:
        result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm, gatekeeper=mock_gk)

    assert result.get("gate_status") == "planning_failed"
    assert result.get("status") == "escalated"
    assert result.get("plan_queue") == []
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["error_type"] == "api_failure"
    assert "API failure after 5 retries: 503 Service Unavailable" in result["trajectory"][0]["error"]
    mock_gk.escalate_deadlock.assert_called_once_with(
        trajectory=result["trajectory"],
        triggering_tier="planning",
    )
    assert fake_llm.invoke.call_count == 6
    assert mock_sleep.call_count == 4


# 24. Real Gatekeeper instance creates escalation.log distinguishing API failure from validation failure
def test_node_plan_api_failure_writes_escalation_log_with_real_gatekeeper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real_gk = Gatekeeper(api_url="http://mock-api.local", api_key="dummy_key")

    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = Exception("Network connection aborted")
    state: State = {"ticket": "Escalation log write ticket", "trajectory": []}

    with patch("jev.engine.time.sleep"):
        result = node_plan(state, workspace=MockWorkspace(), llm=fake_llm, gatekeeper=real_gk)

    log_file = tmp_path / "escalation.log"
    assert log_file.exists()
    content = json.loads(log_file.read_text(encoding="utf-8"))

    assert content["triggering_tier"] == "planning"
    assert len(content["trajectory"]) == 1
    traj_entry = content["trajectory"][0]
    assert traj_entry["node"] == "plan"
    assert traj_entry["error_type"] == "api_failure"
    assert "Network connection aborted" in traj_entry["error"]
    assert traj_entry["subgoals"] == []


# 25. Ungrounded scope on ticket modifying existing code fails validation and retries with feedback
def test_node_plan_ungrounded_scope_fails_validation_and_retries_successfully(tmp_path):
    # Setup a repo with real TypeScript file
    auth_file = tmp_path / "src" / "lib" / "auth.ts"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("export function createSession() {}", encoding="utf-8")

    class TestWorkspace:
        def __init__(self, root):
            self.worktree_dir = root
            self.repo_dir = root

    ws = TestWorkspace(tmp_path)
    hallucinated_plan = [
        {
            "description": "Add docstring to existing function",
            "scope": ["src/main.py"],
            "expects_tests": True,
        }
    ]
    grounded_plan = [
        {
            "description": "Add docstring to createSession",
            "scope": ["src/lib/auth.ts"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(hallucinated_plan), json.dumps(grounded_plan)])
    state: State = {
        "ticket": "Add a docstring to one existing function in this project",
        "investigation_notes": "Found undocumented functions: createSession in src/lib/auth.ts",
        "trajectory": [],
    }

    result = node_plan(state, workspace=ws, llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    # Verify retry prompt contains grounding validation error
    retry_invocation = fake_llm.invocations[1]
    human_messages = [m for m in retry_invocation if isinstance(m, HumanMessage)]
    retry_msg = human_messages[-1].content
    assert "does not exist in repository" in retry_msg.lower() or "not found during investigation" in retry_msg.lower()

    # Plan queue is grounded after retry
    assert result.get("gate_status") != "planning_failed"
    assert len(result["plan_queue"]) == 1
    assert result["plan_queue"][0].scope == ["src/lib/auth.ts"]


# 26. Ungrounded scope on both attempts escalates as planning_failed
def test_node_plan_ungrounded_scope_fails_both_attempts_and_escalates(tmp_path):
    class TestWorkspace:
        def __init__(self, root):
            self.worktree_dir = root
            self.repo_dir = root

    ws = TestWorkspace(tmp_path)
    hallucinated_plan = [
        {
            "description": "Add docstring to existing function",
            "scope": ["src/main.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(hallucinated_plan), json.dumps(hallucinated_plan)])
    mock_gk = MagicMock()
    state: State = {
        "ticket": "Add a docstring to one existing function in this project",
        "investigation_notes": "Found undocumented functions: createSession in src/lib/auth.ts",
        "trajectory": [],
    }

    result = node_plan(state, workspace=ws, llm=fake_llm, gatekeeper=mock_gk)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("status") == "escalated"
    assert result.get("plan_queue") == []
    assert len(result["trajectory"]) == 1
    assert result["trajectory"][0]["error_type"] == "validation_failure"
    assert "does not exist in repository" in result["trajectory"][0]["error"].lower()
    mock_gk.escalate_deadlock.assert_called_once_with(
        trajectory=result["trajectory"],
        triggering_tier="planning",
    )


# 27. Alien file extension not present in investigation notes or repo triggers validation failure
def test_node_plan_alien_extension_rejected_when_notes_specify_different_language(tmp_path):
    class TestWorkspace:
        def __init__(self, root):
            self.worktree_dir = root
            self.repo_dir = root

    ws = TestWorkspace(tmp_path)
    alien_plan = [
        {
            "description": "Add python script",
            "scope": ["src/script.py"],
            "expects_tests": True,
        }
    ]
    fake_llm = ScriptedChatModel([json.dumps(alien_plan), json.dumps(alien_plan)])
    state: State = {
        "ticket": "Update payment processor",
        "investigation_notes": "Discovered TypeScript code in src/lib/payment.ts and src/lib/auth.ts",
        "trajectory": [],
    }

    result = node_plan(state, workspace=ws, llm=fake_llm)

    assert len(fake_llm.invocations) == 2
    assert result.get("gate_status") == "planning_failed"
    assert result.get("status") == "escalated"
    assert "extension" in result["trajectory"][0]["error"].lower() or "does not exist" in result["trajectory"][0]["error"].lower()







