import subprocess
import json
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


def test_gate_never_excludes_test_results_from_jev_even_if_workspace_reports_a_pass():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/src/app.py b/src/app.py\n+return 1\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=True, detail="Untested pass: skipped per test_policy=never.",
        checks_run=["compile", "scope"],
        checks={"tests": {"ran": False, "passed": True}},
        test_runner_outcome={"ran": False, "outcome": "NO_TESTS_COLLECTED"},
    )
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.9)
    state: State = {"current_subgoal": Subgoal(description="Change app", scope=["src/app.py"]),
                    "test_policy": "never", "trajectory": []}

    result = node_gate(state, workspace=ws, gatekeeper=gk)

    assert result.get("gate_status") == "passed"
    detail = gk.validate_subgoal.call_args.kwargs["mechanical_detail"]
    assert "tests_excluded_by_policy=true" in detail
    assert "pass" not in detail.lower() and "NO_TESTS_COLLECTED" not in detail
    assert result.get("trajectory", [])[-1]["test_runner"]["outcome"] == "EXCLUDED"


def test_gate_verify_only_required_tests_are_unverified_not_failed():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/src/app.py b/src/app.py\n+return 1\n"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=True, detail="Untested pass: skipped per test_policy=verify-only.",
        checks={"tests": {"ran": False, "passed": True}},
        test_runner_outcome={"ran": False, "outcome": "NO_TESTS_COLLECTED"},
    )
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.9)

    state: State = {"current_subgoal": Subgoal(description="Change app", expects_tests=True),
                    "test_policy": "verify-only", "trajectory": []}
    result = node_gate(state, workspace=ws, gatekeeper=gk)

    assert result.get("gate_status") == "passed"
    assert result.get("trajectory", [])[-1]["test_runner"]["outcome"] == "UNVERIFIED"
    assert '"outcome": "UNVERIFIED"' in gk.validate_subgoal.call_args.kwargs["mechanical_detail"]


def test_gate_auto_forwards_structured_targeted_test_evidence(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")

    class TargetedWorkspace:
        worktree_dir = tmp_path

        def get_staged_diff(self):
            return "diff --git a/src/app.py b/src/app.py\n+return 1\n"

        def run_mechanical_checks(self, subgoal, test_policy="auto", test_targets=None):
            self.targets = test_targets
            return MechanicalCheckResult(
                passed=True, checks_run=["compile", "tests", "scope"],
                test_runner_outcome={"ran": True, "command": ["pytest", "tests/test_app.py"],
                                     "exit_code": 0, "outcome": "PASSED", "new_failures": []},
            )

        def commit_subgoal(self):
            pass

    ws = TargetedWorkspace()
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.9)
    state: State = {"current_subgoal": Subgoal(description="Change app", scope=["src/app.py"]),
                    "test_policy": "auto", "trajectory": []}

    result = node_gate(state, workspace=ws, gatekeeper=gk)

    assert ws.targets == ["tests/test_app.py"]
    evidence = result.get("trajectory", [])[-1]["test_runner"]
    assert evidence["policy"] == "auto"
    assert evidence["ran"] is True
    assert evidence["command"] == ["pytest", "tests/test_app.py"]
    assert evidence["exit_code"] == 0
    assert evidence["new_failures"] == []
    assert json.loads(gk.validate_subgoal.call_args.kwargs["mechanical_detail"].split("Test evidence: ")[1]) == evidence


def test_gate_auto_targets_workspace_run_tests_inside_mechanical_checks(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")

    class WorkspaceWithTargetedRunner:
        worktree_dir = tmp_path

        def get_staged_diff(self):
            return "diff --git a/src/app.py b/src/app.py\n+return 1\n"

        def run_tests(self, targets=None):
            self.selected = targets
            self.last_test_run = {"command": ["pytest", *(targets or [])], "exit_code": 0, "outcome": "PASSED"}
            return TestOutcome.PASSED

        def run_mechanical_checks(self, subgoal, test_policy="auto"):
            self.run_tests()
            return MechanicalCheckResult(passed=True, checks_run=["compile", "tests", "scope"],
                                         test_runner_outcome=self.last_test_run)

        def commit_subgoal(self):
            pass

    ws = WorkspaceWithTargetedRunner()
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.9)
    state: State = {"current_subgoal": Subgoal(description="Change app", scope=["src/app.py"]),
                    "test_policy": "auto", "trajectory": []}

    result = node_gate(state, workspace=ws, gatekeeper=gk)

    assert ws.selected == ["tests/test_app.py"]
    assert result.get("trajectory", [])[-1]["test_runner"]["targets"] == ws.selected
    assert ws.run_tests.__func__ is WorkspaceWithTargetedRunner.run_tests


def test_verify_never_does_not_claim_no_tests_collected_or_failure():
    ws = MagicMock()
    ws.get_cumulative_diff.return_value = "diff --git a/src/app.py b/src/app.py\n+return 1\n"
    ws.check_compile.return_value = {"outcome": "PASSED"}
    ws.last_test_run = {"ran": True, "outcome": "FAILED", "output_tail": "old failure"}
    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.9)

    state: State = {"ticket": "Fix app", "integration_branch": "branch",
                    "test_policy": "never", "trajectory": []}
    result = node_verify(state, workspace=ws, gatekeeper=gk)

    ws.run_tests.assert_not_called()
    assert result.get("status") == "completed"
    output = gk.verify_ticket.call_args.args[2]
    assert "tests_excluded_by_policy=true" in output
    assert "PASSED" not in output and "FAILED" not in output and "NO_TESTS_COLLECTED" not in output
    assert result.get("trajectory", [])[-1]["test_runner"]["outcome"] == "EXCLUDED"


def test_verify_auto_prefers_targeted_runner_and_forwards_evidence(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
    ws = MagicMock()
    ws.worktree_dir = tmp_path
    ws.get_cumulative_diff.return_value = "diff --git a/tests/test_app.py b/tests/test_app.py\n+assert True\n"
    ws.check_compile.return_value = {"outcome": "PASSED"}
    ws.run_targeted_tests.return_value = TestOutcome.PASSED
    ws.last_test_run = {"command": ["pytest", "tests/test_app.py"], "exit_code": 0,
                        "outcome": "PASSED", "base_failures": ["old"], "new_failures": []}
    gk = MagicMock()
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.9)

    state: State = {"ticket": "Fix tests", "integration_branch": "branch",
                    "test_policy": "auto", "trajectory": []}
    result = node_verify(state, workspace=ws, gatekeeper=gk)

    ws.run_targeted_tests.assert_called_once_with(["tests/test_app.py"])
    ws.run_tests.assert_not_called()
    evidence = result.get("trajectory", [])[-1]["test_runner"]
    assert evidence["targets"] == ["tests/test_app.py"]
    assert evidence["base_failures"] == ["old"]
    assert evidence["new_failures"] == []
    assert json.loads(gk.verify_ticket.call_args.args[2].split("Test evidence: ")[1]) == evidence
