"""tests/test_phase7_test_surface_integrity.py

Tests for Phase 7: Test-surface and verifier integrity (corrected rule).
Verifies:
1. Classification of touched paths into 7 categories:
   pre-existing code, pre-existing test surface, worker-staged production code,
   worker-staged test files, worker-staged shared test surface, engine-generated,
   and protected verifier/command.
2. Shared surface changes cannot produce a parser-free strike:
   returns UNMEASURED(TEST_SURFACE_CHANGED) and 0 strikes in node_gate.
3. Targeted test file(s) created or edited are NOT shared surface:
   a broken test the worker created returns strikable FAIL and adds 1 strike in node_gate.
4. Editing a pre-existing test sets verifier_modified=true (shown to Jev) and failing run still strikes.
5. Worker-authored tests alone never produce counted or named evidence (forced to EXIT_ONLY),
   and never excuse a failing pre-existing test.
6. Collision between worker-staged path and engine-touched path returns
   UNMEASURED(ENGINE_WORKER_PATH_COLLISION) and blocks commit.
7. Engine outputs are removed on discard_engine_touched().
"""

from __future__ import annotations

import json
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
    TargetConfidence,
)
from jev.models import Subgoal, TestOutcome, ValidationVerdict
from jev.workspace import (
    PathCollisionError,
    Workspace,
    is_shared_test_surface,
    is_test_file,
)


@pytest.fixture
def clean_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    # Initial commit with README and an existing source and test file
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text("def test_add(): assert True\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)

    ws = Workspace(repo_dir=repo)
    return ws


# ==============================================================================
# 1. Classification of touched paths into 7 categories
# ==============================================================================

def test_classify_touched_paths_categories(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # 1. Pre-existing code: modify src/calc.py
    (repo / "src" / "calc.py").write_text("def add(a, b): return a + b + 0\n", encoding="utf-8")

    # 2. Pre-existing test surface: modify tests/test_calc.py
    (repo / "tests" / "test_calc.py").write_text("def test_add(): assert 1 + 1 == 2\n", encoding="utf-8")

    # 3. Worker-staged production code: create src/utils.py
    (repo / "src" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")

    # 4. Worker-staged test files: create tests/test_utils.py
    (repo / "tests" / "test_utils.py").write_text("def test_utils(): pass\n", encoding="utf-8")

    # 5. Worker-staged shared test surface: create tests/conftest.py and a helper
    (repo / "tests" / "conftest.py").write_text("# fixtures\n", encoding="utf-8")
    helpers_dir = repo / "tests" / "helpers"
    helpers_dir.mkdir(parents=True, exist_ok=True)
    (helpers_dir / "fixtures.py").write_text("# helper\n", encoding="utf-8")

    # 6. Engine-generated path
    ws.record_engine_touched("reports/engine_out.tmp")

    # 7. Protected verifier/command path
    jev_dir = repo / ".jev"
    jev_dir.mkdir(parents=True, exist_ok=True)
    (jev_dir / "verifier.sh").write_text("# verifier\n", encoding="utf-8")

    staged_paths = [
        "src/calc.py",
        "tests/test_calc.py",
        "src/utils.py",
        "tests/test_utils.py",
        "tests/conftest.py",
        "tests/helpers/fixtures.py",
        "reports/engine_out.tmp",
        ".jev/verifier.sh",
    ]

    classified = ws.classify_touched_paths(staged_paths=staged_paths)

    assert "src/calc.py" in classified["pre_existing_code"]
    assert "tests/test_calc.py" in classified["pre_existing_test_surface"]
    assert "src/utils.py" in classified["worker_staged_production_code"]
    assert "tests/test_utils.py" in classified["worker_staged_test_files"]
    assert "tests/conftest.py" in classified["worker_staged_shared_test_surface"]
    assert "tests/helpers/fixtures.py" in classified["worker_staged_shared_test_surface"]
    assert "reports/engine_out.tmp" in classified["engine_generated"]
    assert ".jev/verifier.sh" in classified["protected_verifier_command"]


# ==============================================================================
# 2. Shared test surface changes cannot produce a parser-free strike
# ==============================================================================

def test_shared_test_surface_failure_returns_unmeasured_and_zero_strikes(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Worker modifies shared test surface (e.g. pytest.ini or conftest.py)
    (repo / "tests" / "conftest.py").write_text("import pytest\n# invalid fixture syntax causing exit 1", encoding="utf-8")
    subprocess.run(["git", "add", "tests/conftest.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg-shared", description="change shared fixtures", scope=["tests/conftest.py"], expects_tests=True)

    # Mock candidate test execution exiting with failure (exit code 1)
    fail_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="",
        stderr="INTERNALERROR: conftest syntax error",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert evidence is not None
        assert evidence.mech_status == MechStatus.UNMEASURED
        assert evidence.reason == MechReason.TEST_SURFACE_CHANGED
        assert evidence.test_surface_changed is True
        assert evidence.mechanically_unverified is True

        # Clear candidate run counts before node_gate test
        ws._candidate_run_counts.clear()

        # Now verify node_gate does NOT strike on this unmeasured failure
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "unmeasured_retry_count": 0,
            "trajectory": [],
        }

        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

        assert result_state.get("gate_status") == "unmeasured"
        assert result_state.get("mechanical_strike_count") == 0  # 0 strikes!
        assert result_state.get("unmeasured_retry_count") == 1
        mock_gatekeeper.validate_subgoal.assert_not_called()


# ==============================================================================
# 3. Targeted test file(s) created or edited are NOT shared surface: own broken test strikes
# ==============================================================================

def test_worker_authored_broken_test_strikes(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Worker authored a new test file: tests/test_new_feature.py
    new_test_path = repo / "tests" / "test_new_feature.py"
    new_test_path.write_text("def test_fails(): assert False\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_new_feature.py"], cwd=repo, check=True)

    assert not is_shared_test_surface("tests/test_new_feature.py")
    assert is_test_file("tests/test_new_feature.py")

    subgoal = Subgoal(id="sg-own-test", description="add test", scope=["tests/test_new_feature.py"], expects_tests=True)

    # Mock candidate failing run on this targeted test
    fail_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_new_feature.py"],
        returncode=1,
        stdout="FAILED tests/test_new_feature.py::test_fails",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/test_new_feature.py"],
        )

        assert evidence is not None
        assert evidence.mech_status == MechStatus.FAIL
        assert evidence.test_surface_changed is False

        # Clear candidate run counts before node_gate test
        ws._candidate_run_counts.clear()

        # In node_gate, this must cost 1 strike!
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "trajectory": [],
        }

        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

        assert result_state.get("gate_status") == "mechanical_failure"
        assert result_state.get("mechanical_strike_count") == 1  # 1 strike!
        mock_gatekeeper.validate_subgoal.assert_not_called()


# ==============================================================================
# 4. Editing pre-existing test sets verifier_modified=True and failing run still strikes
# ==============================================================================

def test_editing_preexisting_test_sets_verifier_modified_and_strikes_on_failure(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Edit pre-existing tests/test_calc.py
    calc_test = repo / "tests" / "test_calc.py"
    calc_test.write_text("def test_add(): assert 1 == 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_calc.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg-edit-test", description="edit test", scope=["tests/test_calc.py"], expects_tests=True)

    # 4a. Failing candidate run
    fail_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_calc.py"],
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_add",
        stderr="",
    )

    # Base tests pass
    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch.object(ws, "_run_detached_base_tests", return_value={"base_status": BaseStatus.PASS, "valid_cache": True}), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/test_calc.py"],
        )

        assert evidence is not None
        assert evidence.mech_status == MechStatus.FAIL
        assert evidence.verifier_modified is True

        # Clear candidate run counts before node_gate test
        ws._candidate_run_counts.clear()

        # In node_gate, failing edited test strikes!
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

    # 4b. Passing candidate run: verifier_modified is shown to Jev
    calc_test.write_text("def test_add(): assert 1 == 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_calc.py"], cwd=repo, check=True)
    subgoal_pass = Subgoal(id="sg-edit-test-pass", description="edit test pass", scope=["tests/test_calc.py"], expects_tests=True)
    pass_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_calc.py"],
        returncode=0,
        stdout="1 passed in 0.01s",
        stderr="",
    )
    with patch.object(ws, "_run_test_subprocess", return_value=pass_proc), \
         patch("shutil.which", return_value=sys.executable):
        pass_ev = ws.evaluate_tests(
            subgoal=subgoal_pass,
            test_policy="always",
            test_targets=["tests/test_calc.py"],
        )

        assert pass_ev is not None
        assert pass_ev.mech_status == MechStatus.PASS
        assert pass_ev.verifier_modified is True

        # Clear candidate run counts before node_gate test
        ws._candidate_run_counts.clear()

        # When validated in node_gate, verifier_modified is passed to Jev
        mock_gatekeeper = MagicMock()
        mock_gatekeeper.validate_subgoal.return_value = ValidationVerdict(valid=True, reason="ok")
        state_pass = {
            "status": "in_progress",
            "current_subgoal": subgoal_pass,
            "mechanical_strike_count": 0,
            "trajectory": [],
        }
        node_gate(state_pass, workspace=ws, gatekeeper=mock_gatekeeper)
        call_kwargs = mock_gatekeeper.validate_subgoal.call_args[1]
        mech_detail = call_kwargs.get("mechanical_detail", "")
        assert "verifier_modified: true" in mech_detail


# ==============================================================================
# 5. Worker-authored tests alone never produce counted or named evidence,
#    and never excuse a failing pre-existing test
# ==============================================================================

def test_worker_authored_tests_alone_forced_to_exit_only(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Worker creates brand new test file
    new_test = repo / "tests" / "test_brand_new.py"
    new_test.write_text("def test_ok(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_brand_new.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg-worker-only", description="new tests", scope=["tests/test_brand_new.py"], expects_tests=True)

    # Mock test execution exiting 0 with counted tests: "5 passed"
    pass_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_brand_new.py"],
        returncode=0,
        stdout="5 passed in 0.05s",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=pass_proc), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/test_brand_new.py"],
        )

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    # Forced to EXIT_ONLY! Never COUNTED or NAMED
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert evidence.mechanically_unverified is True
    assert evidence.unverified_kind == "worker_authored_only"
    assert evidence.test_authorship == "worker_authored_only"
    assert evidence.full_suite_verified is False


def test_worker_authored_test_does_not_excuse_failing_preexisting_test(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Candidate has worker-authored test AND breaks pre-existing code/test
    (repo / "tests" / "test_worker.py").write_text("def test_worker(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_worker.py"], cwd=repo, check=True)

    subgoal = Subgoal(id="sg-mix", description="mixed tests", scope=["tests/test_worker.py"], expects_tests=True)

    # Full suite run: worker test passed, but pre-existing test failed!
    fail_proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_add\n1 failed, 1 passed in 0.05s",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch.object(ws, "_run_detached_base_tests", return_value={"base_status": BaseStatus.PASS, "valid_cache": True}), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert evidence is not None
        # Cannot be excused by passing worker test; candidate failed while base passed -> FAIL!
        assert evidence.mech_status == MechStatus.FAIL

        # Clear candidate run counts before node_gate test
        ws._candidate_run_counts.clear()

        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "trajectory": [],
        }
        res_state = node_gate(state, workspace=ws, gatekeeper=MagicMock())
        assert res_state.get("gate_status") == "mechanical_failure"
        assert res_state.get("mechanical_strike_count") == 1


# ==============================================================================
# 6. Collision between worker-staged path and engine-touched path
# ==============================================================================

def test_collision_between_worker_staged_and_engine_touched_blocks_commit_and_returns_unmeasured(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Engine touched a file
    collision_file = "build_output.log"
    (repo / collision_file).write_text("engine logs\n", encoding="utf-8")
    ws.record_engine_touched(collision_file)

    # Worker stages that same file
    subprocess.run(["git", "add", collision_file], cwd=repo, check=True)
    ws.worker_staged_paths.append(collision_file)

    subgoal = Subgoal(id="sg-collision", description="colliding file", scope=[collision_file], expects_tests=True)

    # 6a. evaluate_tests returns UNMEASURED(ENGINE_WORKER_PATH_COLLISION)
    evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")
    assert evidence is not None
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.ENGINE_WORKER_PATH_COLLISION
    assert evidence.mechanically_unverified is True
    assert evidence.unverified_kind == "collision"

    # 6b. commit_subgoal raises PathCollisionError
    with pytest.raises(PathCollisionError) as exc_info:
        ws.commit_subgoal()
    assert "Collision" in str(exc_info.value)


# ==============================================================================
# 7. Engine outputs removed on discard_engine_touched()
# ==============================================================================

def test_engine_outputs_removed_on_discard_engine_touched(clean_workspace):
    ws = clean_workspace
    repo = ws.repo_dir

    # Engine runs in a worktree
    wt = ws.create_subgoal_worktree(subgoal_id="sg-engine-clean")
    engine_file = wt / "engine_temp.json"
    engine_file.write_text('{"run": 1}\n', encoding="utf-8")
    ws.record_engine_touched("engine_temp.json")

    assert engine_file.exists()
    assert "engine_temp.json" in ws.engine_touched_paths

    # Discard engine touched paths
    ws.discard_engine_touched()

    assert not engine_file.exists()
    assert len(ws.engine_touched_paths) == 0
