"""tests/test_hardening_r3_security.py

Regression test suite for Phase R3 security audit and hardening:
1. Python syntax self-check isolates subprocess execution (-I flag prevents cwd module hijacking).
2. run_read_tool restricts execution to authorized read-only commands (blocks rm, bash, python, etc.).
3. run_read_tool blocks mutating git subcommands (push, reset, commit, clean, etc.).
4. stage_file_mutation in node_implement blocks file mutations when subgoal scope is empty.
5. load_proposal rejects path traversal attempts.
6. get_trust_file_path and get_knowledge_db_path reject path traversal in repo_id / repo_key.
7. stage_file_mutation rejects mutating symlink files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jev.engine import node_implement
from jev.knowledge import get_knowledge_db_path
from jev.models import State, Subgoal
from jev.proposals import Proposal, load_proposal
from jev.repo_config import get_trust_dir, get_trust_file_path
from jev.workspace import Workspace


# -----------------------------------------------------------------------------
# 1. Python syntax self-check isolated execution (-I flag)
# -----------------------------------------------------------------------------

def test_syntax_self_check_isolates_python_execution(tmp_path):
    """Verifies that Python syntax self-check executes with -I to prevent cwd module hijacking."""
    # Create an untrusted workspace containing a rogue ast.py
    rogue_ast = tmp_path / "ast.py"
    rogue_ast.write_text("raise RuntimeError('ROGUE AST HIJACKED EXECUTION')\n", encoding="utf-8")

    # Create a valid Python file to check
    valid_file = tmp_path / "app.py"
    valid_file.write_text("def hello(): return 'world'\n", encoding="utf-8")

    # Run the exact checker pattern used in engine.py
    checker = (
        "import ast, pathlib, sys\n"
        "for name in sys.argv[1:]:\n"
        "    source = pathlib.Path(name).read_text(encoding='utf-8')\n"
        "    ast.parse(source, filename=name)\n"
        "    print(f'OK: {name}')\n"
    )

    # First assert that without -I it is vulnerable (demonstrating the defect)
    unisolated = subprocess.run(
        [sys.executable, "-c", checker, "app.py"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert unisolated.returncode != 0
    assert "ROGUE AST HIJACKED EXECUTION" in unisolated.stderr

    # Now verify that passing -I isolates execution and succeeds cleanly
    isolated = subprocess.run(
        [sys.executable, "-I", "-c", checker, "app.py"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert isolated.returncode == 0
    assert "OK: app.py" in isolated.stdout


def test_node_implement_syntax_self_check_uses_isolated_python(tmp_path):
    """Verifies that node_implement's validate_changes tool does not execute rogue ast.py in worktree."""
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    # Put rogue ast.py in worktree root
    (repo / "ast.py").write_text("raise RuntimeError('ROGUE AST HIJACKED EXECUTION')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add calc and rogue ast"], cwd=str(repo), check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    class ValidateToolCallLLM:
        def __init__(self):
            self.val_result = ""

        def bind_tools(self, tools):
            self.tools = {t.name: t for t in tools}
            return self

        def invoke(self, messages):
            self.val_result = self.tools["validate_changes"].invoke({"paths": ["src/calc.py"]})
            self.tools["submit_subgoal"].invoke({"notes": "done"})
            msg = MagicMock()
            msg.content = "done"
            msg.tool_calls = []
            return msg

    fake_llm = ValidateToolCallLLM()

    state: State = {
        "ticket": "Add calc function",
        "plan_queue": [
            Subgoal(
                id="sg-1",
                description="Implement calc",
                scope=["src/calc.py"],
                expects_tests=False,
            )
        ],
        "trajectory": [],
    }

    node_implement(state, workspace=ws, llm=fake_llm)
    assert "ROGUE AST HIJACKED EXECUTION" not in fake_llm.val_result
    assert "Self-check: passed" in fake_llm.val_result


# -----------------------------------------------------------------------------
# 2. run_read_tool restricts command execution to read-only tools
# -----------------------------------------------------------------------------

def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True, capture_output=True)
    return path


def test_run_read_tool_restricts_to_allowed_read_commands(tmp_path):
    """Verifies that run_read_tool rejects unauthorized commands (e.g. rm, bash, python)."""
    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)

    for cmd in ["rm", "bash", "python", "sh", "curl", "chmod"]:
        res = ws.run_read_tool(cmd, ["-h"])
        assert "Access denied" in res or "not an authorized read-only tool" in res.lower(), (
            f"Expected command '{cmd}' to be rejected as unauthorized read tool, got: {res}"
        )


# -----------------------------------------------------------------------------
# 3. run_read_tool blocks mutating git subcommands
# -----------------------------------------------------------------------------

def test_run_read_tool_blocks_mutating_git_subcommands(tmp_path):
    """Verifies that run_read_tool allows read git subcommands but blocks mutating git commands."""
    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)

    # Allowed read-only git commands must succeed
    status_res = ws.run_read_tool("git", ["status", "--porcelain"])
    assert "Access denied" not in status_res

    log_res = ws.run_read_tool("git", ["log", "-1", "--oneline"])
    assert "initial commit" in log_res

    # Mutating / dangerous git subcommands must be blocked
    mutating_subcommands = [
        ["push", "origin", "master"],
        ["reset", "--hard", "HEAD~1"],
        ["commit", "-m", "evil commit"],
        ["clean", "-fdx"],
        ["rebase", "master"],
        ["merge", "origin/master"],
        ["rm", "README.md"],
        ["config", "core.editor", "evil"],
    ]

    for subcmd in mutating_subcommands:
        res = ws.run_read_tool("git", subcmd)
        assert "Access denied" in res or "not an authorized read-only git command" in res.lower(), (
            f"Expected 'git {subcmd[0]}' to be blocked, got: {res}"
        )


# -----------------------------------------------------------------------------
# 4. stage_file_mutation in node_implement blocks mutation when scope is empty
# -----------------------------------------------------------------------------

def test_empty_subgoal_scope_blocks_all_file_mutations(tmp_path):
    """When a subgoal has scope=[] (empty), no files are authorized for mutation."""
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "src").mkdir(parents=True, exist_ok=True)
    target_file = repo / "src" / "app.py"
    target_file.write_text("def app(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(repo), check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    # Fake LLM that calls stage_file_mutation on src/app.py
    class MutateToolCallLLM:
        def __init__(self):
            self.invocations = []

        def bind_tools(self, tools):
            self.tools = {t.name: t for t in tools}
            return self

        def invoke(self, messages):
            # Attempt to mutate src/app.py when scope is empty
            tool = self.tools["stage_file_mutation"]
            res = tool.invoke({"path": "src/app.py", "content": "MALICIOUS CONTENT"})
            # Also submit to finish
            self.tools["submit_subgoal"].invoke({"notes": "done"})

            msg = MagicMock()
            msg.content = f"Tool result: {res}"
            msg.tool_calls = []
            return msg

    fake_llm = MutateToolCallLLM()

    state: State = {
        "ticket": "Documentation only task",
        "plan_queue": [
            Subgoal(
                id="sg-1",
                description="Review docs",
                scope=[],  # EMPTY SCOPE!
                expects_tests=False,
            )
        ],
        "trajectory": [],
    }

    res_state = node_implement(state, workspace=ws, llm=fake_llm)

    # Verify that src/app.py was NOT modified
    assert (ws.worktree_dir / "src" / "app.py").read_text(encoding="utf-8") == "def app(): pass\n"


# -----------------------------------------------------------------------------
# 5. load_proposal rejects path traversal
# -----------------------------------------------------------------------------

def test_load_proposal_path_traversal_rejected(tmp_path):
    """Verifies that load_proposal rejects traversal sequences and does not load files outside .jev/proposals."""
    pdir = tmp_path / ".jev" / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)

    # Create an outside JSON file with a valid proposal payload
    outside_json = tmp_path / "secret_proposal.json"
    valid_proposal = Proposal(
        id="prop-secret",
        command_type="test",
        repo_id="test-repo",
        exact_argv=["pytest"],
        cwd=".",
        source_evidence="Makefile",
        platform=sys.platform,
        identity_hash="hash",
        created_at="2026-10-30T00:00:00Z",
    )
    outside_json.write_text(valid_proposal.model_dump_json(), encoding="utf-8")

    # Attempt to load using directory traversal - should be rejected and return None
    loaded = load_proposal("../../secret_proposal", tmp_path)
    assert loaded is None, f"Expected load_proposal to reject traversal, got: {loaded}"

    loaded_backslash = load_proposal("..\\..\\secret_proposal", tmp_path)
    assert loaded_backslash is None, f"Expected load_proposal to reject backslash traversal, got: {loaded_backslash}"


# -----------------------------------------------------------------------------
# 6. get_trust_file_path and get_knowledge_db_path reject path traversal
# -----------------------------------------------------------------------------

def test_trust_and_knowledge_path_traversal_rejected():
    """Verifies that get_trust_file_path and get_knowledge_db_path reject path traversal in repo_id/repo_key."""
    for evil_id in ["../evil", "..\\evil", "foo/../../bar", "/root/pwn"]:
        with pytest.raises(ValueError, match="path traversal"):
            get_trust_file_path(evil_id)

    for evil_key in ["../evil", "..\\evil", "foo/../../bar", "/root/pwn"]:
        with pytest.raises(ValueError, match="path traversal"):
            get_knowledge_db_path(evil_key)


# -----------------------------------------------------------------------------
# 7. stage_file_mutation rejects mutating symlink files
# -----------------------------------------------------------------------------

def test_stage_file_mutation_rejects_symlinks(tmp_path):
    """Verifies that stage_file_mutation refuses to write to symlinks."""
    repo = _init_git_repo(tmp_path / "repo")
    target = repo / "real_file.txt"
    target.write_text("original content\n", encoding="utf-8")
    symlink_file = repo / "symlink_file.txt"

    try:
        symlink_file.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment")

    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add symlink"], cwd=str(repo), check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    with pytest.raises(PermissionError, match="symlink"):
        ws.stage_file_mutation("symlink_file.txt", "overwritten content")

    # Verify target was untouched
    assert target.read_text(encoding="utf-8") == "original content\n"
