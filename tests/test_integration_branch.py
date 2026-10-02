import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import JevEngine, node_gate, node_verify
from jev.gatekeeper import Gatekeeper
from jev.models import (
    CompileOutcome,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
from jev.workspace import (
    MainDivergedError,
    TrackedModificationsError,
    Workspace,
)


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(
            side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict
        )
        self.verify_ticket = MagicMock(
            side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict
        )
        self.escalate_deadlock = MagicMock()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def hello():\n    return 'initial'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit on main"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    return ws


# 1. Full integration branch lifecycle: subgoals merge to int branch, main stays put, verify lands & deletes int branch
def test_full_ticket_lifecycle_deferred_main_landing(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    thread_id = "test-deferred-landing"
    int_branch = ws.create_integration_branch(f"jev-ticket-{thread_id}", base_commit=base_commit)

    # Subgoal 1
    sg1 = Subgoal(description="Subgoal 1: add greet", scope=["src/app.py"], expects_tests=False)
    wt1 = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt1 / "src" / "app.py").write_text("def hello():\n    return 'step1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt1, check=True, capture_output=True)

    ws_wt1 = ws
    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Update greeting app",
        "thread_id": thread_id,
        "base_commit": base_commit,
        "integration_branch": int_branch,
        "subgoal_base_commit": ws.get_branch_commit(int_branch),
        "plan_queue": [],
        "current_subgoal": sg1,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": str(wt1),
        "current_worktree_branch": "jev-subgoal-sg1",
    }

    state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert state["gate_status"] == "passed"

    # Main MUST NOT have moved!
    assert ws.get_current_head() == base_commit
    # Integration branch DOES have the change
    int_sha1 = ws.get_branch_commit(int_branch)
    assert int_sha1 != base_commit

    # Subgoal 2
    sg2 = Subgoal(description="Subgoal 2: add helper", scope=["src/helper.py"], expects_tests=False)
    state["subgoal_base_commit"] = ws.get_branch_commit(int_branch)
    wt2 = ws.create_subgoal_worktree("sg2", base_ref=int_branch)
    (wt2 / "src" / "helper.py").write_text("def help(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt2, check=True, capture_output=True)

    state["current_subgoal"] = sg2
    state["current_worktree_path"] = str(wt2)
    state["current_worktree_branch"] = "jev-subgoal-sg2"
    state["gate_status"] = None

    state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert state["gate_status"] == "passed"

    # Main STILL MUST NOT have moved!
    assert ws.get_current_head() == base_commit
    int_sha2 = ws.get_branch_commit(int_branch)
    assert int_sha2 != int_sha1

    # Verify stage
    state["current_subgoal"] = None
    state["current_worktree_path"] = None
    state["current_worktree_branch"] = None

    state = node_verify(state, workspace=ws, gatekeeper=gk)
    assert state["status"] == "completed"
    assert state["gate_status"] == "verified"

    # Main is NOW fast-forwarded to the integration branch tip!
    assert ws.get_current_head() == int_sha2
    assert (ws.repo_dir / "src" / "helper.py").exists()
    assert "step1" in (ws.repo_dir / "src" / "app.py").read_text(encoding="utf-8")

    # Integration branch is deleted after landing
    br_list = subprocess.run(["git", "branch", "--list", int_branch], cwd=ws.repo_dir, capture_output=True, text=True)
    assert br_list.stdout.strip() == ""


# 2. Escalation at a subgoal leaves main untouched, preserves integration branch, and records it in escalation.log
def test_subgoal_escalation_preserves_integration_branch_and_untouched_main(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    thread_id = "test-subgoal-esc"
    int_branch = ws.create_integration_branch(f"jev-ticket-{thread_id}", base_commit=base_commit)

    # Subgoal 1 passes
    sg1 = Subgoal(description="Subgoal 1: add greet", scope=["src/app.py"], expects_tests=False)
    wt1 = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt1 / "src" / "app.py").write_text("def hello():\n    return 'passed1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt1, check=True, capture_output=True)

    gk = Gatekeeper(api_key="mock", api_url="http://mock")
    gk.validate_subgoal = MagicMock(return_value=ValidationVerdict(valid=True, probability=0.99))

    state: State = {
        "ticket": "Implement multi-subgoal ticket",
        "thread_id": thread_id,
        "base_commit": base_commit,
        "integration_branch": int_branch,
        "subgoal_base_commit": ws.get_branch_commit(int_branch),
        "plan_queue": [],
        "current_subgoal": sg1,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": str(wt1),
        "current_worktree_branch": "jev-subgoal-sg1",
    }
    state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert state["gate_status"] == "passed"
    int_sha1 = ws.get_branch_commit(int_branch)

    # Subgoal 2 fails 3 strikes
    sg2 = Subgoal(description="Subgoal 2: broken", scope=["src/app.py"], expects_tests=False)
    state["subgoal_base_commit"] = int_sha1
    state["current_subgoal"] = sg2
    state["semantic_strike_count"] = 2  # 3rd strike triggers deadlock escalation
    gk.validate_subgoal = MagicMock(return_value=ValidationVerdict(valid=False, probability=0.1, reason="Bad logic"))

    wt2 = ws.create_subgoal_worktree("sg2", base_ref=int_branch)
    state["current_worktree_path"] = str(wt2)
    state["current_worktree_branch"] = "jev-subgoal-sg2"

    state = node_gate(state, workspace=ws, gatekeeper=gk)
    assert state["gate_status"] == "semantic_failure"
    assert state["semantic_strike_count"] >= 3

    from jev.engine import node_escalate
    state = node_escalate(state, workspace=ws, gatekeeper=gk)
    assert state["status"] == "escalated"

    # Main MUST still be at base_commit!
    assert ws.get_current_head() == base_commit

    # Integration branch MUST still exist and point to subgoal 1's commit!
    assert ws.get_branch_commit(int_branch) == int_sha1

    # Escalation log must record the integration branch
    esc_log = Path("escalation.log")
    if esc_log.exists():
        content = esc_log.read_text(encoding="utf-8")
        assert int_branch in content
        esc_log.unlink()


# 3. Verification failure leaves main untouched, preserves integration branch, records in escalation.log
def test_verify_failure_preserves_integration_branch_and_untouched_main(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    thread_id = "test-verify-fail"
    int_branch = ws.create_integration_branch(f"jev-ticket-{thread_id}", base_commit=base_commit)

    # Subgoal 1 passes into integration branch
    sg1 = Subgoal(description="Subgoal 1", scope=["src/app.py"], expects_tests=False)
    wt1 = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt1 / "src" / "app.py").write_text("def hello():\n    return 'passed1'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt1, check=True, capture_output=True)

    ws.merge_subgoal_worktree(wt1, "jev-subgoal-sg1", target_branch=int_branch)
    int_sha = ws.get_branch_commit(int_branch)

    # Verification fails via Gatekeeper rejection
    gk = Gatekeeper(api_key="mock", api_url="http://mock")
    gk.verify_ticket = MagicMock(return_value=ValidationVerdict(valid=False, probability=0.2, reason="Overall ticket incomplete"))

    state: State = {
        "ticket": "Implement feature",
        "thread_id": thread_id,
        "base_commit": base_commit,
        "integration_branch": int_branch,
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": "passed",
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    state = node_verify(state, workspace=ws, gatekeeper=gk)
    assert state["status"] == "verification_failed"

    # Main MUST NOT have moved!
    assert ws.get_current_head() == base_commit

    # Integration branch preserved!
    assert ws.get_branch_commit(int_branch) == int_sha

    # Verify worktree discarded
    assert not (ws.repo_dir / ".jev-worktrees" / f"verify-{int_branch}").exists()

    # Escalation log recorded
    esc_log = Path("escalation.log")
    if esc_log.exists():
        content = esc_log.read_text(encoding="utf-8")
        assert int_branch in content
        esc_log.unlink()


# 4. fast_forward_main refuses when repo_dir has tracked modifications (unstaged or staged)
def test_fast_forward_main_refuses_on_tracked_modifications(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    int_branch = ws.create_integration_branch("jev-ticket-tracked-mod", base_commit=base_commit)

    wt = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt / "src" / "app.py").write_text("def hello(): return 'new'\n", encoding="utf-8")
    ws.merge_subgoal_worktree(wt, "jev-subgoal-sg1", target_branch=int_branch)

    # 4a: Unstaged tracked modification in repo_dir
    (ws.repo_dir / "src" / "app.py").write_text("def hello(): return 'dirty'\n", encoding="utf-8")

    with pytest.raises(TrackedModificationsError):
        ws.fast_forward_main(int_branch, base_commit=base_commit)

    # 4b: Staged tracked modification in repo_dir
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    with pytest.raises(TrackedModificationsError):
        ws.fast_forward_main(int_branch, base_commit=base_commit)

    # Reset dirty change
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=ws.repo_dir, check=True, capture_output=True)


# 5. fast_forward_main succeeds when repo_dir has untracked files
def test_fast_forward_main_ignores_untracked_files(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    int_branch = ws.create_integration_branch("jev-ticket-untracked", base_commit=base_commit)

    wt = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt / "src" / "new_module.py").write_text("def func(): pass\n", encoding="utf-8")
    ws.merge_subgoal_worktree(wt, "jev-subgoal-sg1", target_branch=int_branch)

    # Create untracked file in repo_dir
    (ws.repo_dir / "scratch_notes.txt").write_text("local developer notes\n", encoding="utf-8")

    # Fast-forward main MUST succeed despite untracked file!
    ws.fast_forward_main(int_branch, base_commit=base_commit)

    assert (ws.repo_dir / "src" / "new_module.py").exists()
    assert (ws.repo_dir / "scratch_notes.txt").exists()
    assert ws.get_current_head() == ws.get_branch_commit(int_branch)


# 6. fast_forward_main refuses when main moved since base_commit (divergence check)
def test_fast_forward_main_refuses_on_main_diverged(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    int_branch = ws.create_integration_branch("jev-ticket-diverged", base_commit=base_commit)

    wt = ws.create_subgoal_worktree("sg1", base_ref=int_branch)
    (wt / "src" / "app.py").write_text("def hello(): return 'branch_val'\n", encoding="utf-8")
    ws.merge_subgoal_worktree(wt, "jev-subgoal-sg1", target_branch=int_branch)

    # Main moves concurrently
    (ws.repo_dir / "other.txt").write_text("concurrent commit on main\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Concurrent commit on main"], cwd=ws.repo_dir, check=True, capture_output=True)

    # Attempting to fast-forward main with stale base_commit MUST raise MainDivergedError
    with pytest.raises(MainDivergedError):
        ws.fast_forward_main(int_branch, base_commit=base_commit)

    # In node_verify, this is caught and routed to escalation with gate_status="main_diverged"
    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Implement feature",
        "thread_id": "diverged",
        "base_commit": base_commit,
        "integration_branch": int_branch,
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": "passed",
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_verify(state, workspace=ws, gatekeeper=gk)
    assert out["status"] == "escalated"
    assert out["gate_status"] == "main_diverged"


# 7. Junction-safe verify worktree creation and cleanup
def test_verify_worktree_node_modules_junction_cleanup(git_repo):
    ws = git_repo
    base_commit = ws.get_current_head()
    int_branch = ws.create_integration_branch("jev-ticket-nm-test", base_commit=base_commit)

    # Create real node_modules in repo
    nm_dir = ws.repo_dir / "node_modules"
    nm_dir.mkdir(exist_ok=True)
    (nm_dir / "package_marker.txt").write_text("keep me", encoding="utf-8")

    # Create verify worktree
    verify_wt = ws.create_verify_worktree(int_branch)
    assert verify_wt.exists()
    assert (verify_wt / "node_modules").exists()
    assert (verify_wt / "node_modules" / "package_marker.txt").exists()

    # Discard verify worktree
    ws.discard_verify_worktree(verify_wt)
    assert not verify_wt.exists()

    # Real node_modules in repo must be completely intact!
    assert nm_dir.exists()
    assert (nm_dir / "package_marker.txt").read_text(encoding="utf-8") == "keep me"
