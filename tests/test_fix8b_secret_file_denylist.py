"""Regression tests for the Phase S secret-file denylist."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jev.engine import node_implement
from jev.models import Subgoal
from jev.retrieval import is_secret_path
from jev.workspace import Workspace


@pytest.mark.parametrize(
    "path",
    [
        "service-account-prod.json",
        "prod-key.json",
        "db-credentials-prod.json",
        "secrets/api.txt",
        ".secrets/api.txt",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git/config",
        ".docker/config.json",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "prod.tfvars",
        "home/id_ecdsa",
        "home/id_dsa",
        ".gnupg/private-keys-v1.d/key",
        "vault.kdbx",
        "keystore.jks",
        "server.keystore",
    ],
)
def test_new_secret_paths_are_denied(path):
    assert is_secret_path(path)


@pytest.mark.parametrize(
    "path",
    ["README.md", "src/keyboard.py", "src/environment.py", "tests/fixtures/dummy.json", "docs/secrets-policy.md"],
)
def test_normal_paths_and_documented_policy_are_allowed(path):
    assert not is_secret_path(path)


def test_new_secret_path_is_blocked_by_cat_and_stage_file_mutation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets").mkdir()
    (repo / "secrets" / "api.txt").write_text("TOP_SECRET", encoding="utf-8")
    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    output = ws.run_read_tool("cat", ["secrets/api.txt"])
    assert "Access denied to protected secret file" in output
    with pytest.raises(PermissionError, match="Modifying protected secret path"):
        ws.stage_file_mutation("secrets/api.txt", "replacement")


def test_engine_read_file_and_stage_file_mutation_tools_use_denylist(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    class DummyLLM:
        def bind_tools(self, tools):
            self.tools = {tool.name: tool for tool in tools}
            return self

        def invoke(self, messages):
            return MagicMock(tool_calls=[])

    llm = DummyLLM()
    state = {
        "ticket": "implement feature",
        "plan_queue": [Subgoal(id="sg", description="implement", scope=["secrets/new.txt"])],
        "completed_subgoals": [],
        "trajectory": [],
        "status": "in_progress",
    }
    node_implement(state, workspace=ws, llm=llm)
    assert "Access denied to protected secret file" in llm.tools["read_file"].invoke(
        {"path": "secrets/new.txt"}
    )
    assert "Modifying protected secret path" in llm.tools["stage_file_mutation"].invoke(
        {"path": "secrets/new.txt", "content": "secret"}
    )
