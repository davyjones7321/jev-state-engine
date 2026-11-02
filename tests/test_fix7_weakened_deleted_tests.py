"""tests/test_fix7_weakened_deleted_tests.py

Tests for Fix 7: Mechanical response to weakened or deleted pre-existing tests.
Requirements:
(a) weaken an assertion so the edited test passes but the original fails -> strikable FAIL with failing test ids;
(b) delete a test file -> original restored in scratch copy, FAIL if behavior broke;
(c) ticket scope explicitly includes the changed test file -> UNMEASURED with verifier_modified, no auto-FAIL;
(d) untouched tests -> behavior unchanged;
(e) edited Makefile test recipe -> handled the same as a changed test surface.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import node_gate
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechReason,
    MechStatus,
)
from jev.models import Subgoal, TestOutcome
from jev.repo_config import is_build_or_test_config
from jev.workspace import Workspace, is_shared_test_surface


@pytest.fixture
def repo_with_tests(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # Initial commit
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
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


# ==============================================================================
# (a) Weaken assertion so edited test passes but original fails -> strikable FAIL
# ==============================================================================

def test_weaken_assertion_edited_passes_original_fails_strikable_fail(repo_with_tests):
    ws = repo_with_tests
    repo = ws.repo_dir

    # Break candidate code
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    # Weaken test assertion so it passes on candidate
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 999\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/test_calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(
        id="sg-weaken",
        description="weaken test",
        scope=["src/calc.py", "tests/test_calc.py"],
        expects_tests=True,
    )

    # Candidate run passes
    cand_pass_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )

    # Scratch run with original tests fails on candidate code (add(1, 2) == 999 != 3)
    scratch_fail_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_add - assert 999 == 3",
        stderr="",
    )

    def mock_subprocess(cmd, cwd=None, **kwargs):
        # If running in scratch copy (isolated verification)
        if cwd and ".jev-worktrees" in str(cwd):
            return scratch_fail_proc
        return cand_pass_proc

    with patch.object(ws, "_run_test_subprocess", side_effect=mock_subprocess), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert ev is not None
        assert ev.mech_status == MechStatus.FAIL
        assert ev.reason is None
        assert ev.comparison == Comparison.CANDIDATE_ONLY
        assert ev.verifier_modified is True
        assert ws.last_test_run.get("failing_test_ids") == ["tests/test_calc.py::test_add"]
        assert "tests/test_calc.py::test_add" in (ev.attribution_note or "")

        # Verify candidate worktree files were not modified by scratch check
        cand_test_text = (repo / "tests" / "test_calc.py").read_text(encoding="utf-8")
        assert "assert add(1, 2) == 999" in cand_test_text

        # Verify in node_gate: counts as a mechanical failure with 1 strike
        ws._candidate_run_counts.clear()
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "trajectory": [],
        }
        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)
        assert result_state.get("gate_status") == "mechanical_failure"
        assert result_state.get("mechanical_strike_count") == 1
        mock_gatekeeper.validate_subgoal.assert_not_called()


# ==============================================================================
# (b) Delete a test file -> original restored, FAIL if behavior broke
# ==============================================================================

def test_delete_test_file_original_restored_fail_if_behavior_broke(repo_with_tests):
    ws = repo_with_tests
    repo = ws.repo_dir

    # Break candidate code
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    # Delete pre-existing test file
    subprocess.run(["git", "rm", "tests/test_calc.py"], cwd=repo, check=True)

    # Candidate has another passing test
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_dummy.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_dummy.py"], cwd=repo, check=True)

    subgoal = Subgoal(
        id="sg-del-test",
        description="delete test",
        scope=["src/calc.py", "tests/test_calc.py"],
        expects_tests=True,
    )

    cand_pass_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )

    scratch_fail_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_add - assert 0 == 3",
        stderr="",
    )

    def mock_subprocess(cmd, cwd=None, **kwargs):
        if cwd and ".jev-worktrees" in str(cwd):
            return scratch_fail_proc
        return cand_pass_proc

    with patch.object(ws, "_run_test_subprocess", side_effect=mock_subprocess), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert ev is not None
        assert ev.mech_status == MechStatus.FAIL
        assert ev.reason is None
        assert ev.comparison == Comparison.CANDIDATE_ONLY
        assert ev.verifier_modified is True
        assert ws.last_test_run.get("failing_test_ids") == ["tests/test_calc.py::test_add"]
        assert "tests/test_calc.py::test_add" in (ev.attribution_note or "")

        # Ensure original file was NOT recreated in candidate worktree
        assert not (repo / "tests" / "test_calc.py").exists()

        # In node_gate: costs 1 strike
        ws._candidate_run_counts.clear()
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "trajectory": [],
        }
        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)
        assert result_state.get("gate_status") == "mechanical_failure"
        assert result_state.get("mechanical_strike_count") == 1


# ==============================================================================
# (c) Ticket scope explicitly includes changed test file -> UNMEASURED, no auto-FAIL
# ==============================================================================

def test_ticket_scope_explicitly_includes_changed_test_file_unmeasured(repo_with_tests):
    ws = repo_with_tests
    repo = ws.repo_dir

    # Modify pre-existing test
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(0, 0) == 0\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "tests/test_calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(
        id="sg-ticket-scope",
        description="refactor calc tests",
        scope=["tests/test_calc.py"],
        expects_tests=True,
    )

    cand_pass_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=cand_pass_proc), \
         patch("shutil.which", return_value=sys.executable):
        # 1. With ticket_scope explicitly listing the test file
        ev = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            ticket_scope=["tests/test_calc.py"],
        )

        assert ev is not None
        assert ev.mech_status == MechStatus.UNMEASURED
        assert ev.reason == MechReason.TEST_SURFACE_CHANGED
        assert ev.verifier_modified is True
        assert ev.mechanically_unverified is True
        assert ev.unverified_kind == "verifier_modified"

        # In node_gate: 0 strikes (not auto-FAIL)
        ws._candidate_run_counts.clear()
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "unmeasured_retry_count": 0,
            "trajectory": [],
            "ticket": {"scope": ["tests/test_calc.py"]},
        }
        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)
        assert result_state.get("gate_status") == "unmeasured"
        assert result_state.get("mechanical_strike_count") == 0

        # 2. With ticket string containing the test path
        (repo / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(0, 0) == 0\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "tests/test_calc.py"], cwd=repo, check=True)
        ws._candidate_run_counts.clear()
        ev2 = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            ticket="Update test cases in tests/test_calc.py for new requirements",
        )
        assert ev2 is not None
        assert ev2.mech_status == MechStatus.UNMEASURED
        assert ev2.verifier_modified is True


# ==============================================================================
# (d) Untouched tests -> behavior unchanged
# ==============================================================================

def test_untouched_tests_behavior_unchanged(repo_with_tests):
    ws = repo_with_tests
    repo = ws.repo_dir

    # Candidate modifies only production code, tests are completely untouched
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    # optimized\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(
        id="sg-untouched",
        description="optimize calc",
        scope=["src/calc.py"],
        expects_tests=True,
    )

    cand_pass_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )

    scratch_spy = MagicMock()
    with patch.object(ws, "_run_test_subprocess", return_value=cand_pass_proc), \
         patch.object(ws, "_run_original_tests_in_scratch", scratch_spy), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert ev is not None
        assert ev.mech_status == MechStatus.PASS
        assert ev.evidence_level == EvidenceLevel.COUNTED
        assert ev.verifier_modified is False
        assert ev.test_surface_changed is False
        assert ev.mechanically_unverified is False

        # Scratch verification was NOT invoked because pre-existing tests were untouched
        scratch_spy.assert_not_called()


# ==============================================================================
# (e) Edited Makefile test recipe -> handled same as changed test surface
# ==============================================================================

def test_edited_makefile_test_recipe_handled_same_as_changed_test_surface(repo_with_tests):
    ws = repo_with_tests
    repo = ws.repo_dir

    # Initial commit with Makefile
    makefile = repo / "Makefile"
    makefile.write_text("test:\n\tpytest\n\nbuild:\n\tpython setup.py build\n", encoding="utf-8")
    subprocess.run(["git", "add", "Makefile"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add Makefile"], cwd=repo, check=True)
    base_commit = ws._get_head_commit()
    ws.base_commit = base_commit

    # 1. Edit Makefile to change the test recipe: pytest -> pytest -k dummy
    makefile.write_text("test:\n\tpytest -k dummy\n\nbuild:\n\tpython setup.py build\n", encoding="utf-8")
    subprocess.run(["git", "add", "Makefile"], cwd=repo, check=True)

    base_c = ws._get_base_content("Makefile", base_commit)
    cand_c = ws._get_candidate_content("Makefile")

    assert is_build_or_test_config("Makefile", base_c, cand_c) is True
    assert is_shared_test_surface("Makefile", base_c, cand_c) is True

    classified = ws.classify_touched_paths(staged_paths=["Makefile"])
    assert "Makefile" in classified["worker_staged_shared_test_surface"]

    # 2. When candidate test runs and passes with edited Makefile test recipe:
    subgoal = Subgoal(id="sg-makefile", description="edit makefile test", scope=["Makefile"], expects_tests=True)
    cand_pass_proc = subprocess.CompletedProcess(
        args=["make", "test"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=cand_pass_proc), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert ev is not None
        assert ev.mech_status == MechStatus.PASS
        assert ev.test_surface_changed is True
        assert ev.mechanically_unverified is True
        assert ev.unverified_kind in ("test_surface_changed", "verifier_modified")
        assert ev.evidence_level == EvidenceLevel.EXIT_ONLY

    # 2b. When candidate test fails with edited Makefile test recipe:
    cand_fail_proc = subprocess.CompletedProcess(
        args=["make", "test"],
        returncode=1,
        stdout="",
        stderr="make: *** [test] Error 1",
    )
    with patch.object(ws, "_run_test_subprocess", return_value=cand_fail_proc), \
         patch("shutil.which", return_value=sys.executable):
        ev_fail = ws.evaluate_tests(subgoal=subgoal, test_policy="always")
        assert ev_fail is not None
        assert ev_fail.mech_status == MechStatus.UNMEASURED
        assert ev_fail.mechanically_unverified is True

        ws._candidate_run_counts.clear()
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "unmeasured_retry_count": 0,
            "trajectory": [],
        }
        mock_gatekeeper = MagicMock()
        res_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)
        assert res_state.get("gate_status") == "unmeasured"
        assert res_state.get("mechanical_strike_count") == 0

    # 3. Conversely, editing ONLY non-test recipe (e.g. build target) does NOT flag test surface changed
    makefile.write_text("test:\n\tpytest\n\nbuild:\n\tpython setup.py build --fast\n", encoding="utf-8")
    subprocess.run(["git", "add", "Makefile"], cwd=repo, check=True)
    cand_c2 = ws._get_candidate_content("Makefile")

    assert is_build_or_test_config("Makefile", base_c, cand_c2) is False
    assert is_shared_test_surface("Makefile", base_c, cand_c2) is False
    classified2 = ws.classify_touched_paths(staged_paths=["Makefile"])
    assert "Makefile" not in classified2["worker_staged_shared_test_surface"]
