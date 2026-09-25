import subprocess
from unittest.mock import MagicMock

import pytest

from jev.models import MechanicalCheckResult, Subgoal, TestOutcome
from jev.workspace import Workspace


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


# 1. test_rollback_subgoal_reverts_dirty_file
def test_rollback_subgoal_reverts_dirty_file(git_repo):
    """Stage a mutation, call rollback_subgoal(), assert the file matches the pre-mutation state via native git commands."""
    ws = Workspace(repo_dir=git_repo)
    dirty_file = git_repo / "test_file.py"
    ws.stage_file_mutation("test_file.py", "x = 42\n")

    assert dirty_file.exists()
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True)
    assert "test_file.py" in status_before.stdout

    ws.rollback_subgoal()

    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True)
    assert "test_file.py" not in status_after.stdout
    assert not dirty_file.exists()


# 2. test_check_build_fails_on_syntax_error
def test_check_build_fails_on_syntax_error(git_repo):
    """Stage a file with a deliberate syntax error, assert check_build() returns failure without invoking run_tests()."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("bad_syntax.py", "def broken_func(:\n    pass\n")

    ws.run_tests = MagicMock()

    result = ws.check_build()

    assert not result
    assert result.passed is False
    assert ws.run_tests.call_count == 0


# 3. test_check_build_passes_on_valid_file
def test_check_build_passes_on_valid_file(git_repo):
    """Sanity check, valid file passes."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("good_syntax.py", "def valid_func():\n    return 42\n")

    result = ws.check_build()

    assert result
    assert result.passed is True


# 4. test_run_tests_returns_passed
def test_run_tests_returns_passed(git_repo):
    """Stage a passing test file, assert TestOutcome.PASSED."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("test_sample.py", "def test_ok():\n    assert True\n")

    outcome = ws.run_tests()

    assert outcome == TestOutcome.PASSED


# 5. test_run_tests_returns_failed
def test_run_tests_returns_failed(git_repo):
    """Stage a failing test, assert TestOutcome.FAILED."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("test_sample.py", "def test_not_ok():\n    assert False\n")

    outcome = ws.run_tests()

    assert outcome == TestOutcome.FAILED


# 6. test_run_tests_on_empty_directory_returns_no_tests_collected
def test_run_tests_on_empty_directory_returns_no_tests_collected(git_repo):
    """Point run_tests() at a directory with zero test files, assert TestOutcome.NO_TESTS_COLLECTED specifically — must not return PASSED."""
    ws = Workspace(repo_dir=git_repo)

    outcome = ws.run_tests()

    assert outcome == TestOutcome.NO_TESTS_COLLECTED
    assert outcome != TestOutcome.PASSED


# 7. test_check_scope_flags_out_of_scope_file
def test_check_scope_flags_out_of_scope_file(git_repo):
    """Stage a diff touching a file not listed in subgoal.scope, assert check_scope() flags it."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("unexpected.py", "secret = 1\n")
    diff = ws.get_staged_diff()

    subgoal = Subgoal(scope=["expected.py"], expects_tests=True)
    result = ws.check_scope(diff, subgoal.scope)

    assert not result
    assert result.passed is False


# 8. test_check_scope_passes_in_scope_diff
def test_check_scope_passes_in_scope_diff(git_repo):
    """Diff only touches declared files, assert pass."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("expected.py", "expected = 1\n")
    diff = ws.get_staged_diff()

    subgoal = Subgoal(scope=["expected.py"], expects_tests=True)
    result = ws.check_scope(diff, subgoal.scope)

    assert result
    assert result.passed is True


# 9. test_run_mechanical_checks_short_circuits_on_build_failure
def test_run_mechanical_checks_short_circuits_on_build_failure(git_repo):
    """Mock check_build to fail, assert run_tests() and check_scope() are never called (use call-count assertions, not just the final result)."""
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(scope=["sample.py"], expects_tests=True)

    build_mock = MagicMock()
    build_mock.passed = False
    build_mock.detail = "SyntaxError on line 1"
    build_mock.__bool__ = lambda self: False

    ws.check_build = MagicMock(return_value=build_mock)
    ws.run_tests = MagicMock()
    ws.check_scope = MagicMock()

    result = ws.run_mechanical_checks(subgoal)

    assert isinstance(result, MechanicalCheckResult)
    assert result.passed is False
    assert result.failed_check == "build"
    assert "SyntaxError" in result.detail
    assert ws.run_tests.call_count == 0
    assert ws.check_scope.call_count == 0


# 10. test_run_mechanical_checks_short_circuits_on_test_failure
def test_run_mechanical_checks_short_circuits_on_test_failure(git_repo):
    """Build passes, tests fail, assert check_scope() is never called."""
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(scope=["sample.py"], expects_tests=True)

    build_mock = MagicMock()
    build_mock.passed = True
    build_mock.detail = ""
    build_mock.__bool__ = lambda self: True

    ws.check_build = MagicMock(return_value=build_mock)
    ws.run_tests = MagicMock(return_value=TestOutcome.FAILED)
    ws.check_scope = MagicMock()

    result = ws.run_mechanical_checks(subgoal)

    assert isinstance(result, MechanicalCheckResult)
    assert result.passed is False
    assert result.failed_check == "tests"
    assert ws.check_scope.call_count == 0


# 11. test_run_mechanical_checks_rejects_no_tests_when_expected
def test_run_mechanical_checks_rejects_no_tests_when_expected(git_repo):
    """subgoal.expects_tests=True, run_tests() returns NO_TESTS_COLLECTED, assert MechanicalCheckResult.passed=False, failed_check="no_tests_collected"."""
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(scope=["sample.py"], expects_tests=True)

    build_mock = MagicMock()
    build_mock.passed = True
    build_mock.detail = ""
    build_mock.__bool__ = lambda self: True

    ws.check_build = MagicMock(return_value=build_mock)
    ws.run_tests = MagicMock(return_value=TestOutcome.NO_TESTS_COLLECTED)
    ws.check_scope = MagicMock()

    result = ws.run_mechanical_checks(subgoal)

    assert isinstance(result, MechanicalCheckResult)
    assert result.passed is False
    assert result.failed_check == "no_tests_collected"
    assert ws.check_scope.call_count == 0


# 12. test_run_mechanical_checks_allows_no_tests_when_not_expected
def test_run_mechanical_checks_allows_no_tests_when_not_expected(git_repo):
    """subgoal.expects_tests=False, run_tests() returns NO_TESTS_COLLECTED, assert MechanicalCheckResult.passed=True and detail contains an explicit untested-pass flag."""
    ws = Workspace(repo_dir=git_repo)
    subgoal = Subgoal(scope=["sample.py"], expects_tests=False)

    build_mock = MagicMock()
    build_mock.passed = True
    build_mock.detail = ""
    build_mock.__bool__ = lambda self: True

    scope_mock = MagicMock()
    scope_mock.passed = True
    scope_mock.detail = ""
    scope_mock.__bool__ = lambda self: True

    ws.check_build = MagicMock(return_value=build_mock)
    ws.run_tests = MagicMock(return_value=TestOutcome.NO_TESTS_COLLECTED)
    ws.check_scope = MagicMock(return_value=scope_mock)

    result = ws.run_mechanical_checks(subgoal)

    assert isinstance(result, MechanicalCheckResult)
    assert result.passed is True
    assert result.failed_check is None
    assert "untested" in result.detail.lower()


# 13. test_get_cumulative_diff_captures_committed_subgoals
def test_get_cumulative_diff_captures_committed_subgoals(git_repo):
    """When a subgoal is committed, get_staged_diff is empty, but get_cumulative_diff returns the committed diff."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("doc.py", "# New docstring\ndef hello(): pass\n")
    ws.commit_subgoal("Added hello function")

    # Staged diff is empty because the commit already occurred
    assert ws.get_staged_diff().strip() == ""

    # Cumulative diff captures the diff from base_commit to HEAD
    cumulative = ws.get_cumulative_diff()
    assert "doc.py" in cumulative
    assert "+# New docstring" in cumulative
