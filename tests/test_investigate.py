import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

_src_path = str(Path(__file__).resolve().parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from jev.engine import node_investigate
from jev.models import State


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


class MockInvestigateWorkspace:
    """Workspace mock tracking read tool calls and providing sample file system state."""

    def __init__(self, files: Optional[Dict[str, str]] = None, dirs: Optional[Dict[str, List[str]]] = None):
        self.files = files or {}
        self.dirs = dirs or {}
        self.read_tool_calls: List[tuple] = []

    def run_read_tool(self, cmd: str, args: Optional[List[str]] = None) -> str:
        args_list = args or []
        self.read_tool_calls.append((cmd, args_list))
        if cmd == "cat" and args_list:
            path = args_list[0]
            if path in self.files:
                return self.files[path]
            return f"cat: {path}: No such file or directory"
        elif cmd == "git" and len(args_list) >= 2 and args_list[0] == "status":
            return "M mock_file.py"
        return ""

    def list_dir(self, path: str = ".") -> str:
        items = self.dirs.get(path, ["src/", "tests/", "README.md"])
        return "\n".join(items)

    def grep(self, query: str, path: Optional[str] = None) -> str:
        matches = []
        for fpath, content in self.files.items():
            if path and not fpath.startswith(path):
                continue
            for idx, line in enumerate(content.splitlines(), 1):
                if query in line:
                    matches.append(f"{fpath}:{idx}:{line}")
        return "\n".join(matches) if matches else "No matches found."


# 1. Multi-turn investigation: list_dir -> grep -> read_file -> finish_investigation
def test_node_investigate_multi_turn_flow():
    ws = MockInvestigateWorkspace(
        files={"src/app.py": "def main():\n    print('hello world')\n"},
        dirs={".": ["src/", "tests/"]},
    )
    state: State = {
        "ticket": "Investigate main entrypoint",
        "trajectory": [],
    }

    # Turn 1: list_dir
    turn_1 = AIMessage(
        content="Listing files",
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "c1"}],
    )
    # Turn 2: grep
    turn_2 = AIMessage(
        content="Searching for main",
        tool_calls=[{"name": "grep", "args": {"query": "main"}, "id": "c2"}],
    )
    # Turn 3: read_file
    turn_3 = AIMessage(
        content="Reading src/app.py",
        tool_calls=[{"name": "read_file", "args": {"path": "src/app.py"}, "id": "c3"}],
    )
    # Turn 4: finish_investigation
    turn_4 = AIMessage(
        content="Investigation complete.",
        tool_calls=[
            {
                "name": "finish_investigation",
                "args": {"summary": "Entrypoint is src/app.py with main() printing hello."},
                "id": "c4",
            }
        ],
    )

    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2, turn_3, turn_4])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # 4 turns executed
    assert len(fake_llm.invocations) == 4

    # Investigation notes saved to state
    assert "investigation_notes" in result_state
    assert "Entrypoint is src/app.py with main() printing hello." in result_state["investigation_notes"]

    # Trajectory updated with investigate node and tool calls
    assert len(result_state["trajectory"]) == 1
    traj = result_state["trajectory"][0]
    assert traj["node"] == "investigate"
    assert traj["notes"] == "Entrypoint is src/app.py with main() printing hello."
    tool_names = [call["name"] for call in traj.get("tool_calls", [])]
    assert "list_dir" in tool_names
    assert "grep" in tool_names
    assert "read_file" in tool_names
    assert "finish_investigation" in tool_names


# 2. Assert read_file is backed by workspace.run_read_tool()
def test_node_investigate_read_file_backed_by_workspace_run_read_tool():
    ws = MockInvestigateWorkspace(files={"config.json": '{"mode": "debug"}'})
    state: State = {"ticket": "Read config", "trajectory": []}

    turn_1 = AIMessage(
        content="Read config file",
        tool_calls=[{"name": "read_file", "args": {"path": "config.json"}, "id": "c_read"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Config loaded"}, "id": "c_fin"}],
    )

    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # Assert workspace.run_read_tool was called for read_file
    assert len(ws.read_tool_calls) >= 1
    assert any(cmd == "cat" and "config.json" in args for cmd, args in ws.read_tool_calls)
    assert result_state["investigation_notes"] == "Config loaded"


# 3. Assert strictly READ-ONLY tools bound (no write tool like stage_file_mutation)
def test_node_investigate_binds_read_only_tools_no_write_tools():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Check bound tools", "trajectory": []}

    turn_1 = AIMessage(
        content="Finish immediately",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Nothing to do"}, "id": "c0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1])
    node_investigate(state, workspace=ws, llm=fake_llm)

    # Check bound tools directly on the mock LLM
    assert len(fake_llm.bound_tools) >= 4
    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in fake_llm.bound_tools]

    # Must contain required read-only tools
    assert "list_dir" in tool_names
    assert "grep" in tool_names
    assert "read_file" in tool_names
    assert "finish_investigation" in tool_names

    # MUST NOT contain any write tools
    assert "stage_file_mutation" not in tool_names
    assert "write_file" not in tool_names
    assert "edit_file" not in tool_names
    assert "stage_mutation" not in tool_names


# 4. Starting context: prompt includes the ticket description
def test_node_investigate_starting_context_receives_ticket():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Fix issue #404: 404 handler returns 500 error", "trajectory": []}

    turn_1 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Found 404 handler"}, "id": "c0"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1])
    node_investigate(state, workspace=ws, llm=fake_llm)

    assert len(fake_llm.invocations) >= 1
    first_invocation_messages = fake_llm.invocations[0]
    prompt_text = first_invocation_messages[0].content
    assert "Fix issue #404: 404 handler returns 500 error" in prompt_text


# 5. Circuit breaker: respects turn cap if finish_investigation is never called
def test_node_investigate_respects_turn_cap():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Endless loop ticket", "trajectory": []}

    # 15 consecutive list_dir calls without finish_investigation
    endless_responses = [
        AIMessage(
            content=f"Turn {i}",
            tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": f"c_{i}"}],
        )
        for i in range(15)
    ]
    fake_llm = ScriptedChatModel(responses=endless_responses)
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # Must cap at max_turns (10)
    assert len(fake_llm.invocations) <= 10
    assert "investigation_notes" in result_state
    assert len(result_state["trajectory"]) == 1
    traj = result_state["trajectory"][0]
    assert traj["node"] == "investigate"
    assert len(traj.get("tool_calls", [])) <= 10


# 6. Single turn parallel tool calls
def test_node_investigate_single_turn_parallel_tools():
    ws = MockInvestigateWorkspace(files={"info.txt": "Database connection pool settings"})
    state: State = {"ticket": "Quick check", "trajectory": []}

    parallel_turn = AIMessage(
        content="Read info and finish",
        tool_calls=[
            {"name": "read_file", "args": {"path": "info.txt"}, "id": "c1"},
            {"name": "finish_investigation", "args": {"summary": "Info read successfully."}, "id": "c2"},
        ],
    )
    fake_llm = ScriptedChatModel(responses=[parallel_turn])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    assert len(fake_llm.invocations) == 1
    assert result_state["investigation_notes"] == "Info read successfully."


# 7. Backwards compatibility: llm is None does not crash and preserves prior behavior
def test_node_investigate_llm_none_backwards_compatible():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "No LLM ticket", "trajectory": []}

    result_state = node_investigate(state, workspace=ws, llm=None)
    assert "Workspace status" in result_state.get("last_feedback", "")
    assert len(result_state["trajectory"]) == 1
    assert result_state["trajectory"][0]["node"] == "investigate"


# 8. Rejection of unauthorized write tool invocation
def test_node_investigate_rejects_unauthorized_write_tool():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Try illegal write", "trajectory": []}

    turn_1 = AIMessage(
        content="Attempting write",
        tool_calls=[
            {
                "name": "stage_file_mutation",
                "args": {"path": "hacked.py", "content": "malicious code"},
                "id": "c_bad",
            }
        ],
    )
    turn_2 = AIMessage(
        content="Giving up and finishing",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Cannot write"}, "id": "c_fin"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # Verify that in invocation 1, the response was rejected with an error message
    assert len(fake_llm.invocations) == 2
    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    assert any("not permitted" in m.content or "Unknown tool" in m.content for m in tool_msgs)
    assert result_state["investigation_notes"] == "Cannot write"


# 9. Empty file read returns empty string, not error
def test_node_investigate_empty_file_read_returns_empty_string():
    ws = MockInvestigateWorkspace(files={"empty.py": ""})
    state: State = {"ticket": "Read empty file", "trajectory": []}

    turn_1 = AIMessage(
        content="Read empty file",
        tool_calls=[{"name": "read_file", "args": {"path": "empty.py"}, "id": "c_empty"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Empty file verified"}, "id": "c_fin"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    read_msg = [m for m in tool_msgs if m.name == "read_file"][0]
    assert read_msg.content == ""
    assert "Workspace unavailable" not in read_msg.content
    assert result_state["investigation_notes"] == "Empty file verified"


# 10. Turn cap circuit breaker does not pollute notes with tool output
def test_node_investigate_turn_cap_does_not_pollute_notes_with_tool_output():
    ws = MockInvestigateWorkspace(dirs={".": ["fileA.py", "fileB.py"]})
    state: State = {"ticket": "Loop ticket", "trajectory": []}

    endless_responses = [
        AIMessage(
            content=f"Searching turn {i}",
            tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": f"c_{i}"}],
        )
        for i in range(15)
    ]
    fake_llm = ScriptedChatModel(responses=endless_responses)
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # Investigation notes must NOT be raw stdout of list_dir ("fileA.py\nfileB.py")
    assert result_state["investigation_notes"] != "fileA.py\nfileB.py"
    assert "Searching turn 9" in result_state["investigation_notes"] or "turn" in result_state["investigation_notes"].lower()


# 11. finish_investigation with empty args extracts summary from AIMessage.content
def test_node_investigate_finish_empty_args_extracts_aimessage_content():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Summary in text", "trajectory": []}

    turn_1 = AIMessage(
        content="Comprehensive analysis: Architecture uses FSM and LangGraph with 4 distinct phases.",
        tool_calls=[{"name": "finish_investigation", "args": {}, "id": "c_fin"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    # Must NOT be "Investigation finished."
    assert result_state["investigation_notes"] != "Investigation finished."
    assert "Comprehensive analysis: Architecture uses FSM and LangGraph" in result_state["investigation_notes"]


# 12. finish_investigation accepts alternative parameter names
def test_node_investigate_finish_alternative_param_names():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "Alt param test", "trajectory": []}

    turn_1 = AIMessage(
        content="",
        tool_calls=[{"name": "finish_investigation", "args": {"findings": "Identified root cause in auth service."}, "id": "c1"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)
    assert result_state["investigation_notes"] == "Identified root cause in auth service."


# 13. AIMessage with list content blocks normalized to string
def test_node_investigate_list_content_normalized_to_str():
    ws = MockInvestigateWorkspace()
    state: State = {"ticket": "List content test", "trajectory": []}

    turn_1 = AIMessage(
        content=[{"type": "text", "text": "Structured summary block 1"}, {"type": "text", "text": "Structured summary block 2"}],
        tool_calls=[{"name": "finish_investigation", "args": {}, "id": "c1"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1])
    result_state = node_investigate(state, workspace=ws, llm=fake_llm)

    assert isinstance(result_state["investigation_notes"], str)
    assert "Structured summary block 1" in result_state["investigation_notes"]
    assert "Structured summary block 2" in result_state["investigation_notes"]


# 14. read_file blocks path traversal outside workspace
def test_node_investigate_read_file_blocks_path_traversal(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    from jev.workspace import Workspace
    real_ws = Workspace(worktree_dir=ws_dir)

    state: State = {"ticket": "Traversal test", "trajectory": []}
    turn_1 = AIMessage(
        content="Read outside file",
        tool_calls=[{"name": "read_file", "args": {"path": "../secret.txt"}, "id": "c_trav"}],
    )
    turn_2 = AIMessage(
        content="Finish",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c_fin"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=real_ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    read_msg = [m for m in tool_msgs if m.name == "read_file"][0]
    assert "super_secret" not in read_msg.content
    assert "denied" in read_msg.content.lower() or "not found" in read_msg.content.lower()


# 15. grep passes --untracked to find untracked files
def test_node_investigate_grep_passes_untracked():
    class TrackingMockWS:
        def __init__(self):
            self.git_calls = []
        def run_read_tool(self, cmd, args=None):
            if cmd == "git":
                self.git_calls.append(list(args or []))
                return "tracked_file.py:1:needle"
            return ""

    ws = TrackingMockWS()
    state: State = {"ticket": "Grep untracked test", "trajectory": []}

    turn_1 = AIMessage(
        content="Grep needle",
        tool_calls=[{"name": "grep", "args": {"query": "needle"}, "id": "c_grep"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Found"}, "id": "c_fin"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=ws, llm=fake_llm)

    assert len(ws.git_calls) >= 1
    grep_args = ws.git_calls[0]
    assert "grep" in grep_args
    assert "--untracked" in grep_args


# 16. Workspace.run_read_tool handling for directory, empty file, and outside traversal
def test_workspace_run_read_tool_cat_directory_empty_and_traversal(tmp_path):
    from jev.workspace import Workspace

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    empty_f = ws_dir / "empty.txt"
    empty_f.write_text("", encoding="utf-8")
    sub_dir = ws_dir / "subdir"
    sub_dir.mkdir()

    outside_f = tmp_path / "outside.txt"
    outside_f.write_text("secret", encoding="utf-8")

    ws = Workspace(worktree_dir=ws_dir)

    # Empty file returns empty string
    res_empty = ws.run_read_tool("cat", ["empty.txt"])
    assert res_empty == ""

    # Directory returns Is a directory
    res_dir = ws.run_read_tool("cat", ["subdir"])
    assert "directory" in res_dir.lower()

    # Traversal outside workspace is blocked
    res_trav = ws.run_read_tool("cat", ["../outside.txt"])
    assert "secret" not in res_trav


# 17. grep blocks relative ../ traversal in git-backed path
def test_node_investigate_grep_blocks_relative_traversal_git_backed(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret_git", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    class TrackingGitWS:
        def __init__(self, worktree_dir):
            self.worktree_dir = worktree_dir
            self.git_calls = []

        def run_read_tool(self, cmd, args=None):
            if cmd == "git":
                self.git_calls.append(list(args or []))
                return "found_file.py:1:super_secret_git"
            return ""

    ws = TrackingGitWS(worktree_dir=ws_dir)
    state: State = {"ticket": "Grep traversal git test", "trajectory": []}

    turn_1 = AIMessage(
        content="Grep outside file",
        tool_calls=[{"name": "grep", "args": {"query": "super_secret_git", "path": "../secret.txt"}, "id": "c1"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c2"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    grep_msg = [m for m in tool_msgs if m.name == "grep"][0]
    assert grep_msg.content == "Error: Access denied for path outside workspace: ../secret.txt"
    assert len(ws.git_calls) == 0


# 18. grep blocks absolute path outside workspace in git-backed path
def test_node_investigate_grep_blocks_absolute_path_outside_git_backed(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret_git_abs", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    class TrackingGitWS:
        def __init__(self, worktree_dir):
            self.worktree_dir = worktree_dir
            self.git_calls = []

        def run_read_tool(self, cmd, args=None):
            if cmd == "git":
                self.git_calls.append(list(args or []))
                return "found_file.py:1:super_secret_git_abs"
            return ""

    ws = TrackingGitWS(worktree_dir=ws_dir)
    state: State = {"ticket": "Grep absolute git test", "trajectory": []}

    turn_1 = AIMessage(
        content="Grep outside file",
        tool_calls=[{"name": "grep", "args": {"query": "super_secret_git_abs", "path": str(outside_file)}, "id": "c1"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c2"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    grep_msg = [m for m in tool_msgs if m.name == "grep"][0]
    assert grep_msg.content == f"Error: Access denied for path outside workspace: {outside_file}"
    assert len(ws.git_calls) == 0


# 19. grep blocks relative ../ traversal in regex fallback path
def test_node_investigate_grep_blocks_relative_traversal_regex_fallback(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret_regex", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    from jev.workspace import Workspace
    real_ws = Workspace(worktree_dir=ws_dir)

    state: State = {"ticket": "Grep regex traversal test", "trajectory": []}
    turn_1 = AIMessage(
        content="Grep outside file",
        tool_calls=[{"name": "grep", "args": {"query": "super_secret_regex", "path": "../secret.txt"}, "id": "c1"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c2"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=real_ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    grep_msg = [m for m in tool_msgs if m.name == "grep"][0]
    assert grep_msg.content == "Error: Access denied for path outside workspace: ../secret.txt"
    assert "super_secret_regex" not in grep_msg.content


# 20. grep blocks absolute path outside workspace in regex fallback path
def test_node_investigate_grep_blocks_absolute_path_outside_regex_fallback(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("super_secret_regex_abs", encoding="utf-8")

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    from jev.workspace import Workspace
    real_ws = Workspace(worktree_dir=ws_dir)

    state: State = {"ticket": "Grep regex abs test", "trajectory": []}
    turn_1 = AIMessage(
        content="Grep outside file",
        tool_calls=[{"name": "grep", "args": {"query": "super_secret_regex_abs", "path": str(outside_file)}, "id": "c1"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c2"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=real_ws, llm=fake_llm)

    tool_msgs = [m for m in fake_llm.invocations[1] if isinstance(m, ToolMessage)]
    grep_msg = [m for m in tool_msgs if m.name == "grep"][0]
    assert grep_msg.content == f"Error: Access denied for path outside workspace: {outside_file}"
    assert "super_secret_regex_abs" not in grep_msg.content


# 21. grep passes safe relative POSIX path to git when absolute path inside workspace is provided
def test_node_investigate_grep_git_backed_passes_safe_relative_posix_path(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    sub_dir = ws_dir / "subdir"
    sub_dir.mkdir()

    class TrackingGitWS:
        def __init__(self, worktree_dir):
            self.worktree_dir = worktree_dir
            self.git_calls = []

        def run_read_tool(self, cmd, args=None):
            if cmd == "git":
                self.git_calls.append(list(args or []))
                return "subdir/file.py:1:needle_in_sub"
            return ""

    ws = TrackingGitWS(worktree_dir=ws_dir)
    state: State = {"ticket": "Grep safe posix test", "trajectory": []}

    turn_1 = AIMessage(
        content="Grep subdir via absolute path",
        tool_calls=[{"name": "grep", "args": {"query": "needle", "path": str(sub_dir)}, "id": "c1"}],
    )
    turn_2 = AIMessage(
        content="Done",
        tool_calls=[{"name": "finish_investigation", "args": {"summary": "Done"}, "id": "c2"}],
    )
    fake_llm = ScriptedChatModel(responses=[turn_1, turn_2])
    node_investigate(state, workspace=ws, llm=fake_llm)

    assert len(ws.git_calls) == 1
    git_args = ws.git_calls[0]
    assert git_args == ["grep", "-n", "-I", "--untracked", "needle", "--", "subdir"]



