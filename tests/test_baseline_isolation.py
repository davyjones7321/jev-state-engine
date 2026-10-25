import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import _snapshot_attempt, node_gate
from jev.ledger import SqliteLedger
from jev.mech_evidence import BaseStatus, MechStatus
from jev.models import MechanicalCheckResult, State, Subgoal, TestOutcome, ValidationVerdict
from jev.workspace import Workspace, parse_failing_test_ids


@pytest.fixture
def git_repo(tmp_path):
    """Initializes a temporary git repository with an initial commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    initial_file = repo_dir / "init.txt"
    initial_file.write_text("initial content", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    return repo_dir


# a. Baseline failure exists before change, same failure after -> REGRESSION_FREE_WITH_BASELINE_FAILURES, full_suite_clean=false
def test_a_baseline_failure_exists_same_failure_after(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    subgoal = Subgoal(id="sg-1", description="Implement feature", scope=["app.py"], expects_tests=True)
    ws.stage_file_mutation("app.py", "x = 1\n")

    cand_run = {
        "ran": True,
        "exit_code": 1,
        "output_tail": "FAILED tests/test_foo.py::test_preexisting",
        "outcome": TestOutcome.FAILED.value,
        "failing_test_ids": ["tests/test_foo.py::test_preexisting"],
        "argv": ["pytest"],
        "cwd": str(ws.worktree_dir),
        "tool_path": "/bin/pytest",
    }
    base_run_data = {
        "base_status": BaseStatus.FAIL,
        "failing_ids": ["tests/test_foo.py::test_preexisting"],
        "valid_cache": True,
        "disagreed": False,
    }
    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.FAILED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests), \
         patch.object(ws, "_run_detached_base_tests", return_value=base_run_data):
        res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    assert res.passed is True
    assert res.failed_check is None
    assert res.test_classification == TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
    assert res.full_suite_clean is False
    assert res.baseline_failures == 1
    assert res.new_failures == 0
    assert res.baseline_failing_ids == ["tests/test_foo.py::test_preexisting"]
    assert res.new_failing_ids == []


# b. The "my-harness" case: 5 baseline failures + new cache code with passing targeted test -> regression-free, no mechanical strike
def test_b_my_harness_case_baseline_failures_with_passing_targeted_test(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    # 5 pre-existing broken tests in the repo baseline
    five_baseline_failures = {f"tests/test_legacy.py::test_broken_{i}" for i in range(1, 6)}
    ws._baseline_test_cache[base_commit] = {
        "outcome": TestOutcome.FAILED.value,
        "failing_ids": five_baseline_failures,
        "ran": True,
    }

    # Worker writes new cache code and targeted tests pass
    subgoal = Subgoal(id="sg-cache", description="Add caching layer", scope=["cache.py"], expects_tests=True)
    ws.stage_file_mutation("cache.py", "class Cache:\n    pass\n")

    cand_run = {
        "ran": True,
        "exit_code": 0,
        "output_tail": "tests/test_cache.py::test_cache_hit PASSED",
        "outcome": TestOutcome.PASSED.value,
        "failing_test_ids": [],
    }

    gatekeeper = MagicMock()
    gatekeeper.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.95)

    state: State = {
        "ticket": "Implement cache layer",
        "subgoal_base_commit": base_commit,
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "auto",
        "integration_branch": "master",
    }

    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.PASSED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests):
        out_state = node_gate(state, workspace=ws, gatekeeper=gatekeeper)

    # Gate proceeds without mechanical strike!
    assert out_state.get("mechanical_strike_count") == 0
    assert out_state.get("test_classification") != TestOutcome.FULL_SUITE_PASSED.value
    assert out_state.get("test_classification") == TestOutcome.PASSED_EXIT_ONLY.value
    assert out_state.get("full_suite_clean") is not True
    assert out_state.get("full_suite_clean") is None
    assert out_state.get("baseline_failures") == 0
    assert out_state.get("new_failures") == 0
    assert gatekeeper.validate_subgoal.called


# c. New failing test introduced by the worker's diff -> REGRESSION_DETECTED, mechanical strike recorded
def test_c_new_failing_test_introduced_regression_detected(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    # Baseline was clean
    ws._baseline_test_cache[base_commit] = {
        "outcome": TestOutcome.FULL_SUITE_PASSED.value,
        "failing_ids": set(),
        "ran": True,
    }

    subgoal = Subgoal(id="sg-1", description="Change logic", scope=["logic.py"], expects_tests=True)
    ws.stage_file_mutation("logic.py", "x = 42\n")

    # Worker introduced new failing test
    cand_run = {
        "ran": True,
        "exit_code": 1,
        "output_tail": "FAILED tests/test_logic.py::test_new_bug",
        "outcome": TestOutcome.FAILED.value,
        "failing_test_ids": ["tests/test_logic.py::test_new_bug"],
        "argv": ["pytest"],
        "cwd": str(ws.worktree_dir),
        "tool_path": "/bin/pytest",
    }

    gatekeeper = MagicMock()
    state: State = {
        "ticket": "Update logic",
        "subgoal_base_commit": base_commit,
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "auto",
    }

    base_run_data = {
        "base_status": BaseStatus.PASS,
        "failing_ids": [],
        "valid_cache": True,
        "disagreed": False,
    }
    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.FAILED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests), \
         patch.object(ws, "_run_detached_base_tests", return_value=base_run_data):
        out_state = node_gate(state, workspace=ws, gatekeeper=gatekeeper)

    assert out_state.get("mechanical_strike_count") == 1
    assert out_state.get("gate_status") == "mechanical_failure"
    assert out_state.get("test_classification") == TestOutcome.REGRESSION_DETECTED.value
    assert out_state.get("new_failures") == 1
    assert out_state.get("baseline_failures") == 0
    # Gatekeeper must NOT be invoked on Tier 0 failure
    assert not gatekeeper.validate_subgoal.called


# d. Same count, different test: baseline had {A, B} failing, after has {A, C} failing -> REGRESSION_DETECTED
def test_d_same_count_different_test_regression_detected(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    # Baseline had tests A and B failing (count = 2)
    ws._baseline_test_cache[base_commit] = {
        "outcome": TestOutcome.FAILED.value,
        "failing_ids": {"tests/test_mod.py::test_A", "tests/test_mod.py::test_B"},
        "ran": True,
    }

    subgoal = Subgoal(id="sg-1", description="Fix A but break C", scope=["mod.py"], expects_tests=True)
    ws.stage_file_mutation("mod.py", "val = True\n")

    # Candidate has tests A and C failing (count = 2, same count!)
    cand_run = {
        "ran": True,
        "exit_code": 1,
        "output_tail": "FAILED tests/test_mod.py::test_A\nFAILED tests/test_mod.py::test_C",
        "outcome": TestOutcome.FAILED.value,
        "failing_test_ids": ["tests/test_mod.py::test_A", "tests/test_mod.py::test_C"],
        "argv": ["pytest"],
        "cwd": str(ws.worktree_dir),
        "tool_path": "/bin/pytest",
    }

    base_run_data = {
        "base_status": BaseStatus.FAIL,
        "failing_ids": ["tests/test_mod.py::test_A", "tests/test_mod.py::test_B"],
        "valid_cache": True,
        "disagreed": False,
    }
    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.FAILED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests), \
         patch.object(ws, "_run_detached_base_tests", return_value=base_run_data):
        res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Comparing sets proves test_C is a regression, despite identical count of 2!
    assert res.passed is False
    assert res.failed_check == "tests"
    assert res.test_classification == TestOutcome.REGRESSION_DETECTED.value
    assert res.baseline_failures == 2
    assert res.new_failures == 1
    assert res.new_failing_ids == ["tests/test_mod.py::test_C"]


# e. Clean repo: all tests pass -> FULL_SUITE_PASSED, full_suite_clean=true
def test_e_clean_repo_all_tests_pass_full_suite_passed(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    ws._baseline_test_cache[base_commit] = {
        "outcome": TestOutcome.FULL_SUITE_PASSED.value,
        "failing_ids": set(),
        "ran": True,
    }

    subgoal = Subgoal(id="sg-1", description="Clean implementation", scope=["clean.py"], expects_tests=True)
    ws.stage_file_mutation("clean.py", "a = 10\n")

    cand_run = {
        "ran": True,
        "exit_code": 0,
        "output_tail": "=== 5 passed in 0.1s ===",
        "outcome": TestOutcome.PASSED.value,
        "failing_test_ids": [],
    }

    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.PASSED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests):
        res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    assert res.passed is True
    assert res.failed_check is None
    assert res.test_classification == TestOutcome.PASSED_EXIT_ONLY.value
    assert res.full_suite_clean is None
    assert res.baseline_failures == 0
    assert res.new_failures == 0


# f. expects_tests=true + no targeted test -> NO_TESTS_COLLECTED, mechanical strike
def test_f_expects_tests_true_no_targeted_test_collected(git_repo):
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(id="sg-1", description="Untested change", scope=["feature.py"], expects_tests=True)
    ws.stage_file_mutation("feature.py", "val = 1\n")

    state: State = {
        "ticket": "Implement feature",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "auto",
    }

    gatekeeper = MagicMock()

    cand_run = {
        "ran": True,
        "exit_code": 5,
        "output_tail": "no tests collected",
        "outcome": TestOutcome.NO_TESTS_COLLECTED.value,
        "failing_test_ids": [],
    }
    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.NO_TESTS_COLLECTED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests):
        out_state = node_gate(state, workspace=ws, gatekeeper=gatekeeper)

    assert out_state.get("mechanical_strike_count") == 1
    assert out_state.get("gate_status") == "mechanical_failure"
    assert not gatekeeper.validate_subgoal.called


# g. expects_tests=false + no targeted test -> untested, passes mechanical, explicit untested evidence to Jev
def test_g_expects_tests_false_no_targeted_test_untested_evidence(git_repo):
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(id="sg-doc", description="Update README docs", scope=["README.md"], expects_tests=False)
    ws.stage_file_mutation("README.md", "# Documentation\n")

    gatekeeper = MagicMock()
    gatekeeper.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    state: State = {
        "ticket": "Update documentation",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "auto",
        "integration_branch": "master",
    }

    out_state = node_gate(state, workspace=ws, gatekeeper=gatekeeper)

    # Passes mechanical Tier 0 and calls gatekeeper with explicit untested evidence
    assert out_state.get("mechanical_strike_count") == 0
    assert gatekeeper.validate_subgoal.called
    call_kwargs = gatekeeper.validate_subgoal.call_args[1]
    mech_detail = call_kwargs.get("mechanical_detail", "")
    assert "Untested pass" in mech_detail or "expects_tests=False" in mech_detail


# h. test_policy=never -> EXEMPT_BY_POLICY, no baseline run happens
def test_h_test_policy_never_exempt_by_policy(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    # run_baseline_tests with never policy must return EXEMPT_BY_POLICY without running tests
    with patch.object(ws, "run_tests") as mock_runner:
        base_res = ws.run_baseline_tests(base_commit=base_commit, test_policy="never")
        assert base_res["outcome"] == TestOutcome.EXEMPT_BY_POLICY.value
        assert mock_runner.call_count == 0

    subgoal = Subgoal(id="sg-1", description="Fast change", scope=["quick.py"], expects_tests=True)
    ws.stage_file_mutation("quick.py", "val = 'fast'\n")

    with patch.object(ws, "run_tests") as mock_runner:
        mech_res = ws.run_mechanical_checks(subgoal, base_commit=base_commit, test_policy="never")
        assert mech_res.passed is True
        assert mech_res.test_classification == TestOutcome.EXEMPT_BY_POLICY.value
        assert mock_runner.call_count == 0


# i. Test IDs unparseable from failing run -> fails closed, REGRESSION_DETECTED
def test_i_test_ids_unparseable_fails_closed_regression_detected(git_repo):
    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    # Baseline was clean
    ws._baseline_test_cache[base_commit] = {
        "outcome": TestOutcome.FULL_SUITE_PASSED.value,
        "failing_ids": set(),
        "ran": True,
    }

    subgoal = Subgoal(id="sg-1", description="Code mutation", scope=["mod.py"], expects_tests=True)
    ws.stage_file_mutation("mod.py", "x = 1\n")

    # Candidate run fails (exit_code != 0) but output has no parseable test IDs
    unparseable_output = "Runner crashed with SIGSEGV (core dumped). Internal runner error."
    assert parse_failing_test_ids(unparseable_output, exit_code=1) is None

    cand_run = {
        "ran": True,
        "exit_code": 1,
        "output_tail": unparseable_output,
        "outcome": TestOutcome.FAILED.value,
        "failing_test_ids": None,  # unparseable
        "argv": ["pytest"],
        "cwd": str(ws.worktree_dir),
        "tool_path": "/bin/pytest",
    }

    base_run_data = {
        "base_status": BaseStatus.PASS,
        "failing_ids": None,
        "valid_cache": True,
        "disagreed": False,
    }
    def mock_run_tests(*args, **kwargs):
        ws.last_test_run = cand_run
        return TestOutcome.FAILED

    with patch.object(ws, "run_tests", side_effect=mock_run_tests), \
         patch.object(ws, "_run_detached_base_tests", return_value=base_run_data):
        res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Must fail closed as REGRESSION_DETECTED
    assert res.passed is False
    assert res.failed_check == "tests"
    assert res.test_classification == TestOutcome.REGRESSION_DETECTED.value


# j. Baseline run is cached: two attempts on the same base commit execute the test runner subprocess exactly once
def test_j_baseline_run_is_cached_per_base_commit(git_repo):
    # Add a dummy test file so pytest framework is detected
    test_file = git_repo / "test_dummy.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add dummy test"], cwd=git_repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=git_repo)
    base_commit = ws.get_current_head()

    mock_run_res = MagicMock()
    mock_run_res.returncode = 0
    mock_run_res.stdout = "=== 1 passed in 0.01s ==="
    mock_run_res.stderr = ""

    # Attempt 1: calls run_baseline_tests -> invokes test runner subprocess
    with patch.object(ws, "_run_test_subprocess", return_value=mock_run_res) as mock_sub:
        res1 = ws.run_baseline_tests(base_commit=base_commit, test_policy="auto")
        assert res1["outcome"] == TestOutcome.FULL_SUITE_PASSED.value
        assert mock_sub.call_count == 1

        # Attempt 2 on the exact same base commit: must hit cache and NOT invoke subprocess
        res2 = ws.run_baseline_tests(base_commit=base_commit, test_policy="auto")
        assert res2["outcome"] == TestOutcome.FULL_SUITE_PASSED.value
        assert mock_sub.call_count == 1  # Still 1!


# k. Ledger writes verify: query the ledger after an attempt and assert outcome, baseline_failures, new_failures, full_suite_clean are populated
def test_k_ledger_writes_verify_populated(tmp_path):
    db_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_path)

    run_id = "test-run-123"
    subgoal_id = "sg-test"

    state: State = {
        "run_id": run_id,
        "test_classification": TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value,
        "baseline_failures": 3,
        "new_failures": 0,
        "full_suite_clean": False,
    }

    _snapshot_attempt(
        recorder=ledger,
        state=state,
        subgoal_id=subgoal_id,
        tier="mechanical",
        outcome=TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value,
        diff="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n",
        tests_ran=True,
        test_outcome=TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value,
        baseline_failures=3,
        new_failures=0,
        full_suite_clean=False,
    )

    # Query sqlite ledger directly
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT outcome, baseline_failures, new_failures, full_suite_clean FROM attempts WHERE run_id = ? AND subgoal_id = ?",
        (run_id, subgoal_id),
    ).fetchone()
    conn.close()

    assert row is not None
    outcome, baseline_failures, new_failures, full_suite_clean = row
    assert outcome == TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
    assert baseline_failures == 3
    assert new_failures == 0
    assert full_suite_clean == 0  # 0 representing boolean False in sqlite INTEGER


# =====================================================================
# Real Temp Git Repo Tests (Real pytest, no mocking, no prefilled cache)
# =====================================================================

def _create_real_repo_with_legacy_tests(tmp_path: Path) -> tuple[Path, str]:
    repo_dir = tmp_path / "legacy_test_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    app_py = repo_dir / "app.py"
    app_py.write_text("def helper():\n    return 42\n", encoding="utf-8")

    test_py = repo_dir / "test_legacy.py"
    test_py.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
        "from app import helper\n\n"
        "def test_legacy_fail_one():\n"
        "    assert False, 'legacy failure 1'\n\n"
        "def test_legacy_fail_two():\n"
        "    assert False, 'legacy failure 2'\n\n"
        "def test_passing():\n"
        "    assert helper() == 42\n",
        encoding="utf-8",
    )

    gitignore = repo_dir / ".gitignore"
    gitignore.write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with 2 failing and 1 passing test"], cwd=repo_dir, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    base_commit = res.stdout.strip()
    return repo_dir, base_commit


# 1. repo with 2 failing legacy tests + 1 passing test. Create worktree,
#    run_baseline_tests, apply a good diff -> regression-free, base=2, new=0
def test_real_repo_baseline_and_good_diff_regression_free(tmp_path):
    repo_dir, base_commit = _create_real_repo_with_legacy_tests(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    # Create worktree
    ws.create_subgoal_worktree("sg-good", base_ref=base_commit)

    # Run baseline tests on the base ref in the worktree
    base_res = ws.run_baseline_tests(base_commit=base_commit)
    assert base_res["ran"] is True
    assert base_res["outcome"] == TestOutcome.FAILED.value
    assert len(base_res["failing_ids"]) == 2

    # Apply a good diff that doesn't break helper
    ws.stage_file_mutation("app.py", "def helper():\n    return 42\n\ndef new_feature():\n    return 100\n")

    # Mechanical checks
    subgoal = Subgoal(id="sg-good", description="Add new feature", scope=["app.py"], expects_tests=True)
    res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Assert regression-free, base=2, new=0
    assert res.passed is True
    assert res.failed_check is None
    assert res.test_classification == TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
    assert res.full_suite_clean is False
    assert res.baseline_failures == 2
    assert res.new_failures == 0


# 2. same, diff breaks the passing test -> REGRESSION_DETECTED, names it
def test_real_repo_diff_breaks_passing_test_regression_detected(tmp_path):
    repo_dir, base_commit = _create_real_repo_with_legacy_tests(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    # Create worktree
    ws.create_subgoal_worktree("sg-broken", base_ref=base_commit)

    # Run baseline tests
    base_res = ws.run_baseline_tests(base_commit=base_commit)
    assert len(base_res["failing_ids"]) == 2

    # Diff breaks the passing test
    ws.stage_file_mutation("app.py", "def helper():\n    return 999\n")

    # Mechanical checks
    subgoal = Subgoal(id="sg-broken", description="Break helper", scope=["app.py"], expects_tests=True)
    res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Assert REGRESSION_DETECTED, names it
    assert res.passed is False
    assert res.failed_check == "tests"
    assert res.test_classification == TestOutcome.REGRESSION_DETECTED.value
    assert res.full_suite_clean is False
    assert res.new_failures == 1
    assert any("test_passing" in fid for fid in (res.new_failing_ids or []))
    assert "test_passing" in res.detail


# 3. same, but skip run_baseline_tests (cache miss), diff breaks the
#    passing test -> must NOT pass (this test fails on current code)
def test_real_repo_skip_baseline_diff_breaks_fails_closed(tmp_path):
    repo_dir, base_commit = _create_real_repo_with_legacy_tests(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    # Create worktree
    ws.create_subgoal_worktree("sg-skip-baseline", base_ref=base_commit)

    # Skip run_baseline_tests (cache miss)
    assert base_commit not in ws._baseline_test_cache

    # Diff breaks the passing test
    ws.stage_file_mutation("app.py", "def helper():\n    return 999\n")

    # Mechanical checks without pre-filled baseline
    subgoal = Subgoal(id="sg-skip-baseline", description="Break helper without baseline", scope=["app.py"], expects_tests=True)
    res = ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Must NOT pass; fails as regression detected via on-demand same-command comparison
    assert res.passed is False
    assert res.failed_check == "tests"
    assert res.test_classification == TestOutcome.REGRESSION_DETECTED.value
    assert res.full_suite_clean is False


# 4. a fresh Workspace object (empty cache) at gate time -> same as 3
def test_real_repo_fresh_workspace_diff_breaks_fails_closed(tmp_path):
    repo_dir, base_commit = _create_real_repo_with_legacy_tests(tmp_path)
    ws = Workspace(repo_dir=repo_dir)

    # Create worktree and apply diff that breaks the passing test
    wt_path = ws.create_subgoal_worktree("sg-fresh-ws", base_ref=base_commit)
    ws.stage_file_mutation("app.py", "def helper():\n    return 999\n")

    # A fresh Workspace object (empty cache) at gate time
    fresh_ws = Workspace(repo_dir=repo_dir)
    fresh_ws.worktree_dir = wt_path
    assert len(fresh_ws._baseline_test_cache) == 0

    # Mechanical checks with fresh workspace
    subgoal = Subgoal(id="sg-fresh-ws", description="Break helper with fresh workspace object", scope=["app.py"], expects_tests=True)
    res = fresh_ws.run_mechanical_checks(subgoal, base_commit=base_commit)

    # Must NOT pass; same as 3
    assert res.passed is False
    assert res.failed_check == "tests"
    assert res.test_classification == TestOutcome.REGRESSION_DETECTED.value
    assert res.full_suite_clean is False
