import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence, Tuple, Union
from unittest.mock import patch

import pytest

from jev.models import CompileOutcome, Subgoal, TestOutcome
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)
from jev.workspace import ResolvedTestCommand, Workspace


def _create_real_git_repo(tmp_path: Path) -> Tuple[Path, str]:
    """Initializes a real git repo with an initial commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo_dir / ".gitignore").write_text(".jev-worktrees/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    (repo_dir / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True)
    return repo_dir, res.stdout.strip()


def _create_fake_runner(tmp_path: Path) -> Path:
    """Creates a scripted fake test runner executable in python."""
    runner_script = tmp_path / "fake_runner.py"
    runner_script.write_text(
        "import sys, os, time, json\n"
        "from pathlib import Path\n"
        "config_file = Path(os.environ.get('FAKE_RUNNER_CONFIG', 'fake_runner_config.json'))\n"
        "cfg = {}\n"
        "if config_file.exists():\n"
        "    try:\n"
        "        cfg = json.loads(config_file.read_text(encoding='utf-8'))\n"
        "    except Exception:\n"
        "        pass\n"
        "cwd_name = Path.cwd().name\n"
        "is_base = cwd_name.startswith('base-')\n"
        "mode = cfg.get('base_mode' if is_base else 'candidate_mode', cfg.get('mode', 'pass'))\n"
        "sleep_sec = cfg.get('base_sleep' if is_base else 'candidate_sleep', cfg.get('sleep', 0))\n"
        "if sleep_sec:\n"
        "    time.sleep(sleep_sec)\n"
        "if mode == 'pass':\n"
        "    sys.stdout.write('FAKE RUNNER PASSED\\n')\n"
        "    sys.exit(0)\n"
        "elif mode == 'pass_garbage':\n"
        "    sys.stdout.write('GARBAGE RANDOM PASS 0x992384792387498237498\\n')\n"
        "    sys.exit(0)\n"
        "elif mode == 'fail_clean':\n"
        "    sys.stdout.write('FAILED tests/test_foo.py::test_fail\\n')\n"
        "    sys.exit(1)\n"
        "elif mode == 'fail_garbage':\n"
        "    sys.stdout.write('!@#$%^&*()_+ GARBAGE CORRUPTED OUTPUT\\n')\n"
        "    sys.stderr.write('0xDEADBEEF UNKNOWN RUNNER FAILURE\\n')\n"
        "    sys.exit(1)\n"
        "elif mode == 'fail_empty':\n"
        "    sys.exit(1)\n"
        "elif mode == 'flaky_rerun':\n"
        "    marker = Path(cfg.get('marker_file', 'flaky.marker'))\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        sys.stdout.write('FAILED tests/test_foo.py::test_flake\\n')\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write('FAKE RUNNER PASSED\\n')\n"
        "    sys.exit(0)\n"
        "elif mode == 'flaky_fingerprint':\n"
        "    marker = Path(cfg.get('marker_file', 'flaky_fp.marker'))\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        sys.stdout.write('FAILED Alpha error in test\\n')\n"
        "        sys.exit(1)\n"
        "    sys.stdout.write('FAILED Beta error in test\\n')\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sys.stdout.write(f'Unknown mode: {mode}\\n')\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    return runner_script


def _set_runner_config(config_path: Path, monkeypatch, **kwargs) -> None:
    config_path.write_text(json.dumps(kwargs), encoding="utf-8")
    monkeypatch.setenv("FAKE_RUNNER_CONFIG", str(config_path))


# 1. Clean pass needs no baseline.
def test_01_clean_pass_needs_no_baseline(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"
    _set_runner_config(config_path, monkeypatch, mode="pass")

    # Add test file to repo and commit
    test_file = repo_dir / "tests" / "test_foo.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_foo"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-1", base_ref=head_commit)
    subgoal = Subgoal(id="sg-1", description="Clean change", scope=["app.py"], expects_tests=True)
    ws.stage_file_mutation("app.py", "x = 1\n")

    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_foo.py"])

    assert ev is not None
    assert ev.mech_status == MechStatus.PASS
    assert ev.evidence_level == EvidenceLevel.EXIT_ONLY
    assert ev.base_status is None
    assert ev.comparison == Comparison.NOT_COMPARED
    assert len(ws._base_run_counts) == 0


# 2. New test file failing (base NOT_APPLICABLE) strikes only when all 3 conditions hold; fails closed to UNMEASURED otherwise.
def test_02_new_target_failing_provisional_strike_conditions(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-new", base_ref=base_commit)
    subgoal = Subgoal(id="sg-new", description="Add new test", scope=["tests/test_new.py"], expects_tests=True)

    # Worker stages new test file (absent at base)
    ws.stage_file_mutation("tests/test_new.py", "def test_new_failing(): assert False\n")

    # Case A: all 3 conditions hold -> strikes (FAIL, provisional)
    _set_runner_config(config_path, monkeypatch, mode="fail_clean")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev1 = ws.evaluate_tests(
            subgoal,
            base_commit=base_commit,
            test_targets=["tests/test_new.py"],
            compile_res={"passed": True, "outcome": "passed"},
            attempt=1,
        )
    assert ev1.mech_status == MechStatus.FAIL
    assert ev1.base_status == BaseStatus.NOT_APPLICABLE
    assert ev1.comparison == Comparison.CANDIDATE_ONLY
    assert ev1.target_rule == "provisional"
    assert "not_applicable" in (ev1.attribution_note or "")

    # Case B: runner output is empty -> fails closed to UNMEASURED(TOOLING_ERROR)
    _set_runner_config(config_path, monkeypatch, mode="fail_empty")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev2 = ws.evaluate_tests(
            subgoal,
            base_commit=base_commit,
            test_targets=["tests/test_new.py"],
            compile_res={"passed": True, "outcome": "passed"},
            attempt=2,
        )
    assert ev2.mech_status == MechStatus.UNMEASURED
    assert ev2.reason == MechReason.TOOLING_ERROR
    assert ev2.base_status == BaseStatus.NOT_APPLICABLE


# 3. Modified existing test breaking (base passes) -> FAIL.
def test_03_modified_existing_test_breaking_base_passes_fails(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    # Add existing test at base commit
    test_file = repo_dir / "tests" / "test_mod.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_mod(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_mod"], cwd=repo_dir, check=True, capture_output=True)
    base_commit_with_test = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-mod", base_ref=base_commit_with_test)
    subgoal = Subgoal(id="sg-mod", description="Modify existing test", scope=["tests/test_mod.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_mod.py", "def test_mod(): assert False\n")

    # Candidate fails, base passes
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=base_commit_with_test, test_targets=["tests/test_mod.py"])

    assert ev.mech_status == MechStatus.FAIL
    assert ev.base_status == BaseStatus.PASS
    assert ev.comparison == Comparison.CANDIDATE_ONLY


# 4. Old broken tests in other files never run (targeted ignores them).
def test_04_targeted_ignores_unrelated_broken_tests(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"
    _set_runner_config(config_path, monkeypatch, mode="pass")

    # Create both broken test and good test at base commit
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_broken.py").write_text("def test_broken(): assert False\n", encoding="utf-8")
    (repo_dir / "tests" / "test_good.py").write_text("def test_good(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add tests"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-targeted", base_ref=head_commit)
    subgoal = Subgoal(id="sg-targeted", description="Touch good test", scope=["tests/test_good.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_good.py", "def test_good(): assert True\n")

    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_good.py"])

    assert ev.mech_status == MechStatus.PASS
    # Assert targeted execution only included test_good.py
    cmd_run = ws.last_test_run.get("argv", [])
    assert any("test_good.py" in arg for arg in cmd_run)
    assert not any("test_broken.py" in arg for arg in cmd_run)


# 5. Same command on both sides: assert argv on candidate == argv on base.
def test_05_same_command_argv_on_base_and_candidate(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_same.py").write_text("def test_same(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_same"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-same", base_ref=head_commit)
    subgoal = Subgoal(id="sg-same", description="Mutate test_same", scope=["tests/test_same.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_same.py", "def test_same(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_same.py"])

    assert ev.mech_status == MechStatus.FAIL
    cand_argv = ws.last_test_run["argv"]
    # Check that candidate argv and base argv match
    assert ev.command_identity["cmd"] == cand_argv
    assert any("test_same.py" in arg for arg in cand_argv)


# 6. Flaky: candidate fails then passes on rerun -> UNMEASURED(FLAKY).
def test_06_flaky_candidate_rerun_passes(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"
    marker_file = tmp_path / "flaky_run.marker"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_flake.py").write_text("def test_flake(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_flake"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-flake", base_ref=head_commit)
    subgoal = Subgoal(id="sg-flake", description="Flaky test", scope=["tests/test_flake.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_flake.py", "def test_flake(): pass # mutated\n")

    _set_runner_config(config_path, monkeypatch, mode="flaky_rerun", marker_file=str(marker_file))
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_flake.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.FLAKY
    assert ev.mechanically_unverified is True


# 7. Flaky: candidate fails twice with different fingerprints -> UNMEASURED(FLAKY).
def test_07_flaky_candidate_different_fingerprints(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"
    marker_file = tmp_path / "fp_run.marker"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_diff_fp.py").write_text("def test_diff_fp(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_diff_fp"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-fp", base_ref=head_commit)
    subgoal = Subgoal(id="sg-fp", description="Fingerprint mismatch", scope=["tests/test_diff_fp.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_diff_fp.py", "def test_diff_fp(): pass # mut\n")

    _set_runner_config(config_path, monkeypatch, mode="flaky_fingerprint", marker_file=str(marker_file))
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_diff_fp.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.FLAKY
    assert ev.mechanically_unverified is True


# 8. Timeout -> UNMEASURED(TIMEOUT).
def test_08_timeout_unmeasured(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_timeout.py").write_text("def test_timeout(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_timeout"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir, test_timeout=1)
    ws.create_subgoal_worktree("sg-timeout", base_ref=head_commit)
    subgoal = Subgoal(id="sg-timeout", description="Timeout test", scope=["tests/test_timeout.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_timeout.py", "def test_timeout(): pass # hang\n")

    _set_runner_config(config_path, monkeypatch, mode="pass", sleep=5)
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_timeout.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.TIMEOUT
    assert ev.mechanically_unverified is True


# 9. Missing tool -> UNMEASURED(MISSING_TOOL).
def test_09_missing_tool_unmeasured(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_missing.py").write_text("def test_missing(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_missing"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-tool", base_ref=head_commit)
    subgoal = Subgoal(id="sg-tool", description="Missing tool test", scope=["tests/test_missing.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_missing.py", "def test_missing(): pass # tool\n")

    fake_cmd = ResolvedTestCommand("pytest", ["nonexistent_tool_binary_xyz_12345"], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_missing.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.MISSING_TOOL
    assert ev.mechanically_unverified is True


# 10. Missing target -> no run, UNMEASURED(NO_TEST_EXECUTION_SIGNAL).
def test_10_missing_target_no_run_unmeasured(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-missing-target", base_ref=base_commit)
    subgoal = Subgoal(id="sg-missing-target", description="Missing target", scope=["app.py"], expects_tests=True)

    ev = ws.evaluate_tests(subgoal, base_commit=base_commit, test_targets=["tests/nonexistent_test.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert ev.mechanically_unverified is True
    # Pre-run check halts immediately; no test command is run
    assert ws.last_test_run is None


# 11. Base run fails to start or times out -> UNMEASURED(BASELINE_UNAVAILABLE).
def test_11_base_run_fails_to_start_or_timeout_unmeasured(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_base_to.py").write_text("def test_base_to(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_base_to"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir, test_timeout=1)
    ws.create_subgoal_worktree("sg-base-to", base_ref=head_commit)
    subgoal = Subgoal(id="sg-base-to", description="Base timeout", scope=["tests/test_base_to.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_base_to.py", "def test_base_to(): assert False\n")

    # Candidate fails stably, base sleeps 5s triggering timeout retry and final failure
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass", base_sleep=5)
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_base_to.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.BASELINE_UNAVAILABLE
    assert ev.mechanically_unverified is True


# 12. Both fail without parser IDs -> UNMEASURED(BASELINE_UNAVAILABLE).
def test_12_both_fail_without_parser_ids_baseline_unavailable(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_noparse.py").write_text("def test_noparse(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_noparse"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-noparse", base_ref=head_commit)
    subgoal = Subgoal(id="sg-noparse", description="No parser ids", scope=["tests/test_noparse.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_noparse.py", "def test_noparse(): assert False\n")

    # Both fail with unparseable garbage output
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_garbage", base_mode="fail_garbage")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_noparse.py"])

    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.BASELINE_UNAVAILABLE
    assert ev.base_status == BaseStatus.FAIL
    assert ev.comparison == Comparison.INCONCLUSIVE
    assert ev.mechanically_unverified is True


# 13. Cache: identical command hits cache; different targets/lockfile/tool misses.
def test_13_cache_identical_hits_different_misses(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_c1.py").write_text("def test_c1(): pass\n", encoding="utf-8")
    (repo_dir / "tests" / "test_c2.py").write_text("def test_c2(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_c1 and c2"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-c", base_ref=head_commit)
    subgoal = Subgoal(id="sg-c", description="Cache test", scope=["tests/test_c1.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_c1.py", "def test_c1(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev1 = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_c1.py"], attempt=1)
        assert ev1.mech_status == MechStatus.FAIL
        assert len(ws._base_test_cache) == 1
        base_runs_before = ws._base_run_counts.get("sg-c", 0)

        # Re-run identical command: must hit cache and NOT invoke base runner subprocess again
        ev2 = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_c1.py"], attempt=2)
        assert ev2.mech_status == MechStatus.FAIL
        assert ws._base_run_counts.get("sg-c", 0) == base_runs_before  # No new base runs!

        # Now change targets: different cache key -> cache miss
        ws.stage_file_mutation("tests/test_c2.py", "def test_c2(): assert False\n")
        ev3 = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_c2.py"], attempt=3)
        assert len(ws._base_test_cache) == 2  # New cache entry added!


# 14. Caps: 4th base run or 3rd candidate run triggers UNMEASURED(TOOLING_ERROR).
def test_14_caps_enforced_tooling_error(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_caps.py").write_text("def test_caps(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_caps"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-caps", base_ref=head_commit)
    subgoal = Subgoal(id="sg-caps", description="Caps test", scope=["tests/test_caps.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_caps.py", "def test_caps(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_clean", base_mode="pass")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        # Trigger candidate run cap (> 2 candidate runs per attempt)
        ws._candidate_run_counts["sg-caps#1"] = 2
        ev_cand_cap = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_caps.py"], attempt=1)
        assert ev_cand_cap.mech_status == MechStatus.UNMEASURED
        assert ev_cand_cap.reason == MechReason.TOOLING_ERROR

        # Reset candidate count and trigger base run cap (> 3 base runs per subgoal)
        ws._candidate_run_counts.clear()
        ws._base_run_counts["sg-caps"] = 3
        ev_base_cap = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_caps.py"], attempt=1)
        assert ev_base_cap.mech_status == MechStatus.UNMEASURED
        assert ev_base_cap.reason == MechReason.TOOLING_ERROR


# 15. Scrambled/garbage runner output degrades to UNMEASURED, never upgrades a failure to a pass.
def test_15_scrambled_garbage_output_degrades_never_upgrades(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_garbage.py").write_text("def test_garbage(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_garbage"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-garb", base_ref=head_commit)
    subgoal = Subgoal(id="sg-garb", description="Garbage output test", scope=["tests/test_garbage.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_garbage.py", "def test_garbage(): assert False\n")

    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_garbage", base_mode="fail_garbage")
    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_garbage.py"])

    # Must NEVER upgrade failure to PASS
    assert ev.mech_status != MechStatus.PASS
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.BASELINE_UNAVAILABLE


# 16. Real adversary test: runner emits garbage on both sides; strikes when exit codes say base PASS and candidate FAIL, but refuses to strike when both fail.
def test_16_adversary_garbage_output_both_sides(tmp_path, monkeypatch):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)
    runner_script = _create_fake_runner(tmp_path)
    config_path = tmp_path / "cfg.json"

    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_adv.py").write_text("def test_adv(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add test_adv"], cwd=repo_dir, check=True, capture_output=True)
    head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-adv", base_ref=head_commit)
    subgoal = Subgoal(id="sg-adv", description="Adversary test", scope=["tests/test_adv.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_adv.py", "def test_adv(): assert False\n")

    fake_cmd = ResolvedTestCommand("pytest", [sys.executable, str(runner_script)], source="fake_runner")

    # Adversary case A: base PASS (with garbage output) and candidate FAIL (with garbage output)
    # Parser-free exit code comparison strikes!
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_garbage", base_mode="pass_garbage")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev_strike = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_adv.py"], attempt=1)
    assert ev_strike.mech_status == MechStatus.FAIL
    assert ev_strike.base_status == BaseStatus.PASS
    assert ev_strike.comparison == Comparison.CANDIDATE_ONLY

    # Adversary case B: both base and candidate FAIL with garbage output
    # Refuses to strike! Degrades to UNMEASURED(BASELINE_UNAVAILABLE).
    ws._base_test_cache.clear()
    _set_runner_config(config_path, monkeypatch, candidate_mode="fail_garbage", base_mode="fail_garbage")
    with patch("jev.workspace.resolve_test_command", return_value=fake_cmd):
        ev_no_strike = ws.evaluate_tests(subgoal, base_commit=head_commit, test_targets=["tests/test_adv.py"], attempt=2)
    assert ev_no_strike.mech_status == MechStatus.UNMEASURED
    assert ev_no_strike.reason == MechReason.BASELINE_UNAVAILABLE
    assert ev_no_strike.base_status == BaseStatus.FAIL
    assert ev_no_strike.comparison == Comparison.INCONCLUSIVE


# 17. The my-harness case: 2 old broken tests, new passing test, targeted -> legacy label PASSED_EXIT_ONLY and full_suite_clean None.
def test_17_my_harness_case_targeted_passed_exit_only(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)

    # 2 old broken tests in base commit
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_old.py").write_text(
        "def test_old_1():\n    assert False, 'broken 1'\n\n"
        "def test_old_2():\n    assert False, 'broken 2'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add broken baseline tests"], cwd=repo_dir, check=True, capture_output=True)
    base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-target", base_ref=base_commit)
    subgoal = Subgoal(id="sg-target", description="Add new passing test", scope=["tests/test_new.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_new.py", "def test_new_feature():\n    assert True\n")

    # Real pytest run targeting only tests/test_new.py
    res = ws.run_mechanical_checks(subgoal, base_commit=base_commit, test_targets=["tests/test_new.py"])

    assert res.passed is True
    assert res.failed_check is None
    assert res.test_classification == TestOutcome.PASSED_EXIT_ONLY.value
    assert res.full_suite_clean is None
    assert res.baseline_failures == 0
    assert res.new_failures == 0
    assert res.mech_evidence is not None
    assert res.mech_evidence.mech_status == MechStatus.PASS
    assert res.mech_evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert res.mech_evidence.mechanically_unverified is True


# 18. The my-harness case untargeted -> full suite fails with the same 2 IDs -> regression-free with evidence named.
def test_18_my_harness_case_untargeted_regression_free_named(tmp_path):
    repo_dir, base_commit = _create_real_git_repo(tmp_path)

    # 2 old broken tests in base commit
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_old.py").write_text(
        "def test_old_1():\n    assert False, 'broken 1'\n\n"
        "def test_old_2():\n    assert False, 'broken 2'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add broken baseline tests"], cwd=repo_dir, check=True, capture_output=True)
    base_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=repo_dir)
    ws.create_subgoal_worktree("sg-untargeted", base_ref=base_commit)
    subgoal = Subgoal(id="sg-untargeted", description="Add new passing test", scope=["tests/test_new.py"], expects_tests=True)
    ws.stage_file_mutation("tests/test_new.py", "def test_new_feature():\n    assert True\n")

    # Real pytest run untargeted (test_targets=None)
    res = ws.run_mechanical_checks(subgoal, base_commit=base_commit, test_targets=None)

    assert res.passed is True
    assert res.failed_check is None
    assert res.test_classification == TestOutcome.REGRESSION_FREE_WITH_BASELINE_FAILURES.value
    assert res.full_suite_clean is False
    assert res.baseline_failures == 2
    assert res.new_failures == 0
    assert "tests/test_old.py::test_old_1" in res.baseline_failing_ids
    assert "tests/test_old.py::test_old_2" in res.baseline_failing_ids
    assert res.new_failing_ids == []
    assert res.mech_evidence is not None
    assert res.mech_evidence.mech_status == MechStatus.PASS
    assert res.mech_evidence.comparison == Comparison.SAME
    assert res.mech_evidence.evidence_level == EvidenceLevel.NAMED
    assert res.mech_evidence.mechanically_unverified is False
