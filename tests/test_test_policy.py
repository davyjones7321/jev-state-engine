import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main
from jev.engine import JevEngine, node_gate, node_verify
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    TestPolicy,
    ValidationVerdict,
)
from jev.workspace import Workspace

TestPolicy.__test__ = False


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    src = repo / "src"
    src.mkdir()
    app = src / "app.py"
    app.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()


# ==============================================================================
# 1. CLI Parsing Tests
# ==============================================================================

def test_cli_parser_test_policy_defaults_to_auto():
    parser = main.build_parser()
    args = parser.parse_args(["Implement a feature"])
    assert args.test_policy == "auto"


@pytest.mark.parametrize("policy", ["auto", "verify-only", "never", "always"])
def test_cli_parser_accepts_valid_test_policies(policy):
    parser = main.build_parser()
    args = parser.parse_args(["Implement a feature", "--test-policy", policy])
    assert args.test_policy == policy


def test_cli_parser_rejects_invalid_test_policy():
    parser = main.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["Implement a feature", "--test-policy", "invalid-mode"])


# ==============================================================================
# 2. Workspace.run_mechanical_checks Tests
# ==============================================================================

def test_mechanical_checks_test_policy_never_skips_tests(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)

    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=True)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    result = ws.run_mechanical_checks(subgoal, test_policy="never")

    assert result.passed is True
    assert ws.run_tests.call_count == 0
    assert result.checks["tests"]["ran"] is False
    assert result.checks["tests"]["passed"] is True
    assert "never" in result.checks["tests"]["detail"]
    assert result.test_runner_outcome["ran"] is False
    assert "ecosystem" in result.test_runner_outcome
    assert "tests" not in result.checks_run


def test_mechanical_checks_test_policy_verify_only_skips_tests(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)

    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=True)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    result = ws.run_mechanical_checks(subgoal, test_policy="verify-only")

    assert result.passed is True
    assert ws.run_tests.call_count == 0
    assert result.checks["tests"]["ran"] is False
    assert result.checks["tests"]["passed"] is True
    assert "verify-only" in result.checks["tests"]["detail"]
    assert result.test_runner_outcome["ran"] is False
    assert "ecosystem" in result.test_runner_outcome


def test_mechanical_checks_test_policy_auto_honors_expects_tests_false(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)

    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=False)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    result = ws.run_mechanical_checks(subgoal, test_policy="auto")

    assert result.passed is True
    assert ws.run_tests.call_count == 0
    assert result.checks["tests"]["ran"] is False
    assert result.checks["tests"]["passed"] is True


def test_mechanical_checks_test_policy_auto_runs_tests_when_expects_tests_true(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.PASSED)

    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=True)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    result = ws.run_mechanical_checks(subgoal, test_policy="auto")

    assert result.passed is True
    assert ws.run_tests.call_count == 1
    assert result.checks["tests"]["ran"] is True
    assert result.checks["tests"]["passed"] is True
    assert "tests" in result.checks_run


def test_mechanical_checks_test_policy_always_runs_tests_even_if_expects_tests_false(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)

    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=False)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    result = ws.run_mechanical_checks(subgoal, test_policy="always")

    assert result.passed is False
    assert result.failed_check == "tests"
    assert ws.run_tests.call_count == 1
    assert result.checks["tests"]["ran"] is True


# ==============================================================================
# 3. Node Gate & Verify Tests
# ==============================================================================

def test_node_gate_passes_test_policy_and_records_telemetry(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)

    gk = FakeGatekeeper()
    subgoal = Subgoal(description="Add feature", scope=["src/app.py"], expects_tests=True)
    ws.stage_file_mutation("src/app.py", "def add(a, b):\n    return a + b + 0\n")

    state: State = {
        "current_subgoal": subgoal,
        "test_policy": "never",
        "trajectory": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    res = node_gate(state, workspace=ws, gatekeeper=gk)

    assert res["gate_status"] == "passed"
    assert ws.run_tests.call_count == 0
    assert len(res["trajectory"]) == 1
    gate_record = res["trajectory"][0]
    assert gate_record["test_runner"]["ran"] is False
    assert gate_record["tier0_result"]["checks"]["tests"]["ran"] is False


def test_node_verify_skips_tests_when_policy_never(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)
    ws.create_integration_branch("jev-verify-test")

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Add feature",
        "integration_branch": "jev-verify-test",
        "test_policy": "never",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "completed"
    assert res["gate_status"] == "verified"
    assert ws.run_tests.call_count == 0
    assert len(res["trajectory"]) == 1
    verify_record = res["trajectory"][0]
    assert verify_record["test_runner"]["ran"] is False
    gk.verify_ticket.assert_called_once()
    _, call_kwargs = gk.verify_ticket.call_args
    # First arg is ticket, 2nd diff, 3rd test_output
    call_args = gk.verify_ticket.call_args[0]
    test_output = call_args[2]
    assert "test_policy=never" in test_output


def test_node_verify_runs_tests_when_policy_verify_only(git_repo):
    ws = Workspace(repo_dir=git_repo)
    ws.run_tests = MagicMock(return_value=TestOutcome.PASSED)
    ws.create_integration_branch("jev-verify-test-2")

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Add feature",
        "integration_branch": "jev-verify-test-2",
        "test_policy": "verify-only",
        "trajectory": [],
    }

    res = node_verify(state, workspace=ws, gatekeeper=gk)

    assert res["status"] == "completed"
    assert res["gate_status"] == "verified"
    assert ws.run_tests.call_count == 1
    assert len(res["trajectory"]) == 1
    verify_record = res["trajectory"][0]
    assert verify_record["test_runner"].get("ran") is not False
    assert verify_record["test_runner"].get("outcome") == "PASSED"


def test_jev_engine_seeds_test_policy_in_state():
    ws = MagicMock()
    gk = MagicMock()
    engine = JevEngine(workspace=ws, gatekeeper=gk, test_policy="verify-only")
    assert engine.test_policy == "verify-only"
