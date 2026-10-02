import ast
import subprocess
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from jev.engine import (
    JevEngine,
    node_escalate,
    node_gate,
    node_implement,
    node_investigate,
    node_plan,
    node_verify,
)
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    ValidationVerdict,
)
from jev.workspace import Workspace


class RecordingScriptedChatModel:
    """Scripted LLM that records all invoked messages for assertions."""

    def __init__(self, responses: Optional[List[Any]] = None):
        if isinstance(responses, list):
            self.responses = responses
        else:
            self.responses = list(responses or [])
        self.recorded_invocations: List[List[Any]] = []

    def bind_tools(self, tools: List[Any], **kwargs: Any) -> "RecordingScriptedChatModel":
        bound = RecordingScriptedChatModel(responses=self.responses)
        bound.recorded_invocations = self.recorded_invocations
        return bound

    def invoke(self, messages: List[Any], **kwargs: Any) -> Any:
        self.recorded_invocations.append(messages)
        if not self.responses:
            return AIMessage(content="[]")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


class FakeGatekeeper:
    """Fake gatekeeper for testing."""

    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.99)
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.99)
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out: self.verify_verdict)
        self.escalate_deadlock = MagicMock()


# ==============================================================================
# 1. Compiled-graph integration test: investigation_notes survives into plan
# ==============================================================================

def test_compiled_graph_investigation_notes_flow_and_grounding_candidate(tmp_path):
    """Asserts investigation_notes survives graph transitions from investigate -> plan,

    the plan prompt contains those notes, and the grounding check treats auth.ts
    as the candidate file (rejecting ungrounded files with candidate listing, then
    accepting auth.ts).
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    auth_dir = repo_dir / "src" / "lib"
    auth_dir.mkdir(parents=True)
    auth_file = auth_dir / "auth.ts"
    auth_file.write_text("export function login(user: string) { return true; }\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo_dir)

    notes_text = (
        "Investigation complete:\n"
        "Identified target function in candidate file src/lib/auth.ts.\n"
        "Relevant Files:\n"
        "- src/lib/auth.ts (contains login function)"
    )

    responses = [
        # 1. node_investigate response: finishes investigation and sets notes
        AIMessage(content="", tool_calls=[{"name": "finish_investigation", "args": {"summary": notes_text}, "id": "call_inv"}]),
        # 2. node_plan attempt 1: proposes ungrounded 'main.py' to test candidate grounding rejection
        AIMessage(content='[{"description": "Add docstring to existing login function", "scope": ["main.py"], "expects_tests": false}]'),
        # 3. node_plan attempt 2 (retry): proposes candidate 'src/lib/auth.ts' which should be accepted
        AIMessage(content='[{"description": "Add docstring to existing login function", "scope": ["src/lib/auth.ts"], "expects_tests": false}]'),
        # 4. node_implement: stages docstring mutation in worktree
        AIMessage(content="", tool_calls=[
            {"name": "stage_file_mutation", "args": {"path": "src/lib/auth.ts", "content": "/** Logs in user */\nexport function login(user: string) { return true; }\n"}, "id": "call_stage"},
            {"name": "submit_subgoal", "args": {"notes": "Added docstring to login in auth.ts"}, "id": "call_sub"},
        ]),
    ]
    llm = RecordingScriptedChatModel(responses)
    gk = FakeGatekeeper()

    db_path = tmp_path / "checkpoints.db"
    engine = JevEngine(workspace=ws, gatekeeper=gk, llm=llm, db_path=str(db_path))

    ticket = "Add a docstring to one existing function in this project"
    final_state = engine.execute(ticket=ticket, thread_id="test_notes_flow")

    # Assert 1: investigation_notes survived the compiled graph and is present in final state
    assert final_state.get("investigation_notes") == notes_text

    # Assert 2: node_plan received a prompt containing the investigation notes naming src/lib/auth.ts
    plan_invocations = [
        invocation
        for invocation in llm.recorded_invocations
        if invocation and hasattr(invocation[0], "content") and "Ticket Description:" in invocation[0].content
    ]
    assert len(plan_invocations) >= 1
    first_plan_prompt = plan_invocations[0][0].content
    assert "Investigation Notes:" in first_plan_prompt
    assert "src/lib/auth.ts" in first_plan_prompt

    # Assert 3: The retry prompt contained the grounding validation error showing auth.ts as candidate
    assert len(plan_invocations) >= 2
    retry_prompt = plan_invocations[1][-1].content
    assert "scope must reference one of the files investigation identified" in retry_prompt
    assert "src/lib/auth.ts" in retry_prompt

    # Assert 4: Change merged successfully to main on auth.ts
    assert final_state["status"] == "completed"
    content_on_main = (repo_dir / "src" / "lib" / "auth.ts").read_text(encoding="utf-8")
    assert "/** Logs in user */" in content_on_main


# ==============================================================================
# 2. Schema guard tests: all keys returned or written must be in State
# ==============================================================================

def test_schema_guard_static_ast_audit():
    """Static AST guard: asserts EVERY key written to state in src/jev/engine.py

    is declared in State.__annotations__.
    """
    engine_file = Path(__file__).resolve().parent.parent / "src" / "jev" / "engine.py"
    tree = ast.parse(engine_file.read_text(encoding="utf-8"))

    declared_keys = set(State.__annotations__.keys())

    assigned_keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if getattr(node.value, "id", None) == "state":
                if isinstance(node.slice, ast.Constant):
                    assigned_keys.add(node.slice.value)

    undeclared_keys = assigned_keys - declared_keys
    assert not undeclared_keys, (
        f"Found undeclared state keys assigned in src/jev/engine.py: {sorted(undeclared_keys)}. "
        f"All state keys MUST be declared in State TypedDict in src/jev/models.py to prevent "
        f"LangGraph from silently dropping them between nodes."
    )


@pytest.fixture
def base_fixture_state() -> State:
    return {
        "ticket": "Sample task",
        "plan_queue": [
            Subgoal(description="Subgoal 1", scope=["foo.py"], expects_tests=False)
        ],
        "current_subgoal": Subgoal(description="Subgoal 1", scope=["foo.py"], expects_tests=False),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Existing notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }


def test_schema_guard_node_investigate(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    ws = Workspace(repo_dir=repo_dir)
    llm = RecordingScriptedChatModel([
        AIMessage(content="", tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c1"}])
    ])

    out = node_investigate(dict(base_fixture_state), workspace=ws, llm=llm)
    for k in out.keys():
        assert k in State.__annotations__, f"node_investigate returned undeclared key: {k}"


def test_schema_guard_node_plan(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    ws = Workspace(repo_dir=repo_dir)
    llm = RecordingScriptedChatModel([
        AIMessage(content='[{"description": "Subgoal 1", "scope": ["foo.py"], "expects_tests": false}]')
    ])

    out = node_plan(dict(base_fixture_state), workspace=ws, llm=llm)
    for k in out.keys():
        assert k in State.__annotations__, f"node_plan returned undeclared key: {k}"


def test_schema_guard_node_implement(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    foo = repo_dir / "foo.py"
    foo.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo_dir)
    llm = RecordingScriptedChatModel([
        AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {"notes": "done"}, "id": "c1"}])
    ])

    out = node_implement(dict(base_fixture_state), workspace=ws, llm=llm)
    for k in out.keys():
        assert k in State.__annotations__, f"node_implement returned undeclared key: {k}"


def test_schema_guard_node_gate(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    foo = repo_dir / "foo.py"
    foo.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo_dir)
    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=True, probability=0.99))

    out = node_gate(dict(base_fixture_state), workspace=ws, gatekeeper=gk)
    for k in out.keys():
        assert k in State.__annotations__, f"node_gate returned undeclared key: {k}"


def test_schema_guard_node_verify(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    foo = repo_dir / "foo.py"
    foo.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo_dir)
    ws.create_integration_branch("jev-ticket-fixture")
    gk = FakeGatekeeper()

    state = dict(base_fixture_state)
    state["integration_branch"] = "jev-ticket-fixture"
    out = node_verify(state, workspace=ws, gatekeeper=gk)
    for k in out.keys():
        assert k in State.__annotations__, f"node_verify returned undeclared key: {k}"


def test_schema_guard_node_escalate(base_fixture_state, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    ws = Workspace(repo_dir=repo_dir)
    gk = FakeGatekeeper()

    out = node_escalate(dict(base_fixture_state), workspace=ws, gatekeeper=gk)
    for k in out.keys():
        assert k in State.__annotations__, f"node_escalate returned undeclared key: {k}"
