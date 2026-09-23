import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
import pytest

_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from jev.engine import node_implement
from jev.models import MechanicalCheckResult, State, Subgoal, TestOutcome


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
            return AIMessage(content="No more scripted responses.")
        return self.responses.pop(0)


class MockWorkspace:
    def __init__(self):
        self.staged_mutations: List[Dict[str, str]] = []

    def stage_file_mutation(self, path: str, content: str) -> None:
        self.staged_mutations.append({"path": str(path), "content": content})

    def run_mechanical_checks(self, subgoal: Any) -> MechanicalCheckResult:
        return MechanicalCheckResult(passed=True)

    def get_staged_diff(self) -> str:
        return "+mock diff"

    def rollback_subgoal(self) -> None:
        pass

    def commit_subgoal(self) -> None:
        pass


# 1. Multi-turn scripted tool calls: stage_file_mutation then submit_subgoal
def test_node_implement_with_fake_llm_multi_turn():
    ws = MockWorkspace()
    subgoal = Subgoal(
        description="Implement add(a, b) helper function",
        scope=["math_utils.py"],
        expects_tests=True,
    )
    state: State = {
        "ticket": "Build math utils",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    # Turn 1: Call stage_file_mutation
    turn_1_resp = AIMessage(
        content="I will write the add function.",
        tool_calls=[
            {
                "name": "stage_file_mutation",
                "args": {
                    "path": "math_utils.py",
                    "content": "def add(a: int, b: int) -> int:\n    return a + b\n",
                },
                "id": "call_1",
            }
        ],
    )
    # Turn 2: Call submit_subgoal
    turn_2_resp = AIMessage(
        content="Function written. Submitting subgoal.",
        tool_calls=[
            {
                "name": "submit_subgoal",
                "args": {"notes": "Implemented add function"},
                "id": "call_2",
            }
        ],
    )

    fake_llm = ScriptedChatModel(responses=[turn_1_resp, turn_2_resp])
    result_state = node_implement(state, workspace=ws, llm=fake_llm)

    # Assert mutations occurred on workspace
    assert len(ws.staged_mutations) == 1
    assert ws.staged_mutations[0]["path"] == "math_utils.py"
    assert "def add" in ws.staged_mutations[0]["content"]

    # Assert 2 turns executed
    assert len(fake_llm.invocations) == 2

    # Assert trajectory updated
    assert len(result_state["trajectory"]) == 1
    assert result_state["trajectory"][0]["node"] == "implement"
    assert result_state["trajectory"][0]["current_subgoal"]["description"] == subgoal.description


# 2. Single-turn parallel tool calls: stage_file_mutation AND submit_subgoal in 1 turn
def test_node_implement_single_turn_multiple_tools():
    ws = MockWorkspace()
    subgoal = Subgoal(
        description="Create config file",
        scope=["config.json"],
        expects_tests=False,
    )
    state: State = {
        "ticket": "Add config",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    single_resp = AIMessage(
        content="Staging config and submitting immediately.",
        tool_calls=[
            {
                "name": "stage_file_mutation",
                "args": {"path": "config.json", "content": '{"version": 1}'},
                "id": "call_cfg",
            },
            {
                "name": "submit_subgoal",
                "args": {},
                "id": "call_sub",
            },
        ],
    )

    fake_llm = ScriptedChatModel(responses=[single_resp])
    node_implement(state, workspace=ws, llm=fake_llm)

    assert len(ws.staged_mutations) == 1
    assert ws.staged_mutations[0]["path"] == "config.json"
    assert ws.staged_mutations[0]["content"] == '{"version": 1}'
    assert len(fake_llm.invocations) == 1


# 3. Verify provider-agnostic .bind_tools() is called with correct tool names and schemas
def test_node_implement_binds_write_and_submit_tools():
    ws = MockWorkspace()
    subgoal = Subgoal(
        description="Test bind tools",
        scope=["test.py"],
        expects_tests=True,
    )
    state: State = {
        "ticket": "Test tools",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "trajectory": [],
    }

    turn_resp = AIMessage(
        content="Done",
        tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "call_0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_resp])
    node_implement(state, workspace=ws, llm=fake_llm)

    # Check tools that were bound
    assert len(fake_llm.bound_tools) >= 2
    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in fake_llm.bound_tools]
    assert "stage_file_mutation" in tool_names
    assert "submit_subgoal" in tool_names


# 4. Verify prompt includes current subgoal description and declared scope
def test_node_implement_prompt_receives_description_and_scope():
    ws = MockWorkspace()
    subgoal = Subgoal(
        description="Fix authentication boundary condition",
        scope=["auth/login.py", "auth/tokens.py"],
        expects_tests=True,
    )
    state: State = {
        "ticket": "Ticket #42",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "trajectory": [],
    }

    turn_resp = AIMessage(
        content="Done",
        tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "call_0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_resp])
    node_implement(state, workspace=ws, llm=fake_llm)

    assert len(fake_llm.invocations) == 1
    initial_messages = fake_llm.invocations[0]
    prompt_content = initial_messages[0].content
    assert "Fix authentication boundary condition" in prompt_content
    assert "auth/login.py" in prompt_content
    assert "auth/tokens.py" in prompt_content


# 5. Verify feedback from previous gate failure is passed to the LLM on retry
def test_node_implement_includes_previous_gate_feedback_on_retry():
    ws = MockWorkspace()
    subgoal = Subgoal(
        description="Fix syntax error",
        scope=["calc.py"],
        expects_tests=True,
    )
    state: State = {
        "ticket": "Fix syntax",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "gate_status": "mechanical_failure",
        "last_feedback": "SyntaxError on line 5: invalid syntax",
        "trajectory": [],
    }

    turn_resp = AIMessage(
        content="Fixing syntax",
        tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "call_0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_resp])
    node_implement(state, workspace=ws, llm=fake_llm)

    initial_messages = fake_llm.invocations[0]
    prompt_content = initial_messages[0].content
    assert "SyntaxError on line 5: invalid syntax" in prompt_content


# 6. Verify node_implement pops next subgoal when current is None or previous passed
def test_node_implement_subgoal_queue_management():
    ws = MockWorkspace()
    sg1 = Subgoal(description="Subgoal 1", scope=["a.py"])
    sg2 = Subgoal(description="Subgoal 2", scope=["b.py"])

    # Case A: current_subgoal is None -> pops sg1
    state_a: State = {
        "ticket": "Two steps",
        "plan_queue": [sg1, sg2],
        "current_subgoal": None,
        "trajectory": [],
    }
    turn_resp = AIMessage(
        content="Submitting",
        tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "call_0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_resp])
    out_a = node_implement(state_a, workspace=ws, llm=fake_llm)
    assert out_a["current_subgoal"].description == "Subgoal 1"
    assert len(out_a["plan_queue"]) == 1
    assert out_a["plan_queue"][0].description == "Subgoal 2"

    # Case B: gate_status is "passed" -> pops sg2
    state_b: State = {
        "ticket": "Two steps",
        "plan_queue": [sg2],
        "current_subgoal": sg1,
        "gate_status": "passed",
        "trajectory": [],
    }
    fake_llm_b = ScriptedChatModel(responses=[turn_resp])
    out_b = node_implement(state_b, workspace=ws, llm=fake_llm_b)
    assert out_b["current_subgoal"].description == "Subgoal 2"
    assert len(out_b["plan_queue"]) == 0
    assert out_b["gate_status"] is None


# 7. Verify llm=None does not raise and preserves backwards compatibility
def test_node_implement_llm_none_backwards_compatible():
    ws = MockWorkspace()
    sg = Subgoal(description="No LLM step", scope=["c.py"])
    state: State = {
        "ticket": "Task",
        "plan_queue": [sg],
        "current_subgoal": None,
        "trajectory": [],
    }
    out = node_implement(state, workspace=ws, llm=None)
    assert out["current_subgoal"].description == "No LLM step"
    assert len(ws.staged_mutations) == 0


# 8. Verify circuit breaker limits max turns if submit_subgoal is never called
def test_node_implement_max_turns_circuit_breaker():
    ws = MockWorkspace()
    sg = Subgoal(description="Infinite loop step", scope=["loop.py"])
    state: State = {
        "ticket": "Task",
        "current_subgoal": sg,
        "trajectory": [],
    }

    # Generate 15 consecutive mutations without submit_subgoal
    infinite_responses = [
        AIMessage(
            content=f"Mutation {i}",
            tool_calls=[
                {
                    "name": "stage_file_mutation",
                    "args": {"path": "loop.py", "content": f"# loop {i}"},
                    "id": f"call_{i}",
                }
            ],
        )
        for i in range(15)
    ]
    fake_llm = ScriptedChatModel(responses=infinite_responses)
    node_implement(state, workspace=ws, llm=fake_llm)

    # Should have capped at max_turns (e.g. 10), not drained all 15
    assert len(fake_llm.invocations) <= 10
    assert len(ws.staged_mutations) <= 10


# 9. Verify current_subgoal as dict is properly handled
def test_node_implement_handles_dict_current_subgoal():
    ws = MockWorkspace()
    state: State = {
        "ticket": "Task",
        "current_subgoal": {"description": "Dict subgoal", "scope": ["d.py"], "expects_tests": True},
        "trajectory": [],
    }
    turn_resp = AIMessage(
        content="Submitting",
        tool_calls=[
            {
                "name": "stage_file_mutation",
                "args": {"path": "d.py", "content": "x = 42\n"},
                "id": "c1",
            },
            {"name": "submit_subgoal", "args": {}, "id": "c2"},
        ],
    )
    fake_llm = ScriptedChatModel(responses=[turn_resp])
    out = node_implement(state, workspace=ws, llm=fake_llm)
    assert len(ws.staged_mutations) == 1
    assert ws.staged_mutations[0]["path"] == "d.py"
    assert ws.staged_mutations[0]["content"] == "x = 42\n"
