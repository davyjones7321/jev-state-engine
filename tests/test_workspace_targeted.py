"""Focused, offline checks for workspace test evidence."""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from jev.models import TestOutcome
from jev.workspace import Workspace, compare_test_runs


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return Workspace(repo_dir=repo)


@pytest.mark.parametrize(
    "lockfile,expected",
    [
        (None, ["npm", "test", "--", "tests/a.test.js"]),
        ("yarn.lock", ["yarn", "test", "tests/a.test.js"]),
        ("pnpm-lock.yaml", ["pnpm", "test", "tests/a.test.js"]),
        ("bun.lock", ["bun", "run", "test", "tests/a.test.js"]),
        ("bun.lockb", ["bun", "run", "test", "tests/a.test.js"]),
    ],
)
def test_package_targets_use_existing_script_with_safe_argv(workspace, lockfile, expected):
    (workspace.worktree_dir / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    if lockfile:
        (workspace.worktree_dir / lockfile).touch()
    (workspace.worktree_dir / "tests").mkdir()
    (workspace.worktree_dir / "tests/a.test.js").touch()
    process = subprocess.CompletedProcess([], 0, "one passed", "")

    with patch("shutil.which", return_value="/bin/runner"), patch.object(
        workspace, "_run_test_subprocess", return_value=process
    ) as run:
        assert workspace.run_tests(targets=["tests/a.test.js"]) == TestOutcome.PASSED

    run.assert_called_once_with(["/bin/runner", *expected[1:]], cwd=workspace.worktree_dir, timeout=workspace.test_timeout)
    assert workspace.last_test_run["command"] == expected
    assert workspace.last_test_run["executed_command"] == ["/bin/runner", *expected[1:]]
    assert workspace.last_test_run["exit_code"] == 0
    assert workspace.last_test_run["stdout"] == "one passed"
    assert workspace.last_test_run["stderr"] == ""
    assert workspace.last_test_run["outcome"] == "PASSED"


def test_pytest_targeted_nodeid_records_failure_and_all_output(workspace):
    (workspace.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (workspace.worktree_dir / "test_app.py").touch()
    process = subprocess.CompletedProcess([], 1, "failed assertion", "traceback")
    with patch.object(workspace, "_run_test_subprocess", return_value=process) as run:
        assert workspace.run_tests(targets=["test_app.py::test_failure"]) == TestOutcome.FAILED

    command = [sys.executable, "-m", "pytest", "-q", "test_app.py::test_failure"]
    run.assert_called_once_with(command, cwd=workspace.worktree_dir, timeout=workspace.test_timeout)
    assert workspace.last_test_run["command"] == command
    assert workspace.last_test_run["stdout"] == "failed assertion"
    assert workspace.last_test_run["stderr"] == "traceback"
    assert workspace.last_test_run["exit_code"] == 1
    assert workspace.last_test_run["outcome"] == "FAILED"


def test_pytest_multiple_relative_paths_are_passed_as_distinct_args(workspace):
    (workspace.worktree_dir / "pytest.ini").touch()
    (workspace.worktree_dir / "test_one.py").touch()
    (workspace.worktree_dir / "test_two.py").touch()
    process = subprocess.CompletedProcess([], 0, "2 passed", "")
    with patch.object(workspace, "_run_test_subprocess", return_value=process) as run:
        assert workspace.run_tests(targets=["test_one.py", "test_two.py"]) == TestOutcome.PASSED
    run.assert_called_once_with(
        [sys.executable, "-m", "pytest", "-q", "test_one.py", "test_two.py"],
        cwd=workspace.worktree_dir,
        timeout=workspace.test_timeout,
    )


def test_empty_targets_preserve_full_suite_command(workspace):
    (workspace.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    process = subprocess.CompletedProcess([], 5, "no tests collected", "")
    with patch.object(workspace, "_run_test_subprocess", return_value=process) as run:
        assert workspace.run_tests(targets=[]) == TestOutcome.NO_TESTS_COLLECTED
    run.assert_called_once_with(
        [sys.executable, "-m", "pytest", "-q"], cwd=workspace.worktree_dir, timeout=workspace.test_timeout
    )
    assert workspace.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]
    assert workspace.last_test_run["exit_code"] == 5


@pytest.mark.parametrize("target", ["../outside.py", r"..\outside.py", "/tmp/elsewhere", "C:/elsewhere", "--help"])
def test_invalid_targets_never_start_subprocess(workspace, target):
    (workspace.worktree_dir / "pytest.ini").touch()
    with patch.object(workspace, "_run_test_subprocess") as run:
        with pytest.raises((ValueError, PermissionError)):
            workspace.run_tests(targets=[target])
        run.assert_not_called()


def test_symlink_escape_never_starts_subprocess(workspace, tmp_path):
    (workspace.worktree_dir / "pytest.ini").touch()
    link = workspace.worktree_dir / "outside"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with patch.object(workspace, "_run_test_subprocess") as run:
        with pytest.raises(PermissionError):
            workspace.run_tests(targets=["outside/test.py"])
        run.assert_not_called()


def test_package_targets_reject_shell_metacharacters_even_in_existing_filename(workspace):
    (workspace.worktree_dir / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    (workspace.worktree_dir / "test;run.js").touch()
    with patch.object(workspace, "_run_test_subprocess") as run:
        with pytest.raises(ValueError, match="unsupported characters"):
            workspace.run_tests(targets=["test;run.js"])
        run.assert_not_called()


def test_many_targets_are_rejected_before_execution(workspace):
    (workspace.worktree_dir / "pytest.ini").touch()
    with patch.object(workspace, "_run_test_subprocess") as run:
        with pytest.raises(ValueError, match="At most 32"):
            workspace.run_tests(targets=["test_app.py"] * 33)
        run.assert_not_called()


def test_empty_command_string_is_not_a_full_suite_request(workspace):
    (workspace.worktree_dir / "pytest.ini").touch()
    with patch.object(workspace, "_run_test_subprocess") as run:
        with pytest.raises(ValueError, match="sequence"):
            workspace.run_tests(targets="")
        run.assert_not_called()


def test_unsupported_ecosystem_does_not_run_full_suite_on_target_request(workspace):
    (workspace.worktree_dir / "go.mod").write_text("module test\n", encoding="utf-8")
    (workspace.worktree_dir / "test_app.go").touch()
    with patch.object(workspace, "_run_test_subprocess") as run:
        assert workspace.run_tests(targets=["test_app.go"]) == TestOutcome.NO_TESTS_COLLECTED
        run.assert_not_called()
    assert workspace.last_test_run["outcome"] == "NO_TESTS_COLLECTED"


def test_bounded_telemetry_and_baseline_comparison(workspace):
    (workspace.worktree_dir / "pytest.ini").touch()
    (workspace.worktree_dir / "test_app.py").touch()
    before = {"outcome": "PASSED", "exit_code": 0}
    process = subprocess.CompletedProcess([], 1, "x" * 3000, "y" * 3000)
    with patch.object(workspace, "_run_test_subprocess", return_value=process):
        assert workspace.run_tests(targets=["test_app.py"]) == TestOutcome.FAILED
    after = workspace.last_test_run
    assert after["stdout"] == "x" * 1000
    assert after["stderr"] == "y" * 1000
    assert after["stdout_truncated"] is True
    assert after["stderr_truncated"] is True
    assert len(after["output_tail"]) <= 2010
    assert compare_test_runs(before, after) == {
        "before": "PASSED", "after": "FAILED", "regressed": True, "improved": False
    }
    assert compare_test_runs(None, after)["regressed"] is False
