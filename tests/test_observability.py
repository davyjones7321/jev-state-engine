import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from jev.engine import node_gate, node_plan, node_verify
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    ValidationVerdict,
)
from jev.workspace import Workspace


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Looks good")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()


class ScriptedChatModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        if not self.responses:
            return AIMessage(content="[]")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


@pytest.fixture
def git_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    src = repo / "src"
    src.mkdir()
    f = src / "app.py"
    f.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    return Workspace(repo_dir=repo)


def test_gate_records_tier0_and_test_runner_and_jev_on_passing_path(git_workspace):
    """Passing gate: asserts trajectory captures Tier 0 result, test runner outcome,

    Jev request summary, and Jev verdict.
    """
    ws = git_workspace
    subgoal = Subgoal(description="Update app.py", scope=["src/app.py"], expects_tests=False)

    # Modify file and stage it
    target = ws.repo_dir / "src" / "app.py"
    target.write_text("def hello():\n    return 'hello world'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=True, probability=0.92, reason="Approved"))
    state: State = {
        "ticket": "Update greeting",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Existing notes on app.py",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "passed"

    # Find the gate entry in trajectory
    gate_entries = [e for e in out["trajectory"] if e.get("node") == "gate"]
    assert len(gate_entries) >= 1
    entry = gate_entries[-1]

    # (1) Tier 0 result
    assert "tier0_result" in entry
    tier0 = entry["tier0_result"]
    assert tier0["passed"] is True
    assert tier0["failed_check"] is None
    assert "checks" in tier0
    assert "build" in tier0["checks"]
    assert tier0["checks"]["build"]["passed"] is True
    assert tier0["checks"]["build"]["ran"] is True
    assert "tests" in tier0["checks"]
    assert tier0["checks"]["tests"]["passed"] is True
    assert "scope" in tier0["checks"]
    assert tier0["checks"]["scope"]["passed"] is True

    # (2) Test runner outcome
    assert "test_runner" in entry
    tr = entry["test_runner"]
    assert tr is not None
    assert "ecosystem" in tr
    assert "command" in tr
    assert "exit_code" in tr
    assert "output_tail" in tr
    assert "outcome" in tr

    # (3) Jev request summary and full verdict
    assert "jev_request" in entry
    jr = entry["jev_request"]
    assert jr is not None
    assert jr["diff_size"] > 0
    assert jr["investigation_notes_included"] is True
    assert "subgoal" in jr

    assert "jev_verdict" in entry
    jv = entry["jev_verdict"]
    assert jv is not None
    assert jv["valid"] is True
    assert jv["probability"] == 0.92
    assert jv["reason"] == "Approved"


def test_gate_records_tier0_on_build_failure(git_workspace):
    """Failing gate (Tier 0 build failure): asserts trajectory captures build failure

    and checks breakdown, with no Jev request.
    """
    ws = git_workspace
    subgoal = Subgoal(description="Update app.py", scope=["src/app.py"], expects_tests=False)

    # Introduce Python syntax error
    target = ws.repo_dir / "src" / "app.py"
    target.write_text("def hello(:\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Update greeting",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "mechanical_failure"

    gate_entries = [e for e in out["trajectory"] if e.get("node") == "gate"]
    assert len(gate_entries) >= 1
    entry = gate_entries[-1]

    assert "tier0_result" in entry
    tier0 = entry["tier0_result"]
    assert tier0["passed"] is False
    assert tier0["failed_check"] == "build"
    assert tier0["checks"]["build"]["ran"] is True
    assert tier0["checks"]["build"]["passed"] is False
    assert tier0["checks"]["tests"]["ran"] is False
    assert tier0["checks"]["scope"]["ran"] is False

    # Jev was not called
    assert entry.get("jev_request") is None
    assert entry.get("jev_verdict") is None


def test_gate_records_semantic_failure(git_workspace):
    """Failing gate (Tier 1 semantic rejection): asserts trajectory captures Tier 0 pass

    and Jev rejection with probability and reasoning.
    """
    ws = git_workspace
    subgoal = Subgoal(description="Update app.py", scope=["src/app.py"], expects_tests=False)

    target = ws.repo_dir / "src" / "app.py"
    target.write_text("def hello():\n    return 'changed'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=False, probability=0.25, reason="Subgoal not implemented"))
    state: State = {
        "ticket": "Update greeting",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "semantic_failure"

    gate_entries = [e for e in out["trajectory"] if e.get("node") == "gate"]
    assert len(gate_entries) >= 1
    entry = gate_entries[-1]

    assert entry["tier0_result"]["passed"] is True
    assert entry["jev_request"] is not None
    assert entry["jev_request"]["diff_size"] > 0
    assert entry["jev_verdict"]["valid"] is False
    assert entry["jev_verdict"]["probability"] == 0.25
    assert entry["jev_verdict"]["reason"] == "Subgoal not implemented"


def test_verify_records_telemetry_on_pass_and_fail(git_workspace):
    """Verify node: asserts trajectory captures test runner outcome, Jev ticket summary,

    and Jev verdict on both pass and failure.
    """
    ws = git_workspace
    gk_pass = FakeGatekeeper(verify_verdict=ValidationVerdict(valid=True, probability=0.96, reason="All good"))
    state_pass: State = {
        "ticket": "Test ticket",
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": "passed",
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes on system",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out_pass = node_verify(state_pass, workspace=ws, gatekeeper=gk_pass)
    assert out_pass["status"] == "completed"

    verify_entry = [e for e in out_pass["trajectory"] if e.get("node") == "verify"][-1]
    assert "test_runner" in verify_entry
    assert "jev_request" in verify_entry
    assert verify_entry["jev_request"]["ticket"] == "Test ticket"
    assert verify_entry["jev_request"]["investigation_notes_included"] is True
    assert "jev_verdict" in verify_entry
    assert verify_entry["jev_verdict"]["valid"] is True
    assert verify_entry["jev_verdict"]["probability"] == 0.96

    # Now verify failure
    gk_fail = FakeGatekeeper(verify_verdict=ValidationVerdict(valid=False, probability=0.31, reason="Incomplete diff"))
    state_fail: State = {
        "ticket": "Test ticket",
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

    out_fail = node_verify(state_fail, workspace=ws, gatekeeper=gk_fail)
    assert out_fail["status"] == "verification_failed"

    verify_fail_entry = [e for e in out_fail["trajectory"] if e.get("node") == "verify"][-1]
    assert verify_fail_entry["jev_verdict"]["valid"] is False
    assert verify_fail_entry["jev_verdict"]["probability"] == 0.31
    assert verify_fail_entry["jev_verdict"]["reason"] == "Incomplete diff"


def test_plan_records_directory_grounding_accepted(git_workspace):
    """Plan node: asserts directory-grounding check records status (accepted) and reason for each subgoal."""
    ws = git_workspace
    llm = ScriptedChatModel([
        '[{"description": "Create helper", "scope": ["src/helper.py"], "expects_tests": false}]'
    ])
    state: State = {
        "ticket": "Add helper to src",
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Discovered src/ directory with app.py.",
        "investigated_directories": ["src"],
        "investigation_incomplete": False,
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_plan(state, workspace=ws, llm=llm)
    plan_entry = [e for e in out["trajectory"] if e.get("node") == "plan"][-1]

    assert "grounding_checks" in plan_entry
    assert len(plan_entry["grounding_checks"]) == 1
    chk = plan_entry["grounding_checks"][0]
    assert chk["status"] in ("accepted", "corrected", "rejected")
    assert chk["status"] == "accepted"
    assert "reason" in chk
    assert len(chk["reason"]) > 0


def test_plan_records_directory_grounding_rejected(git_workspace):
    """Plan node: asserts directory-grounding check records status (rejected) and reason when planning fails."""
    ws = git_workspace
    # Both attempts output ungrounded alien directory "alien_dir/foo.py"
    llm = ScriptedChatModel([
        '[{"description": "Create alien file", "scope": ["alien_dir/foo.py"], "expects_tests": false}]',
        '[{"description": "Create alien file retry", "scope": ["alien_dir/foo.py"], "expects_tests": false}]',
    ])
    state: State = {
        "ticket": "Add file",
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Discovered src/ directory.",
        "investigated_directories": ["src"],
        "investigation_incomplete": False,
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_plan(state, workspace=ws, llm=llm)
    assert out["gate_status"] == "planning_failed"

    plan_entry = [e for e in out["trajectory"] if e.get("node") == "plan"][-1]
    assert "grounding_checks" in plan_entry
    assert len(plan_entry["grounding_checks"]) >= 1
    chk = plan_entry["grounding_checks"][0]
    assert chk["status"] == "rejected"
    assert "reason" in chk
    assert "alien_dir" in chk["reason"]


def test_gate_records_unit_tests_failure_telemetry(git_workspace):
    """Gate node on unit test failure: asserts tier0 records tests failed,

    and test_runner captures exit code and output tail.
    """
    ws = git_workspace
    subgoal = Subgoal(description="Update app.py", scope=["src/app.py"], expects_tests=True)

    # Add a pytest test that will fail
    test_file = ws.repo_dir / "tests" / "test_app.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_fail():\n    assert False, 'Expected test failure'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add test"], cwd=ws.repo_dir, check=True, capture_output=True)

    target = ws.repo_dir / "src" / "app.py"
    target.write_text("def hello():\n    return 'something'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Update greeting",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "mechanical_failure"

    gate_entry = [e for e in out["trajectory"] if e.get("node") == "gate"][-1]
    assert gate_entry["tier0_result"]["passed"] is False
    assert gate_entry["tier0_result"]["failed_check"] == "tests"
    assert gate_entry["tier0_result"]["checks"]["build"]["passed"] is True
    assert gate_entry["tier0_result"]["checks"]["tests"]["passed"] is False
    assert gate_entry["tier0_result"]["checks"]["scope"]["ran"] is False

    tr = gate_entry["test_runner"]
    assert tr is not None
    assert tr["ecosystem"] == "pytest"
    assert tr["exit_code"] != 0
    assert tr["outcome"] == "FAILED"
    assert "Expected test failure" in tr["output_tail"] or "FAILURES" in tr["output_tail"]


def test_gate_records_scope_failure_telemetry(git_workspace):
    """Gate node on scope failure: asserts build and tests ran and passed,

    scope ran and failed, and test_runner telemetry is present.
    """
    ws = git_workspace
    subgoal = Subgoal(description="Update app.py only", scope=["src/app.py"], expects_tests=False)

    # Touch both src/app.py and an out of scope file src/extra.py
    app_f = ws.repo_dir / "src" / "app.py"
    app_f.write_text("def hello(): return 'ok'\n", encoding="utf-8")
    extra_f = ws.repo_dir / "src" / "extra.py"
    extra_f.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Update greeting",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "mechanical_failure"

    gate_entry = [e for e in out["trajectory"] if e.get("node") == "gate"][-1]
    tier0 = gate_entry["tier0_result"]
    assert tier0["passed"] is False
    assert tier0["failed_check"] == "scope"
    assert tier0["checks"]["build"]["passed"] is True
    assert tier0["checks"]["tests"]["passed"] is True
    assert tier0["checks"]["scope"]["passed"] is False
    assert "extra.py" in tier0["detail"]


def test_workspace_run_tests_records_no_test_framework_outcome(git_workspace, monkeypatch):
    """Workspace.run_tests: asserts NO_TEST_FRAMEWORK is recorded with ecosystem

    and no command when the tool is missing.
    """
    ws = git_workspace
    tf_file = ws.repo_dir / "main.tf"
    tf_file.write_text('resource "null_resource" "test" {}\n', encoding="utf-8")

    # Mock shutil.which so terraform is not found
    import shutil
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    outcome = ws.run_tests()
    assert outcome == TestOutcome.NO_TEST_FRAMEWORK

    assert ws.last_test_run is not None
    assert ws.last_test_run["ecosystem"] == "terraform"
    assert ws.last_test_run["outcome"] == "NO_TEST_FRAMEWORK"
    assert ws.last_test_run["command"] is None
    assert ws.last_test_run["exit_code"] is None

