import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from jev.models import State, Subgoal
from jev.workspace import Workspace
from jev.gatekeeper import Gatekeeper
from jev.engine import JevEngine, node_implement
import main


# ==============================================================================
# 0.1 Path Containment & resolve_in_worktree tests
# ==============================================================================

def test_resolve_in_worktree_exists_and_rejects_parent_traversal(tmp_path):
    """0.1: Workspace.resolve_in_worktree must exist and reject ../ traversal."""
    ws = Workspace(repo_dir=tmp_path)
    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("../escape.txt")

    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("../../escape.txt")


def test_resolve_in_worktree_rejects_backslash_parent_traversal(tmp_path):
    """0.1: Workspace.resolve_in_worktree must reject Windows backslash ..\\ traversal."""
    ws = Workspace(repo_dir=tmp_path)
    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("..\\escape.txt")

    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("..\\..\\escape.txt")


def test_resolve_in_worktree_rejects_absolute_paths(tmp_path):
    """0.1: Workspace.resolve_in_worktree must reject absolute paths (POSIX and Windows)."""
    ws = Workspace(repo_dir=tmp_path)
    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("/etc/passwd")

    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("C:/Windows/System32/calc.exe")

    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("C:\\Windows\\System32\\calc.exe")

    # Absolute paths remain invalid even when they name a file inside the
    # worktree; callers must use worktree-relative names exclusively.
    inside = tmp_path / "inside.txt"
    inside.write_text("safe", encoding="utf-8")
    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree(str(inside))


def test_resolve_in_worktree_preserves_dot_files_and_dot_github(tmp_path):
    """0.1: .env must not be stripped to env; .github paths must remain valid within worktree."""
    ws = Workspace(repo_dir=tmp_path)
    resolved_env = ws.resolve_in_worktree(".env")
    assert resolved_env.name == ".env"
    assert resolved_env.name != "env"

    resolved_github = ws.resolve_in_worktree(".github/workflows/ci.yml")
    assert "workflows" in resolved_github.parts or "workflows" in str(resolved_github)
    assert resolved_github.name == "ci.yml"


def test_resolve_in_worktree_rejects_symlinks_pointing_outside(tmp_path):
    """0.1: Symlinks inside the worktree pointing outside must be rejected when resolved."""
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")

    wt = tmp_path / "worktree"
    wt.mkdir()
    ws = Workspace(repo_dir=tmp_path, worktree_dir=wt)

    symlink_path = wt / "symlink_out.txt"
    try:
        symlink_path.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    with pytest.raises((ValueError, PermissionError)):
        ws.resolve_in_worktree("symlink_out.txt")


def test_stage_file_mutation_rejects_escaping_paths(tmp_path):
    """0.1: stage_file_mutation must reject paths escaping worktree and not write to disk."""
    ws = Workspace(repo_dir=tmp_path)
    escape_target = tmp_path.parent / "escape.txt"
    if escape_target.exists():
        escape_target.unlink()

    with pytest.raises((ValueError, PermissionError)):
        ws.stage_file_mutation("../escape.txt", "malicious content")

    assert not escape_target.exists()


def _init_git_repo(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)
    (path / "dummy.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_node_implement_scope_guard_rejects_parent_traversal(tmp_path):
    """0.1: node_implement tool guard must reject ../../x even if scope has 'x'."""
    _init_git_repo(tmp_path)
    ws = Workspace(repo_dir=tmp_path)
    sg = Subgoal(description="Test subgoal", scope=["x"], expects_tests=False)
    state: State = {
        "ticket": "Test ticket",
        "current_subgoal": sg,
        "plan_queue": [sg],
        "trajectory": [],
    }

    # Fake LLM that attempts to call stage_file_mutation with ../../x
    fake_tool_call = {
        "name": "stage_file_mutation",
        "args": {"path": "../../x", "content": "hello"},
        "id": "call_1",
    }
    mock_ai_msg = MagicMock()
    mock_ai_msg.tool_calls = [fake_tool_call]
    mock_ai_msg.content = ""

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    # First invoke returns tool call, second returns empty content to end turn
    mock_llm.invoke.side_effect = [mock_ai_msg, MagicMock(tool_calls=[], content="Done")]

    res_state = node_implement(state, workspace=ws, llm=mock_llm)
    # The tool execution must fail or record access denied, not succeed
    last_step = res_state["trajectory"][-1]
    tool_results = [call for call in last_step.get("tool_calls", []) if call.get("name") == "stage_file_mutation"]
    assert len(tool_results) == 1
    err_msg = str(tool_results[0].get("result", ""))
    assert "Access denied" in err_msg or "outside" in err_msg.lower() or "rejected" in err_msg.lower() or "scope" in err_msg.lower()


def test_node_implement_scope_guard_does_not_strip_dot_env_to_env(tmp_path):
    """0.1: node_implement scope guard must not allow .env when scope only allows env."""
    _init_git_repo(tmp_path)
    ws = Workspace(repo_dir=tmp_path)
    sg = Subgoal(description="Test subgoal", scope=["env"], expects_tests=False)
    state: State = {
        "ticket": "Test ticket",
        "current_subgoal": sg,
        "plan_queue": [sg],
        "trajectory": [],
    }

    fake_tool_call = {
        "name": "stage_file_mutation",
        "args": {"path": ".env", "content": "SECRET=1"},
        "id": "call_1",
    }
    mock_ai_msg = MagicMock()
    mock_ai_msg.tool_calls = [fake_tool_call]
    mock_ai_msg.content = ""

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.invoke.side_effect = [mock_ai_msg, MagicMock(tool_calls=[], content="Done")]

    res_state = node_implement(state, workspace=ws, llm=mock_llm)
    last_step = res_state["trajectory"][-1]
    tool_results = [call for call in last_step.get("tool_calls", []) if call.get("name") == "stage_file_mutation"]
    assert len(tool_results) == 1
    err_msg = str(tool_results[0].get("result", ""))
    # Should be rejected because .env != env
    assert "scope" in err_msg.lower() or "Access denied" in err_msg or "rejected" in err_msg.lower()


# ==============================================================================
# 0.2 Platform Parity & Read Guard tests
# ==============================================================================

def test_run_read_tool_blocks_traversal_even_if_cat_installed(tmp_path, monkeypatch):
    """0.2: run_read_tool must block ../outside.txt on every platform (Linux, Mac, Windows)."""
    outside_file = tmp_path.parent / "secret_outside.txt"
    outside_file.write_text("super_secret", encoding="utf-8")

    ws = Workspace(repo_dir=tmp_path)

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/cat" if cmd == "cat" else None)

    output = ws.run_read_tool("cat", ["../secret_outside.txt"])

    # Must block before subprocess is ever invoked
    assert not mock_run.called, "subprocess.run should not be called for path escaping workspace"
    assert "Access denied" in output or "outside" in output.lower() or "escapes" in output.lower()


@pytest.mark.parametrize(
    "path",
    [
        "..\\secret_outside.txt",
        "node_modules\\..\\..\\secret_outside.txt",
        "/etc/passwd",
        "C:/Windows/System32/calc.exe",
        "C:\\Windows\\System32\\calc.exe",
    ],
)
def test_run_read_tool_blocks_windows_style_traversal_before_subprocess(tmp_path, monkeypatch, path):
    """Read guards apply Windows separators even when running on POSIX."""
    outside_file = tmp_path.parent / "secret_outside.txt"
    outside_file.write_text("super_secret", encoding="utf-8")
    ws = Workspace(repo_dir=tmp_path)
    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/cat" if cmd == "cat" else None)

    output = ws.run_read_tool("cat", [path])

    assert not mock_run.called
    assert "Access denied" in output or "outside" in output.lower()
    assert "super_secret" not in output


def test_run_read_tool_rejects_absolute_path_inside_worktree(tmp_path, monkeypatch):
    """Read access does not bypass the relative-path contract for local files."""
    target = tmp_path / "inside.txt"
    target.write_text("safe", encoding="utf-8")
    ws = Workspace(repo_dir=tmp_path)
    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/cat" if cmd == "cat" else None)

    output = ws.run_read_tool("cat", [str(target)])

    assert not mock_run.called
    assert "Access denied" in output
    assert "safe" not in output


def test_resolve_in_worktree_rejects_symlinked_parent_escape(tmp_path):
    """Containment checks resolve existing parent links before mutation."""
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    ws = Workspace(repo_dir=tmp_path, worktree_dir=worktree)
    link = worktree / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    with pytest.raises((ValueError, PermissionError)):
        ws.stage_file_mutation("linked/created.txt", "must not write")
    assert not (outside / "created.txt").exists()


# ==============================================================================
# 0.3 Per-run Artifacts & JEV_HOME tests
# ==============================================================================

def test_jev_home_directory_and_escalation_path(tmp_path, monkeypatch):
    """0.3: JEV_HOME defaults to ~/.jev and escalation writes to $JEV_HOME/runs/<thread_id>/escalation.json."""
    custom_jev_home = tmp_path / ".custom_jev"
    monkeypatch.setenv("JEV_HOME", str(custom_jev_home))

    gk = Gatekeeper(api_url="https://api.typesafe.ai/v1/systemone", api_key="dummy_key")
    trajectory = [{"step": 1, "action": "test"}]

    thread_id = "test-thread-1234"
    gk.escalate_deadlock(
        trajectory=trajectory,
        triggering_tier="semantic",
        thread_id=thread_id,
    )

    expected_file = custom_jev_home / "runs" / thread_id / "escalation.json"
    assert expected_file.exists(), f"Expected escalation JSON at {expected_file}"
    data = json.loads(expected_file.read_text(encoding="utf-8"))
    assert data["triggering_tier"] == "semantic"
    assert data["trajectory"] == trajectory


def test_cli_default_thread_id_format():
    """0.3: The default thread id becomes run-<utc-timestamp>-<4 hex> unless --thread-id is passed."""
    parser = main.build_parser()
    args = parser.parse_args(["A ticket description"])

    # Must match run-<YYYYMMDDTHHMMSSZ>-<4 hex> e.g. run-20261009T101500Z-a1b2
    pattern = r"^run-\d{8}T\d{6}Z-[0-9a-f]{4}$"
    assert re.match(pattern, args.thread_id), f"Thread ID '{args.thread_id}' did not match pattern '{pattern}'"


# ==============================================================================
# 0.4 Resume Semantics tests
# ==============================================================================

def test_rerun_restarts_with_loud_warning_when_checkpoint_exists(tmp_path, capsys):
    """0.4: If thread already has checkpoints/integration branch, warn loudly and restart."""
    _init_git_repo(tmp_path)
    ws = Workspace(repo_dir=tmp_path)
    engine = JevEngine(workspace=ws, db_path=str(tmp_path / "checkpoints.db"))

    thread_id = "existing-run-id"
    # Seed an initial run or branch
    ws.create_integration_branch(f"jev-ticket-{thread_id}")

    # Second execution on the same thread id must log a loud warning about restarting
    engine.execute("Second attempt ticket", thread_id=thread_id)

    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    assert "restart" in combined_output.lower() or "warning" in combined_output.lower()
    assert thread_id in combined_output


# ==============================================================================
# node_modules Read vs Write containment tests
# ==============================================================================

def test_workspace_run_read_tool_allows_reading_in_node_modules(tmp_path):
    """Workspace.run_read_tool allows reading files resolving inside <repo_dir>/node_modules."""
    _init_git_repo(tmp_path)
    nm_pkg = tmp_path / "node_modules" / "some-lib"
    nm_pkg.mkdir(parents=True, exist_ok=True)
    pkg_file = nm_pkg / "index.js"
    pkg_file.write_text("module.exports = { value: 'from_node_modules' };", encoding="utf-8")

    ws = Workspace(repo_dir=tmp_path)

    # 1. Base workspace read
    out = ws.run_read_tool("cat", ["node_modules/some-lib/index.js"])
    assert "from_node_modules" in out
    assert "Access denied" not in out

    # 2. Subgoal worktree read (node_modules is symlinked/accessible)
    wt_path = ws.create_subgoal_worktree(1)
    try:
        out_wt = ws.run_read_tool("cat", ["node_modules/some-lib/index.js"])
        assert "from_node_modules" in out_wt
        assert "Access denied" not in out_wt
    finally:
        ws.discard_subgoal_worktree(wt_path)


def test_workspace_stage_file_mutation_blocks_writing_to_node_modules(tmp_path):
    """Workspace.stage_file_mutation strictly blocks writing into node_modules."""
    _init_git_repo(tmp_path)
    nm_pkg = tmp_path / "node_modules" / "some-lib"
    nm_pkg.mkdir(parents=True, exist_ok=True)
    pkg_file = nm_pkg / "index.js"
    pkg_file.write_text("initial_clean_code", encoding="utf-8")

    ws = Workspace(repo_dir=tmp_path)

    # 1. Direct write to node_modules must be blocked
    with pytest.raises((PermissionError, ValueError)):
        ws.stage_file_mutation("node_modules/some-lib/index.js", "malicious_overwrite")
    assert pkg_file.read_text(encoding="utf-8") == "initial_clean_code"

    # 2. Inside a worktree, write to node_modules must also be blocked
    wt_path = ws.create_subgoal_worktree(1)
    try:
        with pytest.raises((PermissionError, ValueError)):
            ws.stage_file_mutation("node_modules/some-lib/index.js", "malicious_overwrite")
        assert pkg_file.read_text(encoding="utf-8") == "initial_clean_code"
    finally:
        ws.discard_subgoal_worktree(wt_path)


def test_workspace_run_read_tool_blocks_traversal_through_node_modules(tmp_path):
    """Workspace.run_read_tool strictly blocks node_modules/../../x parent traversal."""
    _init_git_repo(tmp_path)
    nm_dir = tmp_path / "node_modules"
    nm_dir.mkdir(parents=True, exist_ok=True)

    # Create secret outside repo
    secret_file = tmp_path.parent / "super_secret.txt"
    secret_file.write_text("SUPER_SECRET_TOKEN", encoding="utf-8")

    ws = Workspace(repo_dir=tmp_path)

    # Traversal attempting to reach outside repo via node_modules
    out = ws.run_read_tool("cat", [f"node_modules/../../{secret_file.name}"])
    assert "Access denied" in out or "outside" in out.lower()
    assert "SUPER_SECRET_TOKEN" not in out

    # Generic node_modules/../../x traversal
    out_x = ws.run_read_tool("cat", ["node_modules/../../x"])
    assert "Access denied" in out_x or "outside" in out_x.lower()

    # And write traversal must also be blocked
    with pytest.raises((PermissionError, ValueError)):
        ws.stage_file_mutation("node_modules/../../x", "bad")

