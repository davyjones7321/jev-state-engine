"""tests/test_target_selection.py

Tests for Phase 5: Target selection, hierarchy, confidence, safe fallback,
heuristic widening, and missing target path detection.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechReason,
    MechStatus,
    TargetConfidence,
)
from jev.models import Subgoal, TestOutcome
from jev.runner_registry import RunnerProfile, build_targeted_command, load_registry
from jev.workspace import Workspace, is_shared_surface_path


@pytest.fixture
def clean_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    initial = repo / "README.md"
    initial.write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)
    return Workspace(repo_dir=repo)


# ==============================================================================
# 1. Exact file targets: pytest, bun, deno, mix
# ==============================================================================

def test_exact_file_target_pytest(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (ws.worktree_dir / "tests").mkdir()
    test_file = ws.worktree_dir / "tests/test_example.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "1 passed", "")
    with patch("shutil.which", return_value=sys.executable), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["tests/test_example.py"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = [sys.executable, "-m", "pytest", "-q", "tests/test_example.py"]
    mock_run.assert_called_once_with(expected_cmd, cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_exact_file_target_bun(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "bunfig.toml").touch()
    (ws.worktree_dir / "tests").mkdir()
    (ws.worktree_dir / "tests/foo.test.ts").touch()

    process = subprocess.CompletedProcess([], 0, "1 pass", "")
    with patch("shutil.which", return_value="/usr/bin/bun"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["tests/foo.test.ts"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["bun", "test", "tests/foo.test.ts"]
    mock_run.assert_called_once_with(["/usr/bin/bun", "test", "tests/foo.test.ts"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_exact_file_target_deno(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "deno.json").write_text("{}", encoding="utf-8")
    (ws.worktree_dir / "tests").mkdir()
    (ws.worktree_dir / "tests/foo_test.ts").touch()

    process = subprocess.CompletedProcess([], 0, "ok", "")
    with patch("shutil.which", return_value="/usr/bin/deno"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["tests/foo_test.ts"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["deno", "test", "tests/foo_test.ts"]
    mock_run.assert_called_once_with(["/usr/bin/deno", "test", "tests/foo_test.ts"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_exact_file_target_mix(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "mix.exs").write_text("defmodule App.MixProject do end\n", encoding="utf-8")
    (ws.worktree_dir / "test").mkdir()
    (ws.worktree_dir / "test/foo_test.exs").touch()

    process = subprocess.CompletedProcess([], 0, "1 test, 0 failures", "")
    with patch("shutil.which", return_value="/usr/bin/mix"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["test/foo_test.exs"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["mix", "test", "test/foo_test.exs"]
    mock_run.assert_called_once_with(["/usr/bin/mix", "test", "test/foo_test.exs"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


# ==============================================================================
# 2. Package targets: go, cargo
# ==============================================================================

def test_package_target_go(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "go.mod").write_text("module testapp\n", encoding="utf-8")
    (ws.worktree_dir / "pkg" / "calc").mkdir(parents=True)
    (ws.worktree_dir / "pkg" / "calc" / "calc_test.go").touch()

    process = subprocess.CompletedProcess([], 0, "ok  testapp/pkg/calc", "")
    with patch("shutil.which", return_value="/usr/bin/go"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["./pkg/calc"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["go", "test", "./pkg/calc"]
    mock_run.assert_called_once_with(["/usr/bin/go", "test", "./pkg/calc"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_package_target_cargo_test_file(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "Cargo.toml").write_text('[package]\nname = "testapp"\n', encoding="utf-8")
    (ws.worktree_dir / "tests").mkdir()
    (ws.worktree_dir / "tests/integration_test.rs").touch()

    process = subprocess.CompletedProcess([], 0, "test result: ok", "")
    with patch("shutil.which", return_value="/usr/bin/cargo"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["tests/integration_test.rs"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["cargo", "test", "--test", "integration_test"]
    mock_run.assert_called_once_with(["/usr/bin/cargo", "test", "--test", "integration_test"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_package_target_cargo_package(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "Cargo.toml").write_text('[package]\nname = "testapp"\n', encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "test result: ok", "")
    with patch("shutil.which", return_value="/usr/bin/cargo"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["my_crate"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["cargo", "test", "-p", "my_crate"]
    mock_run.assert_called_once_with(["/usr/bin/cargo", "test", "-p", "my_crate"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


# ==============================================================================
# 3. Class/filter targets: maven, gradle, dotnet
# ==============================================================================

def test_class_target_maven(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "BUILD SUCCESS", "")
    with patch("shutil.which", return_value="/usr/bin/mvn"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["AppTest"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["mvn", "test", "-Dtest=AppTest"]
    mock_run.assert_called_once_with(["/usr/bin/mvn", "test", "-Dtest=AppTest"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_class_target_gradle(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "build.gradle").write_text("// gradle\n", encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "BUILD SUCCESSFUL", "")
    with patch("shutil.which", return_value="/usr/bin/gradle"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["AppTest"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["gradle", "test", "--tests", "AppTest"]
    mock_run.assert_called_once_with(["/usr/bin/gradle", "test", "--tests", "AppTest"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


def test_filter_target_dotnet(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "App.csproj").write_text("<Project></Project>\n", encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "Passed!", "")
    with patch("shutil.which", return_value="/usr/bin/dotnet"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["AppTest"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["dotnet", "test", "--filter", "FullyQualifiedName~AppTest"]
    mock_run.assert_called_once_with(["/usr/bin/dotnet", "test", "--filter", "FullyQualifiedName~AppTest"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


# ==============================================================================
# 4. Regex target: cmake-ctest
# ==============================================================================

def test_regex_target_ctest(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\n", encoding="utf-8")

    process = subprocess.CompletedProcess([], 0, "100% tests passed", "")
    with patch("shutil.which", return_value="/usr/bin/ctest"), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        outcome = ws.run_tests(targets=["TestMath.*"])
        assert outcome == TestOutcome.PASSED

    expected_cmd = ["ctest", "--output-on-failure", "-R", "TestMath.*"]
    mock_run.assert_called_once_with(["/usr/bin/ctest", "--output-on-failure", "-R", "TestMath.*"], cwd=ws.worktree_dir, timeout=ws.test_timeout)
    assert ws.last_test_run["command"] == expected_cmd


# ==============================================================================
# 5. Unsupported file target falls back
# ==============================================================================

def test_unsupported_file_target_falls_back(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\n", encoding="utf-8")
    subprocess.run(["git", "add", "CMakeLists.txt"], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add cmake"], cwd=ws.worktree_dir, check=True, capture_output=True)
    (ws.worktree_dir / "tests").mkdir()
    (ws.worktree_dir / "tests/test_math.cpp").touch()
    ws.worker_staged_paths = ["tests/test_math.cpp"]

    subgoal = Subgoal(id="sg-ctest", description="test ctest fallback", goal_id="g1", scope=["tests/test_math.cpp"])

    process = subprocess.CompletedProcess([], 0, "100% tests passed", "")
    with patch("shutil.which", return_value="/usr/bin/ctest"), \
         patch.object(ws, "_run_test_subprocess", return_value=process):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/test_math.cpp"],
        )

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    # Fallback recorded in target_scope
    assert evidence.target_scope["kind"] == "fallback"
    assert evidence.target_scope["fallback_scope"] == {"kind": "full_suite"}
    assert evidence.target_rule == "unsupported_target_fallback"
    assert evidence.target_confidence == TargetConfidence.HEURISTIC
    # Heuristic pass stays exit_only
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY


# ==============================================================================
# 6. Heuristic widening for shared fixtures, helpers, and config
# ==============================================================================

@pytest.mark.parametrize("shared_file", [
    "conftest.py",
    "tests/fixtures/data.json",
    "tests/helpers/utils.py",
    "pyproject.toml",
    "poetry.lock",
    "pytest.ini",
    "schema.proto",
    "schema.sql",
    ".env.test",
])
def test_heuristic_widening_shared_surface(clean_workspace, shared_file):
    assert is_shared_surface_path(shared_file) is True

    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (ws.worktree_dir / "tests").mkdir(parents=True, exist_ok=True)
    (ws.worktree_dir / "tests/test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    
    # Simulate worker staging a shared file
    ws.worker_staged_paths.append(shared_file)
    subgoal = Subgoal(id="sg-widen", description="shared test", goal_id="g1", scope=["tests/test_app.py"])

    process = subprocess.CompletedProcess([], 0, "1 passed", "")
    with patch("shutil.which", return_value=sys.executable), \
         patch.object(ws, "_run_test_subprocess", return_value=process) as mock_run:
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/test_app.py"],
        )

    assert evidence is not None
    # Widened to full suite!
    assert evidence.target_scope["kind"] == "widened"
    assert evidence.target_rule == "widened_shared_surface"
    assert evidence.target_confidence == TargetConfidence.HEURISTIC
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    # Ran full suite without specific targets
    mock_run.assert_called_once_with([sys.executable, "-m", "pytest", "-q"], cwd=ws.worktree_dir, timeout=ws.test_timeout)


# ==============================================================================
# 7. Heuristic target failure widens once to narrowest proven scope (non-punitive)
# ==============================================================================

def test_heuristic_target_failure_widens_once_non_punitive(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subprocess.run(["git", "add", "pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    (ws.worktree_dir / "test_a.py").touch()
    ws.worker_staged_paths = ["test_a.py"]

    subgoal = Subgoal(id="sg-fail", description="test ctest fallback fail", goal_id="g1", scope=["test_a.py"])

    with patch.object(ws, "run_tests") as mock_runs:
        mock_runs.side_effect = [TestOutcome.FAILED, TestOutcome.PASSED]
        with patch("jev.runner_registry.build_targeted_command", return_value=(["mock_cmd"], "heuristic_target", TargetConfidence.HEURISTIC)):
            evidence = ws.evaluate_tests(
                subgoal=subgoal,
                test_policy="always",
                test_targets=["test_a.py"],
            )

    assert evidence is not None
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert evidence.target_rule == "target_selection_failure"
    assert evidence.target_confidence == TargetConfidence.HEURISTIC
    assert evidence.mechanically_unverified is True


# ==============================================================================
# 8. Missing target path detected before execution
# ==============================================================================

def test_missing_target_path_detected_before_execution(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subprocess.run(["git", "add", "pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    subgoal = Subgoal(id="sg-miss", description="missing target", goal_id="g1", scope=["tests/nonexistent.py"])

    with patch.object(ws, "_run_test_subprocess") as mock_exec, \
         patch.object(ws, "run_tests") as mock_runs:
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["tests/nonexistent.py"],
        )
        mock_exec.assert_not_called()
        mock_runs.assert_not_called()

    assert evidence is not None
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert evidence.target_rule == "missing_target_path"
    assert evidence.target_confidence == TargetConfidence.EXACT
    assert evidence.mechanically_unverified is True


# ==============================================================================
# 9. Targeted run is never labeled full-suite
# ==============================================================================

def test_targeted_run_never_labeled_full_suite(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (ws.worktree_dir / "test_a.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-target", description="targeted run", goal_id="g1", scope=["test_a.py"])

    process = subprocess.CompletedProcess([], 0, "1 passed", "")
    with patch("shutil.which", return_value=sys.executable), \
         patch.object(ws, "_run_test_subprocess", return_value=process):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["test_a.py"],
        )

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    # A targeted run is never labeled full-suite
    assert evidence.full_suite_verified is False


# ==============================================================================
# 10. Whether target is new at base is recorded in target_scope
# ==============================================================================

def test_target_new_at_base_recorded(clean_workspace):
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # File created on candidate, absent at base
    new_test = ws.worktree_dir / "test_new.py"
    new_test.write_text("def test_new(): pass\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-new", description="new target", goal_id="g1", scope=["test_new.py"])

    process = subprocess.CompletedProcess([], 0, "1 passed", "")
    with patch("shutil.which", return_value=sys.executable), \
         patch.object(ws, "_run_test_subprocess", return_value=process):
        evidence = ws.evaluate_tests(
            subgoal=subgoal,
            test_policy="always",
            test_targets=["test_new.py"],
        )

    assert evidence is not None
    assert evidence.target_scope["new_target"] is True
