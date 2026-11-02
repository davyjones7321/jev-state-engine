"""Mechanical verification for edited test recipes and scripts."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.mech_evidence import EvidenceLevel, MechReason, MechStatus
from jev.engine import node_gate
from jev.models import Subgoal
from jev.workspace import ResolvedTestCommand, Workspace


@pytest.fixture
def recipe_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / "Makefile").write_text("test:\n\tpytest\n\nbuild:\n\tpython -c pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    ws = Workspace(repo_dir=repo)
    ws.base_commit = ws._get_head_commit()
    return ws


def _stage(ws: Workspace, *paths: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=ws.repo_dir, check=True)
    ws.worker_staged_paths.extend(paths)


def _proc(exit_code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["make", "test"], exit_code, stdout, stderr)


def test_weakened_makefile_recipe_runs_original_and_strikes(recipe_repo):
    ws = recipe_repo
    repo = ws.repo_dir
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "test:\n\tpytest -k dummy\n\nbuild:\n\tpython -c pass\n", encoding="utf-8"
    )
    _stage(ws, "src/calc.py", "Makefile")
    subgoal = Subgoal(id="sg-recipe", description="edit test recipe", scope=["src/calc.py"], expects_tests=True)

    def run(cmd, cwd=None, **kwargs):
        if cwd and ".jev-worktrees" in str(cwd):
            return _proc(1, "FAILED tests/test_calc.py::test_add - assert 999 == 3")
        return _proc(0, "1 passed in 0.01s")

    resolved = ResolvedTestCommand("trusted_config", ["make", "test"], "trusted_repo_config")
    with patch("jev.workspace.resolve_test_command", return_value=resolved), patch("shutil.which", return_value="make"), patch.object(ws, "_run_test_subprocess", side_effect=run):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert ev is not None
    assert ev.mech_status == MechStatus.FAIL
    assert ev.verifier_modified is True
    assert ev.evidence_level == EvidenceLevel.COUNTED
    assert ws.last_test_run["failing_test_ids"] == ["tests/test_calc.py::test_add"]

    ws._candidate_run_counts.clear()
    state = {
        "status": "in_progress",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "trajectory": [],
    }
    gatekeeper = MagicMock()
    with patch("shutil.which", return_value="make"), patch.object(ws, "_run_test_subprocess", side_effect=run):
        result = node_gate(state, workspace=ws, gatekeeper=gatekeeper)
    assert result["gate_status"] == "mechanical_failure"
    assert result["mechanical_strike_count"] == 1
    gatekeeper.validate_subgoal.assert_not_called()


def test_declared_makefile_recipe_change_is_unmeasured_without_scratch(recipe_repo):
    ws = recipe_repo
    repo = ws.repo_dir
    (repo / "Makefile").write_text("test:\n\tpytest -k dummy\n\nbuild:\n\tpython -c pass\n", encoding="utf-8")
    _stage(ws, "Makefile")
    subgoal = Subgoal(id="sg-declared", description="edit test recipe", scope=["Makefile"], expects_tests=True)
    scratch = MagicMock()
    resolved = ResolvedTestCommand("trusted_config", ["make", "test"], "trusted_repo_config")
    with patch("jev.workspace.resolve_test_command", return_value=resolved), patch("shutil.which", return_value="make"), patch.object(
        ws, "_run_test_subprocess", return_value=_proc(0, "1 passed")
    ), patch.object(ws, "_run_original_tests_in_scratch", scratch):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always", ticket_scope=["Makefile"])

    assert ev is not None
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.verifier_modified is True
    scratch.assert_not_called()


def test_non_test_makefile_target_and_untouched_recipe_do_not_run_scratch(recipe_repo):
    ws = recipe_repo
    repo = ws.repo_dir
    base = repo / "Makefile"
    base.write_text("test:\n\tpytest\n\nbuild:\n\tpython -c changed\n", encoding="utf-8")
    _stage(ws, "Makefile")
    subgoal = Subgoal(id="sg-build", description="edit build recipe", scope=["Makefile"], expects_tests=True)
    scratch = MagicMock()
    resolved = ResolvedTestCommand("trusted_config", ["make", "test"], "trusted_repo_config")
    with patch("jev.workspace.resolve_test_command", return_value=resolved), patch("shutil.which", return_value="make"), patch.object(
        ws, "_run_test_subprocess", return_value=_proc(0, "1 passed")
    ), patch.object(ws, "_run_original_tests_in_scratch", scratch):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    scratch.assert_not_called()


def test_package_json_test_script_and_pytest_config_are_originally_rechecked(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts":{"test":"pytest"}}', encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    ws = Workspace(repo_dir=repo)
    base = ws._get_head_commit()
    (repo / "package.json").write_text('{"scripts":{"test":"pytest -k dummy"}}', encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q -x\n", encoding="utf-8")

    assert ws._edited_test_recipe_paths(
        ["package.json"], base, ResolvedTestCommand("npm", ["npm", "test"], "package.json")
    ) == ["package.json"]
    assert ws._edited_test_recipe_paths(
        ["pytest.ini"], base, ResolvedTestCommand("pytest", ["python", "-m", "pytest", "-q"], "pytest.ini")
    ) == ["pytest.ini"]
