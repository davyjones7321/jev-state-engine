"""tests/test_phase8_same_command_authority.py

Tests for Phase 8: Same-command evidence is the only comparison authority (verify and close gaps).

Verifies:
1. Candidate pass needs no base (0 base runs, comparison=NOT_COMPARED).
2. Candidate fail with unavailable base is never a regression (UNMEASURED with reason BASELINE_UNAVAILABLE, 0 strikes).
3. New target absent at base is NOT_APPLICABLE (distinct from BASELINE_UNAVAILABLE and UNMEASURED).
4. Legacy classification cannot add a strike in production gate decisions.
5. A failed attempt never lands (worktree torn down, integration branch unchanged).
6. Grep-based test over production gate code verifying safety invariants.
"""

from __future__ import annotations

import ast
import re
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
    MechanicalEvidence,
)
from jev.models import MechanicalCheckResult, Subgoal, TestOutcome, ValidationVerdict
from jev.workspace import Workspace


@pytest.fixture
def test_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Phase8 Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "phase8@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)

    (repo / "README.md").write_text("# Phase 8 Repo\n", encoding="utf-8")
    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text("def test_add(): assert 1 + 1 == 2\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True)

    # Create integration branch
    subprocess.run(["git", "branch", "integration"], cwd=repo, check=True)

    ws = Workspace(repo_dir=repo)
    return ws


# ==============================================================================
# 1. Candidate pass needs no base
# ==============================================================================

def test_candidate_pass_needs_no_base(test_repo):
    ws = test_repo
    repo = ws.repo_dir

    subgoal = Subgoal(id="sg-pass", description="clean pass", scope=["src/calc.py"], expects_tests=True)
    wt = ws.create_subgoal_worktree("sg-pass", base_ref="integration")

    pass_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_calc.py"],
        returncode=0,
        stdout="1 passed in 0.05s\n",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=pass_proc), \
         patch.object(ws, "_run_detached_base_tests") as mock_base_run, \
         patch("shutil.which", return_value=sys.executable):

        evidence = ws.evaluate_tests(subgoal=subgoal, test_targets=["tests/test_calc.py"])

        assert evidence is not None
        assert evidence.mech_status == MechStatus.PASS
        assert evidence.base_status is None
        assert evidence.comparison == Comparison.NOT_COMPARED
        # Base tests were never executed
        mock_base_run.assert_not_called()
        assert len(ws._base_run_counts) == 0


# ==============================================================================
# 2. Candidate fail with unavailable base is never a regression
# ==============================================================================

def test_candidate_fail_with_unavailable_base_is_never_a_regression(test_repo):
    ws = test_repo
    repo = ws.repo_dir

    subgoal = Subgoal(id="sg-fail-nobase", description="fail with no base", scope=["src/calc.py"], expects_tests=True)
    wt = ws.create_subgoal_worktree("sg-fail-nobase", base_ref="integration")

    # Worker breaks calculation
    (wt / "src" / "calc.py").write_text("def add(a, b): return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=wt, check=True)

    fail_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_calc.py"],
        returncode=1,
        stdout="FAILED tests/test_calc.py::test_add\n",
        stderr="",
    )

    # Base tests fail to start or cannot be measured
    unavailable_base_data = {
        "base_status": BaseStatus.UNMEASURED,
        "failing_ids": None,
        "valid_cache": False,
        "disagreed": False,
    }

    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch.object(ws, "_run_detached_base_tests", return_value=unavailable_base_data), \
         patch("shutil.which", return_value=sys.executable):

        evidence = ws.evaluate_tests(subgoal=subgoal, test_targets=["tests/test_calc.py"])

        assert evidence is not None
        assert evidence.mech_status == MechStatus.UNMEASURED
        assert evidence.reason == MechReason.BASELINE_UNAVAILABLE
        assert evidence.base_status == BaseStatus.UNMEASURED
        assert evidence.comparison == Comparison.INCONCLUSIVE
        assert evidence.mechanically_unverified is True

        # Now verify node_gate adjudication: unmeasured base failure NEVER strikes
        state = {
            "status": "in_progress",
            "current_subgoal": subgoal,
            "mechanical_strike_count": 0,
            "unmeasured_retry_count": 0,
            "trajectory": [],
            "current_worktree_path": str(wt),
            "current_worktree_branch": "jev-subgoal-sg-fail-nobase",
        }
        mock_gatekeeper = MagicMock()
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

        assert result_state.get("gate_status") == "unmeasured"
        assert result_state.get("mechanical_strike_count") == 0
        assert result_state.get("unmeasured_retry_count") == 1


# ==============================================================================
# 3. New target absent at base is NOT_APPLICABLE
# ==============================================================================

def test_new_target_absent_at_base_is_not_applicable(test_repo):
    ws = test_repo
    repo = ws.repo_dir

    subgoal = Subgoal(id="sg-new-target", description="new target absent at base", scope=["tests/test_brand_new.py"], expects_tests=True)
    wt = ws.create_subgoal_worktree("sg-new-target", base_ref="integration")

    # Worker introduces a new test file not present at base
    new_test = wt / "tests" / "test_brand_new.py"
    new_test.write_text("def test_brand_new(): assert False\n", encoding="utf-8")
    subprocess.run(["git", "add", "tests/test_brand_new.py"], cwd=wt, check=True)

    fail_proc = subprocess.CompletedProcess(
        args=["pytest", "tests/test_brand_new.py"],
        returncode=1,
        stdout="FAILED tests/test_brand_new.py::test_brand_new\n",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=fail_proc), \
         patch("shutil.which", return_value=sys.executable):

        evidence = ws.evaluate_tests(subgoal=subgoal, test_targets=["tests/test_brand_new.py"])

        assert evidence is not None
        assert evidence.mech_status == MechStatus.FAIL
        # Base status must be explicitly NOT_APPLICABLE
        assert evidence.base_status == BaseStatus.NOT_APPLICABLE
        assert evidence.base_status != BaseStatus.UNMEASURED
        assert evidence.base_status != MechReason.BASELINE_UNAVAILABLE
        assert evidence.comparison == Comparison.CANDIDATE_ONLY
        assert "not_applicable" in evidence.attribution_note


# ==============================================================================
# 4. Legacy classification cannot add a strike
# ==============================================================================

def test_legacy_classification_cannot_add_a_strike(test_repo):
    ws = test_repo
    subgoal = Subgoal(id="sg-legacy-string", description="legacy classification test", scope=["src/calc.py"], expects_tests=True)

    # Simulate an UNMEASURED mechanical evidence
    unmeasured_ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TOOLING_ERROR,
        mechanically_unverified=True,
    )

    # Legacy check result has test_classification="REGRESSION_DETECTED" and passed=False
    legacy_mech_result = MechanicalCheckResult(
        passed=False,
        failed_check="tests",
        detail="Mechanical test unmeasured (legacy classification string present)",
        checks={"build": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
        checks_run=["build", "tests"],
        test_classification=TestOutcome.REGRESSION_DETECTED.value,
        baseline_failures=2,
        new_failures=1,
        full_suite_clean=False,
        mech_evidence=unmeasured_ev,
    )

    state = {
        "status": "in_progress",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "trajectory": [],
    }
    mock_gatekeeper = MagicMock()

    with patch.object(ws, "get_staged_diff", return_value="diff --git a/src/calc.py b/src/calc.py\n+x=1\n"), \
         patch.object(ws, "run_mechanical_checks", return_value=legacy_mech_result):
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

        # Gate status must be unmeasured, and strike count MUST stay 0
        assert result_state.get("gate_status") == "unmeasured"
        assert result_state.get("mechanical_strike_count") == 0


# ==============================================================================
# 5. A failed attempt never lands
# ==============================================================================

def test_failed_attempt_never_lands(test_repo):
    ws = test_repo
    repo = ws.repo_dir

    # Record commit sha of integration branch before attempt
    initial_integration_sha = subprocess.run(
        ["git", "rev-parse", "integration"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    subgoal = Subgoal(id="sg-landing-test", description="attempt that fails", scope=["src/calc.py"], expects_tests=True)
    wt = ws.create_subgoal_worktree("sg-landing-test", base_ref="integration")

    # Worker modifies code in worktree
    (wt / "src" / "calc.py").write_text("def add(a, b): return 'broken'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/calc.py"], cwd=wt, check=True)

    # Mechanical checks fail
    fail_ev = MechanicalEvidence(
        mech_status=MechStatus.FAIL,
        base_status=BaseStatus.PASS,
        comparison=Comparison.CANDIDATE_ONLY,
    )
    fail_mech_result = MechanicalCheckResult(
        passed=False,
        failed_check="tests",
        detail="Unit tests failed.",
        checks={"build": {"ran": True, "passed": True}, "tests": {"ran": True, "passed": False}},
        checks_run=["build", "tests"],
        mech_evidence=fail_ev,
    )

    state = {
        "status": "in_progress",
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "trajectory": [],
        "integration_branch": "integration",
        "current_worktree_path": str(wt),
        "current_worktree_branch": "jev-subgoal-sg-landing-test",
    }
    mock_gatekeeper = MagicMock()

    with patch.object(ws, "run_mechanical_checks", return_value=fail_mech_result):
        result_state = node_gate(state, workspace=ws, gatekeeper=mock_gatekeeper)

        # Gate failed
        assert result_state.get("gate_status") == "mechanical_failure"
        assert result_state.get("mechanical_strike_count") == 1

        # Worktree must be torn down
        assert not wt.exists()

        # Integration branch commit must be UNCHANGED
        current_integration_sha = subprocess.run(
            ["git", "rev-parse", "integration"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        assert current_integration_sha == initial_integration_sha

        # File in integration branch or main must NOT have the broken edit
        calc_content = (repo / "src" / "calc.py").read_text(encoding="utf-8")
        assert "broken" not in calc_content
        assert "def add(a, b): return a + b" in calc_content


# ==============================================================================
# 6. Grep-based test over production gate code
# ==============================================================================

def test_grep_production_gate_code():
    repo_root = Path(__file__).resolve().parent.parent
    engine_file = repo_root / "src" / "jev" / "engine.py"
    workspace_file = repo_root / "src" / "jev" / "workspace.py"

    assert engine_file.exists()
    assert workspace_file.exists()

    engine_code = engine_file.read_text(encoding="utf-8")

    # 1. run_baseline_tests( is NOT called anywhere in engine.py
    assert "run_baseline_tests(" not in engine_code, "run_baseline_tests( found in src/jev/engine.py!"

    # 2. No decision branching in node_gate on baseline_failing_ids or new_failing_ids
    # Parse engine AST to inspect node_gate decisions
    engine_ast = ast.parse(engine_code, filename="src/jev/engine.py")

    node_gate_def = None
    for node in ast.walk(engine_ast):
        if isinstance(node, ast.FunctionDef) and node.name == "node_gate":
            node_gate_def = node
            break

    assert node_gate_def is not None, "node_gate function not found in src/jev/engine.py!"

    # In node_gate, find all if conditions and verify none check legacy classification for strikes
    legacy_decision_vars = {"baseline_failures", "new_failures", "baseline_failing_ids", "new_failing_ids", "full_suite_clean"}

    for subnode in ast.walk(node_gate_def):
        if isinstance(subnode, ast.If):
            cond_names = {n.id for n in ast.walk(subnode.test) if isinstance(n, ast.Name)}
            # Verify no if-condition tests legacy decision variables to increment mechanical strikes
            has_legacy_var = bool(cond_names & legacy_decision_vars)
            if has_legacy_var:
                # If a legacy variable is in condition, assert it is only for telemetry formatting or string construction
                body_code = ast.unparse(subnode)
                assert "mechanical_strike_count" not in body_code, (
                    f"Legacy variable {cond_names & legacy_decision_vars} used in decision that touches mechanical_strike_count!"
                )
