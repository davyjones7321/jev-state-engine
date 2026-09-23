import subprocess
from unittest.mock import MagicMock
import pytest

from jev.engine import node_gate
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    ValidationVerdict,
)
from jev.workspace import Workspace


@pytest.fixture
def git_repo(tmp_path):
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


def test_check_scope_with_spaces_in_filename():
    ws = Workspace()
    diff = (
        "diff --git a/my path/file.py b/my path/file.py\n"
        "index 1234567..89abcdef 100644\n"
        "--- a/my path/file.py\n"
        "+++ b/my path/file.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    # When in scope
    res = ws.check_scope(diff, ["my path/file.py"])
    assert res.passed is True
    assert res.out_of_scope == []

    # When out of scope
    res_out = ws.check_scope(diff, ["other.py"])
    assert res_out.passed is False
    assert "my path/file.py" in res_out.out_of_scope


def test_check_scope_detects_file_rename_source():
    ws = Workspace()
    diff = (
        "diff --git a/old_secret.py b/new_feature.py\n"
        "similarity index 100%\n"
        "rename from old_secret.py\n"
        "rename to new_feature.py\n"
    )
    # If LLM only declared new_feature.py, old_secret.py must be flagged
    res = ws.check_scope(diff, ["new_feature.py"])
    assert res.passed is False
    assert "old_secret.py" in res.out_of_scope

    # If LLM declared both, it should pass
    res_both = ws.check_scope(diff, ["old_secret.py", "new_feature.py"])
    assert res_both.passed is True


def test_check_scope_deleted_file():
    ws = Workspace()
    diff = (
        "diff --git a/deleted.py b/deleted.py\n"
        "deleted file mode 100644\n"
        "index 1234567..0000000 100644\n"
        "--- a/deleted.py\n"
        "+++ /dev/null\n"
    )
    res_pass = ws.check_scope(diff, ["deleted.py"])
    assert res_pass.passed is True

    res_fail = ws.check_scope(diff, [])
    assert res_fail.passed is False
    assert "deleted.py" in res_fail.out_of_scope


def test_check_build_skips_venv_directory(git_repo):
    ws = Workspace(repo_dir=git_repo)
    venv_dir = git_repo / ".venv" / "Lib" / "site-packages"
    venv_dir.mkdir(parents=True)
    broken_file = venv_dir / "broken_package.py"
    broken_file.write_text("def broken(:\n    pass\n", encoding="utf-8")

    # The broken file inside .venv should be ignored during check_build
    result = ws.check_build()
    assert result.passed is True


def test_check_build_out_of_scope_syntax_error_does_not_block_subgoal(git_repo):
    """Stage a syntax error in a file OUTSIDE the current subgoal's declared scope,
    and assert run_mechanical_checks() still returns passed=True for build."""
    ws = Workspace(repo_dir=git_repo)

    # Pre-existing file with a deliberate syntax error committed in the repo
    outside_file = git_repo / "outside_scope.py"
    outside_file.write_text("def broken_syntax(:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "outside_scope.py"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Pre-existing broken file"], cwd=git_repo, check=True, capture_output=True)

    # Current subgoal touches only an in-scope valid file
    ws.stage_file_mutation("inside_scope.py", "x = 42\n")
    subgoal = Subgoal(
        description="Feature in scope",
        scope=["inside_scope.py"],
        expects_tests=False,
    )

    # check_build explicitly scoped to subgoal.scope passes
    build_res = ws.check_build(subgoal.scope)
    assert build_res.passed is True

    # run_mechanical_checks passes build check without being blocked by outside_scope.py
    mech_res = ws.run_mechanical_checks(subgoal)
    assert mech_res.passed is True
    assert mech_res.failed_check is None


def test_check_build_defaults_to_diff_touched_files_when_scope_is_empty(git_repo):
    """When scope is empty, check_build() scopes to diff's touched files instead of whole worktree."""
    ws = Workspace(repo_dir=git_repo)

    # Pre-existing syntax error in repo
    outside_file = git_repo / "pre_existing_err.py"
    outside_file.write_text("def broken(:\n", encoding="utf-8")
    subprocess.run(["git", "add", "pre_existing_err.py"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Broken commit"], cwd=git_repo, check=True, capture_output=True)

    # Subgoal with empty scope touches valid file
    ws.stage_file_mutation("staged_file.py", "valid = 1\n")
    subgoal = Subgoal(
        description="Empty scope subgoal",
        scope=[],
        expects_tests=False,
    )

    # check_build with empty list defaults to diff, ignoring pre_existing_err.py
    res_empty_scope = ws.check_build([])
    assert res_empty_scope.passed is True

    # run_mechanical_checks with empty scope passes build, but flags scope creep if diff is non-empty
    mech_res = ws.run_mechanical_checks(subgoal)
    assert mech_res.failed_check != "build"
    assert mech_res.failed_check == "scope"

    # With declared scope matching the diff, mechanical checks pass completely
    subgoal_valid = Subgoal(
        description="Valid scope subgoal",
        scope=["staged_file.py"],
        expects_tests=False,
    )
    mech_res_valid = ws.run_mechanical_checks(subgoal_valid)
    assert mech_res_valid.passed is True
    assert mech_res_valid.failed_check is None


def test_check_build_ignores_syntax_error_in_staged_out_of_scope_file(git_repo):
    """If a syntax error is staged in an out-of-scope file, check_build(subgoal.scope) passes and mechanical check fails on scope, not build."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("unrelated_staged.py", "def syntax_err(:\n")
    ws.stage_file_mutation("scoped_file.py", "y = 123\n")
    subgoal = Subgoal(scope=["scoped_file.py"], expects_tests=False)

    build_res = ws.check_build(subgoal.scope)
    assert build_res.passed is True

    mech_res = ws.run_mechanical_checks(subgoal)
    assert mech_res.failed_check != "build"
    assert mech_res.failed_check == "scope"


def test_stage_file_mutation_with_absolute_path(git_repo):
    ws = Workspace(repo_dir=git_repo)
    abs_path = git_repo / "subdir" / "abs_file.py"
    ws.stage_file_mutation(abs_path, "y = 99\n")

    assert abs_path.exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True)
    assert "subdir/abs_file.py" in status.stdout.replace("\\", "/")


def test_node_gate_handles_dict_current_subgoal():
    fake_ws = MagicMock()
    fake_ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    fake_ws.get_staged_diff.return_value = ""

    fake_gk = MagicMock()
    fake_gk.validate_subgoal.return_value = ValidationVerdict(valid=True)

    state: State = {
        "ticket": "Test ticket",
        "current_subgoal": {
            "description": "Dict subgoal",
            "scope": ["foo.py"],
            "expects_tests": True,
        },  # type: ignore
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    result = node_gate(state, workspace=fake_ws, gatekeeper=fake_gk)
    assert result["gate_status"] == "passed"
    fake_ws.commit_subgoal.assert_called_once()


def test_node_gate_passes_mechanical_detail_to_gatekeeper():
    untested_detail = "Untested pass: expects_tests=False with NO_TESTS_COLLECTED."
    fake_ws = MagicMock()
    fake_ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=True, detail=untested_detail
    )
    fake_ws.get_staged_diff.return_value = "diff --git a/foo.py b/foo.py"

    captured_kwargs = {}

    class MockGatekeeper:
        def validate_subgoal(self, subgoal, diff, mechanical_detail=""):
            captured_kwargs["mechanical_detail"] = mechanical_detail
            return ValidationVerdict(valid=True)

        def escalate_deadlock(self, trajectory=None, triggering_tier=None):
            pass

    gk = MockGatekeeper()
    state: State = {
        "current_subgoal": Subgoal(scope=["foo.py"], expects_tests=False),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    node_gate(state, workspace=fake_ws, gatekeeper=gk)
    assert captured_kwargs["mechanical_detail"] == untested_detail


def test_node_gate_escalates_when_already_at_or_above_threshold():
    fake_ws = MagicMock()
    fake_ws.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="build", detail="Syntax error"
    )
    fake_gk = MagicMock()

    state: State = {
        "mechanical_strike_count": 3,
        "semantic_strike_count": 0,
        "trajectory": [{"step": 1}],
    }

    result = node_gate(state, workspace=fake_ws, gatekeeper=fake_gk)
    assert result["mechanical_strike_count"] == 4
    fake_gk.escalate_deadlock.assert_called_once_with(
        trajectory=[{"step": 1}], triggering_tier="mechanical"
    )


def test_pydantic_default_truthiness():
    res = MechanicalCheckResult(passed=False)
    assert res is not None
    assert res.passed is False


def test_check_build_with_single_str_and_path(git_repo):
    """Verify check_build properly checks syntax when passed a single str or Path (no character splitting or TypeError)."""
    ws = Workspace(repo_dir=git_repo)
    ws.stage_file_mutation("broken.py", "def syntax_error(:\n    pass\n")

    res_str = ws.check_build("broken.py")
    assert res_str.passed is False
    assert "SyntaxError" in res_str.detail

    res_path = ws.check_build(git_repo / "broken.py")
    assert res_path.passed is False
    assert "SyntaxError" in res_path.detail


def test_check_build_with_directory_in_scope(git_repo):
    """Verify check_build checks .py files when a directory path is in scope."""
    ws = Workspace(repo_dir=git_repo)
    pkg_dir = git_repo / "mypackage"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "broken_module.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    res = ws.check_build(["mypackage"])
    assert res.passed is False
    assert "SyntaxError" in res.detail


def test_check_scope_with_single_str_and_path():
    """Verify check_scope properly handles a single str or Path scope without character splitting."""
    ws = Workspace()
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    res_str = ws.check_scope(diff, scope="foo.py")
    assert res_str.passed is True
    assert res_str.out_of_scope == []

    from pathlib import Path
    res_path = ws.check_scope(diff, scope=Path("foo.py"))
    assert res_path.passed is True
    assert res_path.out_of_scope == []


def test_run_mechanical_checks_passes_subgoal_scope_to_check_build():
    """Explicitly verify that run_mechanical_checks passes subgoal.scope into check_build."""
    ws = Workspace()
    build_mock = MagicMock()
    build_mock.passed = False
    build_mock.detail = "Syntax error"
    build_mock.__bool__ = lambda self: False

    ws.check_build = MagicMock(return_value=build_mock)
    subgoal = Subgoal(scope=["a.py", "b.py"])
    res = ws.run_mechanical_checks(subgoal)

    assert res.failed_check == "build"
    ws.check_build.assert_called_once_with(["a.py", "b.py"])


def test_node_gate_raises_type_error_without_fallback_on_legacy_signature():
    """Verify that node_gate() makes a single direct call with mechanical_detail=
    and raises TypeError when a legacy Gatekeeper with detail= is passed,
    confirming no fallback branches exist."""
    fake_ws = MagicMock()
    fake_ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    fake_ws.get_staged_diff.return_value = ""

    class LegacyGatekeeper:
        def validate_subgoal(self, subgoal, diff, detail=None):
            return ValidationVerdict(valid=True)

    state: State = {
        "current_subgoal": Subgoal(scope=["foo.py"]),
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
    }

    with pytest.raises(TypeError, match="unexpected keyword argument 'mechanical_detail'"):
        node_gate(state, workspace=fake_ws, gatekeeper=LegacyGatekeeper())

