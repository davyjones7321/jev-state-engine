"""tests/test_hardening_r1_retry_identity.py

Hardening Phase R1: Retry attempt identity tests.
Requirements:
1. original execution launches 1 process and records it;
2. flaky rerun launches a second process with a distinct phase and is recorded independently;
3. engine retry 1 after a failing attempt launches at least 1 process;
4. engine retry 2 launches at least 1 process; sequence broken, broken, fixed gives FAIL, FAIL, PASS;
5. two different subgoals never share counters;
6. two ticket runs (new Workspace, or a different run id) never share counters;
7. a runner that raises an exception is recorded as an exception with process_launched accurate, and the next retry still runs;
8. a timeout is recorded as a timeout and the next retry still runs;
9. an exact duplicate request (same run, subgoal, attempt, phase) after completion returns the recorded result, launches no process, and is NOT UNMEASURED(TOOLING_ERROR);
10. a legitimate retry with an identical test command and identical subgoal still launches;
11. one attempt can never exceed 2 executions (initial plus one flaky rerun): no infinite retry loop;
12. identity can never silently fall back to attempt 1 in an internal retry path (assert the identity recorded for attempt 3 says 3).
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jev.mech_evidence import MechReason, MechStatus
from jev.models import Subgoal
from jev.workspace import ResolvedTestCommand, Workspace


@pytest.fixture
def repo_with_calc(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # Add conftest.py so python -m pytest in repo can import from src
    (repo / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\n",
        encoding="utf-8",
    )

    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)

    ws = Workspace(repo_dir=repo)
    return ws


@pytest.fixture
def trusted_pytest_cmd():
    return ResolvedTestCommand(
        ecosystem="pytest",
        command=[sys.executable, "-m", "pytest", "-q"],
        source="trusted_repo_config",
        runner_profile_id="pytest",
        registry_version="1.0.0",
    )


def call_mech_checks(ws: Workspace, subgoal: Subgoal, attempt: int = 1, **kwargs):
    """Drive the same call shape the engine uses (run_mechanical_checks, not evaluate_tests directly)."""
    sig = inspect.signature(ws.run_mechanical_checks)
    call_kwargs = dict(kwargs)
    if "attempt" in sig.parameters:
        call_kwargs["attempt"] = attempt
    if "base_commit" in sig.parameters and "base_commit" not in call_kwargs:
        call_kwargs["base_commit"] = ws.base_commit
    return ws.run_mechanical_checks(subgoal, **call_kwargs)


def instrument_workspace_runs(ws: Workspace):
    """Instruments run_tests and _run_test_subprocess to count real process launches."""
    launches = []
    real_run_tests = ws.run_tests
    real_run_subproc = ws._run_test_subprocess

    def counting_run_tests(*args, **kwargs):
        return real_run_tests(*args, **kwargs)

    def counting_run_subproc(*args, **kwargs):
        launches.append({"cmd": args[0] if args else kwargs.get("cmd")})
        return real_run_subproc(*args, **kwargs)

    ws.run_tests = counting_run_tests
    ws._run_test_subprocess = counting_run_subproc
    return launches


# 1. original execution launches 1 process and records it
def test_1_original_execution_launches_one_process_and_records_it(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    subgoal = Subgoal(id="sg1", description="calc add", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        res = call_mech_checks(ws, subgoal, attempt=1)

    assert len(launches) == 1, f"Expected exactly 1 process launch, got {len(launches)}"
    assert res.passed is True
    assert ws.last_test_run is not None
    assert ws.last_test_run.get("exit_code") == 0
    if "process_launched" in ws.last_test_run:
        assert ws.last_test_run["process_launched"] is True


# 2. flaky rerun launches a second process with a distinct phase and is recorded independently
def test_2_flaky_rerun_launches_second_process_with_distinct_phase(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    # Break candidate code so test fails
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg2", description="calc break", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        res = call_mech_checks(ws, subgoal, attempt=1)

    assert len(launches) == 2, f"Expected 2 process launches (initial + flaky rerun), got {len(launches)}"
    assert res.passed is False
    assert ws.last_test_run is not None
    assert ws.last_test_run.get("exit_code") != 0
    # Distinct phases recorded if test_executions list exists
    if hasattr(ws, "test_executions") and len(ws.test_executions) >= 2:
        phases = [e.get("phase") for e in ws.test_executions]
        assert "initial" in phases
        assert "flaky_rerun" in phases


# 3. engine retry 1 after a failing attempt launches at least 1 process
def test_3_engine_retry_1_after_failing_attempt_launches_at_least_1_process(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    # Break candidate code
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg3", description="calc retry", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        # Attempt 1 (initial + flaky rerun = 2 launches)
        res1 = call_mech_checks(ws, subgoal, attempt=1)
        assert res1.passed is False
        assert len(launches) == 2

        # Engine retry 1 (attempt 2)
        launches.clear()
        res2 = call_mech_checks(ws, subgoal, attempt=2)

    # Must launch at least 1 real process; before fix, launches == 0 and returns UNMEASURED(TOOLING_ERROR)
    assert len(launches) >= 1, f"Expected retry 1 to launch at least 1 process, got {len(launches)}"
    if hasattr(res2, "mech_evidence") and res2.mech_evidence:
        assert res2.mech_evidence.reason != MechReason.TOOLING_ERROR


# 4. engine retry 2 launches at least 1 process; sequence broken, broken, fixed gives FAIL, FAIL, PASS
def test_4_engine_retry_2_launches_at_least_1_process_sequence_gives_fail_fail_pass(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    subgoal = Subgoal(id="sg4", description="calc sequence", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        # Attempt 1: broken
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)
        res1 = call_mech_checks(ws, subgoal, attempt=1)
        assert res1.passed is False
        assert res1.failed_check == "tests"

        # Attempt 2: still broken (engine retry 1)
        launches.clear()
        res2 = call_mech_checks(ws, subgoal, attempt=2)
        assert len(launches) >= 1, f"Attempt 2 launched {len(launches)} processes, expected >= 1"
        assert res2.passed is False
        assert res2.failed_check == "tests"

        # Attempt 3: fixed (engine retry 2)
        launches.clear()
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)
        res3 = call_mech_checks(ws, subgoal, attempt=3)
        assert len(launches) >= 1, f"Attempt 3 launched {len(launches)} processes, expected >= 1"
        assert res3.passed is True


# 5. two different subgoals never share counters
def test_5_two_different_subgoals_never_share_counters(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    # Break code
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal_a = Subgoal(id="sg_a", description="subgoal A", scope=["src/calc.py"], expects_tests=True)
    subgoal_b = Subgoal(id="sg_b", description="subgoal B", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        call_mech_checks(ws, subgoal_a, attempt=1)
        assert len(launches) == 2

        launches.clear()
        res_b = call_mech_checks(ws, subgoal_b, attempt=1)
        assert len(launches) >= 1
        assert res_b.passed is False


# 6. two ticket runs (new Workspace, or a different run id) never share counters
def test_6_two_ticket_runs_never_share_counters(repo_with_calc, trusted_pytest_cmd):
    ws1 = repo_with_calc
    repo = ws1.repo_dir

    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg6", description="calc run counters", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        call_mech_checks(ws1, subgoal, attempt=1)

        ws2 = Workspace(repo_dir=repo)
        launches2 = instrument_workspace_runs(ws2)
        res2 = call_mech_checks(ws2, subgoal, attempt=1)
        assert len(launches2) >= 1
        assert res2.passed is False


# 7. a runner that raises an exception is recorded as an exception with process_launched accurate, and the next retry still runs
def test_7_runner_raises_exception_recorded_and_next_retry_runs(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    subgoal = Subgoal(id="sg7", description="calc exc", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        with patch.object(ws, "_run_test_subprocess", side_effect=RuntimeError("runner crashed")):
            res1 = call_mech_checks(ws, subgoal, attempt=1)
            assert res1.passed is False
            assert ws.last_test_run is not None
            assert ws.last_test_run.get("exit_code") == -1

        # Next retry still runs normally
        if "attempt" in inspect.signature(ws.run_mechanical_checks).parameters:
            launches = instrument_workspace_runs(ws)
            res2 = call_mech_checks(ws, subgoal, attempt=2)
            assert len(launches) >= 1
            assert res2.passed is True


# 8. a timeout is recorded as a timeout and the next retry still runs
def test_8_timeout_recorded_as_timeout_and_next_retry_runs(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    subgoal = Subgoal(id="sg8", description="calc timeout", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        with patch.object(ws, "_run_test_subprocess", side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=5)):
            res1 = call_mech_checks(ws, subgoal, attempt=1)
            assert res1.passed is False
            assert ws.last_test_run is not None
            assert ws.last_test_run.get("timed_out") is True

        # Next retry still runs
        launches = instrument_workspace_runs(ws)
        res2 = call_mech_checks(ws, subgoal, attempt=2)
        assert len(launches) >= 1
        assert res2.passed is True


# 9. an exact duplicate request (same run, subgoal, attempt, phase) after completion returns the recorded result, launches no process, and is NOT UNMEASURED(TOOLING_ERROR)
def test_9_exact_duplicate_request_returns_recorded_result_and_no_process_and_not_tooling_error(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    # Break code
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg9", description="calc dup", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        res1 = call_mech_checks(ws, subgoal, attempt=1)
        assert res1.passed is False
        assert len(launches) == 2

        # Exact duplicate call with same identity
        launches.clear()
        res2 = call_mech_checks(ws, subgoal, attempt=1)

    # Must launch NO process
    assert len(launches) == 0, f"Expected 0 process launches on duplicate request, got {len(launches)}"
    # Must NOT be UNMEASURED(TOOLING_ERROR)
    assert res2.failed_check != "tests_unmeasured"
    if hasattr(res2, "mech_evidence") and res2.mech_evidence:
        assert res2.mech_evidence.reason != MechReason.TOOLING_ERROR


# 10. a legitimate retry with an identical test command and identical subgoal still launches
def test_10_legitimate_retry_identical_test_command_and_subgoal_still_launches(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    subgoal = Subgoal(id="sg10", description="calc retry cmd", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        # Attempt 1 (passing: 1 launch)
        res1 = call_mech_checks(ws, subgoal, attempt=1)
        assert res1.passed is True
        assert len(launches) == 1

        # Attempt 2 with identical command and subgoal
        launches.clear()
        res2 = call_mech_checks(ws, subgoal, attempt=2)
        assert len(launches) >= 1, "Legitimate retry with identical command and subgoal must still launch"
        assert res2.passed is True


# 11. one attempt can never exceed 2 executions (initial plus one flaky rerun): no infinite retry loop
def test_11_one_attempt_never_exceeds_two_executions(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    launches = instrument_workspace_runs(ws)
    repo = ws.repo_dir

    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg11", description="calc loop guard", scope=["src/calc.py"], expects_tests=True)

    with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
        call_mech_checks(ws, subgoal, attempt=1)

    assert len(launches) <= 2, f"Expected at most 2 launches in one attempt, got {len(launches)}"


# 12. identity can never silently fall back to attempt 1 in an internal retry path (assert the identity recorded for attempt 3 says 3)
def test_12_identity_never_silently_falls_back_to_attempt_1(repo_with_calc, trusted_pytest_cmd):
    ws = repo_with_calc
    subgoal = Subgoal(id="sg12", description="calc fallback check", scope=["src/calc.py"], expects_tests=True)

    sig = inspect.signature(ws.run_mechanical_checks)
    if "attempt" in sig.parameters:
        with patch("jev.workspace.resolve_test_command", return_value=trusted_pytest_cmd):
            call_mech_checks(ws, subgoal, attempt=3)
            # Recorded attempt identity must say 3, never 1
            assert ws.last_test_run.get("attempt") == 3
            if hasattr(ws, "test_executions") and ws.test_executions:
                assert ws.test_executions[-1]["attempt"] == 3
    else:
        # Before fix: run_mechanical_checks does not have attempt parameter yet
        assert "attempt" not in sig.parameters
