import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from jev.models import (
    MechanicalCheckResult,
    Subgoal,
    ValidationVerdict,
)
from jev.workspace import Workspace
from jev.engine import node_gate, node_implement, route_gate


@pytest.fixture
def git_repo(tmp_path):
    """Initializes a temporary git repository with an initial commit on master."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    initial_file = repo_dir / "init.txt"
    initial_file.write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    return repo_dir


# 1. create_subgoal_worktree creates isolated worktree unaffected in main repo
def test_create_subgoal_worktree_isolation(git_repo):
    """create_subgoal_worktree creates an isolated worktree with its own branch, and main repo is unaffected."""
    ws = Workspace(repo_dir=git_repo)
    wt_path = ws.create_subgoal_worktree("iso1")

    assert isinstance(wt_path, Path)
    assert wt_path.exists()
    assert (wt_path / "init.txt").exists()

    # Stage a mutation inside worktree
    ws.stage_file_mutation("new_file.txt", "hello from worktree\n")

    # Verify new_file.txt exists in worktree, but NOT in main repo
    assert (wt_path / "new_file.txt").exists()
    assert not (git_repo / "new_file.txt").exists()

    # Verify git status of main repo is completely clean
    st = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True)
    assert st.stdout.strip() == ""

    # Verify branch in worktree
    br = subprocess.run(["git", "branch", "--show-current"], cwd=wt_path, capture_output=True, text=True)
    assert br.stdout.strip() == "jev-subgoal-iso1"


# 2. merge_subgoal_worktree brings committed changes to main and cleans up
def test_merge_subgoal_worktree(git_repo):
    """merge_subgoal_worktree merges changes back to main repo and removes worktree and branch."""
    ws = Workspace(repo_dir=git_repo)
    wt_path = ws.create_subgoal_worktree("merge1")

    ws.stage_file_mutation("feature.txt", "feature content\n")
    ws.merge_subgoal_worktree(wt_path, "jev-subgoal-merge1")

    # Verify main repo has feature.txt
    assert (git_repo / "feature.txt").exists()
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "feature content\n"

    # Verify main repo log contains commit
    log = subprocess.run(["git", "log", "-1", "--oneline"], cwd=git_repo, capture_output=True, text=True)
    assert "jev-subgoal-merge1" in log.stdout or "Subgoal" in log.stdout

    # Verify worktree directory is removed
    assert not wt_path.exists()

    # Verify branch was deleted
    br_list = subprocess.run(["git", "branch", "--list", "jev-subgoal-merge1"], cwd=git_repo, capture_output=True, text=True)
    assert br_list.stdout.strip() == ""

    # Verify ws.worktree_dir is reset to repo_dir
    assert ws.worktree_dir.resolve() == ws.repo_dir.resolve()


# 3. discard_subgoal_worktree leaves main repo untouched and removes worktree
def test_discard_subgoal_worktree(git_repo):
    """discard_subgoal_worktree discards changes without leaking anything into main repo."""
    ws = Workspace(repo_dir=git_repo)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()

    wt_path = ws.create_subgoal_worktree("discard1")
    ws.stage_file_mutation("junk.txt", "junk content\n")

    ws.discard_subgoal_worktree(wt_path, "jev-subgoal-discard1")

    # Verify junk.txt does NOT exist in main repo
    assert not (git_repo / "junk.txt").exists()

    # Verify worktree directory is removed
    assert not wt_path.exists()

    # Verify HEAD of main repo has not changed
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before

    # Verify branch was deleted
    br_list = subprocess.run(["git", "branch", "--list", "jev-subgoal-discard1"], cwd=git_repo, capture_output=True, text=True)
    assert br_list.stdout.strip() == ""

    # Verify ws.worktree_dir is reset to repo_dir
    assert ws.worktree_dir.resolve() == ws.repo_dir.resolve()


# 4. Failed subgoal worktree discarded and fresh worktree created on retry
def test_failed_subgoal_retry_creates_fresh_worktree(git_repo):
    """A failed subgoal's worktree is discarded and a fresh clean worktree is created on retry."""
    ws = Workspace(repo_dir=git_repo)

    # Attempt 1 fails
    wt1 = ws.create_subgoal_worktree("sub_try1")
    ws.stage_file_mutation("broken.txt", "broken syntax\n")
    ws.discard_subgoal_worktree(wt1, "jev-subgoal-sub_try1")

    assert not wt1.exists()

    # Attempt 2 retry
    wt2 = ws.create_subgoal_worktree("sub_try2")
    assert wt2.exists()
    assert not (wt2 / "broken.txt").exists()  # Fresh worktree, no leftover dirty file

    ws.stage_file_mutation("fixed.txt", "fixed code\n")
    ws.merge_subgoal_worktree(wt2, "jev-subgoal-sub_try2")

    assert (git_repo / "fixed.txt").exists()
    assert not (git_repo / "broken.txt").exists()


# 5. node_gate merges worktree on passed gate
def test_node_gate_merges_worktree_on_pass():
    ws = MagicMock()
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_staged_diff.return_value = "+change"

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    state = {
        "current_subgoal": Subgoal(description="Sub 1", scope=["a.py"]),
        "current_worktree_path": "/path/to/wt",
        "current_worktree_branch": "jev-subgoal-1",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    res = node_gate(state, workspace=ws, gatekeeper=gk)

    assert res["gate_status"] == "passed"
    ws.merge_subgoal_worktree.assert_called_once_with("/path/to/wt", "jev-subgoal-1")
    assert res.get("current_worktree_path") is None
    assert res.get("current_worktree_branch") is None


# 6. node_gate discards worktree on mechanical failure
def test_node_gate_discards_worktree_on_mechanical_fail():
    ws = MagicMock()
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=False, detail="Build err")

    gk = MagicMock()

    state = {
        "current_subgoal": Subgoal(description="Sub 1", scope=["a.py"]),
        "current_worktree_path": "/path/to/wt",
        "current_worktree_branch": "jev-subgoal-1",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    res = node_gate(state, workspace=ws, gatekeeper=gk)

    assert res["gate_status"] == "mechanical_failure"
    ws.discard_subgoal_worktree.assert_called_once_with("/path/to/wt", "jev-subgoal-1")
    assert res.get("current_worktree_path") is None
    assert res.get("current_worktree_branch") is None


# 7. node_gate discards worktree on semantic failure
def test_node_gate_discards_worktree_on_semantic_fail():
    ws = MagicMock()
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_staged_diff.return_value = "+change"

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=False, reason="Semantic reject")

    state = {
        "current_subgoal": Subgoal(description="Sub 1", scope=["a.py"]),
        "current_worktree_path": "/path/to/wt",
        "current_worktree_branch": "jev-subgoal-1",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    res = node_gate(state, workspace=ws, gatekeeper=gk)

    assert res["gate_status"] == "semantic_failure"
    ws.discard_subgoal_worktree.assert_called_once_with("/path/to/wt", "jev-subgoal-1")
    assert res.get("current_worktree_path") is None
    assert res.get("current_worktree_branch") is None


# 8. node_implement creates worktree when none exists in state
def test_node_implement_creates_worktree_when_none():
    ws = MagicMock()
    ws.create_subgoal_worktree.return_value = Path("/tmp/wt_sub1")

    state = {
        "plan_queue": [Subgoal(description="Do something", scope=["a.py"])],
        "trajectory": [],
    }

    res = node_implement(state, workspace=ws, llm=None)

    ws.create_subgoal_worktree.assert_called_once()
    assert res.get("current_worktree_path") == str(Path("/tmp/wt_sub1"))
    assert res.get("current_worktree_branch") is not None
    assert res["current_worktree_branch"].startswith("jev-subgoal-")


# 9. Workspace merge_subgoal_worktree fails loud with RuntimeError on non-fast-forward
def test_non_fast_forward_merge_fails_loud_in_workspace(git_repo):
    """When a commit lands on main mid-worktree, --ff-only refuses to merge and raises RuntimeError."""
    ws = Workspace(repo_dir=git_repo)
    wt_path = ws.create_subgoal_worktree("nff1")

    # Worktree makes changes
    ws.stage_file_mutation("worktree_file.txt", "from worktree\n")

    # Concurrently main moves forward with another commit
    (git_repo / "main_file.txt").write_text("concurrent main commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Interim main commit"], cwd=git_repo, check=True, capture_output=True)

    with pytest.raises(RuntimeError) as exc_info:
        ws.merge_subgoal_worktree(wt_path, "jev-subgoal-nff1")

    assert "fast-forward" in str(exc_info.value).lower()


# 10. Non-fast-forward merge failure escalates cleanly in node_gate rather than crashing
def test_non_fast_forward_merge_escalates_cleanly_in_node_gate(git_repo):
    """When merge fails due to non-fast-forward, node_gate escalates cleanly via escalate_deadlock."""
    ws = Workspace(repo_dir=git_repo)
    wt_path = ws.create_subgoal_worktree("nff_gate")

    ws.stage_file_mutation("worktree_file.txt", "from worktree\n")

    # Concurrently main moves forward
    (git_repo / "main_file.txt").write_text("concurrent main commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Interim main commit"], cwd=git_repo, check=True, capture_output=True)

    state = {
        "current_subgoal": Subgoal(description="Sub 1", scope=["worktree_file.txt"], expects_tests=False),
        "current_worktree_path": str(wt_path),
        "current_worktree_branch": "jev-subgoal-nff_gate",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    # Should not crash / raise unhandled exception
    res = node_gate(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "escalated"
    assert res["gate_status"] == "merge_failed"
    assert "fast-forward" in res["last_feedback"].lower()
    gk.escalate_deadlock.assert_called_once()
    assert gk.escalate_deadlock.call_args[1]["triggering_tier"] == "merge"
    assert res["trajectory"][-1]["error_type"] == "merge_failure"

    # route_gate routes merge_failed to escalate
    assert route_gate(res) == "escalate"

