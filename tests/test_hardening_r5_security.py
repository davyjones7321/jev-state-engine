"""Phase R5 adversarial security regression tests."""

import subprocess
from pathlib import Path
import pytest
from jev.models import Subgoal
from jev.workspace import Workspace


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)
    return repo


class TestR5SubgoalWorktreeContainment:
    """VULN-1: Prevent directory traversal and arbitrary deletion via subgoal_id."""

    def test_subgoal_id_path_traversal_blocked(self, git_repo: Path, tmp_path: Path):
        victim_dir = tmp_path / "victim_external"
        victim_dir.mkdir()
        canary = victim_dir / "canary.txt"
        canary.write_text("CRITICAL_EXTERNAL_DATA", encoding="utf-8")

        ws = Workspace(repo_dir=git_repo)

        # Attempt to reach victim_dir via traversal
        malicious_ids = [
            "x/../../../../victim_external",
            r"x\..\..\..\..\victim_external",
            "/victim_external",
            "../victim_external",
            "subgoal/nested",
            "id with spaces",
            "id;semicolon",
            "",
            "   ",
        ]

        for bad_id in malicious_ids:
            with pytest.raises(ValueError, match=r"Invalid subgoal_id|escapes worktree"):
                ws.create_subgoal_worktree(bad_id)

        # Verify canary and victim directory are untouched
        assert victim_dir.exists(), "Victim directory was unexpectedly deleted!"
        assert canary.read_text(encoding="utf-8") == "CRITICAL_EXTERNAL_DATA"

    def test_subgoal_model_validation(self):
        # Empty id is permitted as default
        sg = Subgoal()
        assert sg.id == ""

        # Valid ids
        assert Subgoal(id="sg-1").id == "sg-1"
        assert Subgoal(id="valid_123_test").id == "valid_123_test"

        # Traversal / invalid ids rejected by model
        for bad_id in ["../traversal", "x/../y", "has space", "bad;char"]:
            with pytest.raises(ValueError):
                Subgoal(id=bad_id)
