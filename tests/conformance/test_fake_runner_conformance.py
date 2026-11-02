"""Phase 12A: deterministic conformance cases driven by a fake runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from jev.mech_evidence import BaseStatus, EvidenceLevel, MechReason, MechStatus, TargetConfidence
from jev.models import Subgoal, TestOutcome
from jev.process_runner import run_command
from jev.runner_registry import RunnerProfile, build_targeted_command, parse_test_counts_summary
from jev.workspace import ResolvedTestCommand, Workspace, compute_failure_fingerprint, is_shared_surface_path


FAKE_RUNNER = Path(__file__).with_name("fake_runner_script.txt")


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "tests" / "test_existing.py").write_text("def test_existing(): pass\n", encoding="utf-8")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Conformance"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "conformance@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def _configure(tmp_path: Path, monkeypatch, recipes: dict, *, log: bool = True) -> tuple[Path, Path | None]:
    config = tmp_path / "fake-runner.json"
    state = tmp_path / "fake-runner-state.json"
    state.write_text("{}", encoding="utf-8")
    log_path = tmp_path / "fake-runner.log" if log else None
    payload = {"recipes": recipes, "state": str(state)}
    if log_path:
        payload["log"] = str(log_path)
    config.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("JEV_FAKE_RUNNER_CONFIG", str(config))
    return config, log_path


def _cmd() -> ResolvedTestCommand:
    return ResolvedTestCommand(
        ecosystem="pytest",
        command=[sys.executable, str(FAKE_RUNNER)],
        source="phase12a-fake-runner",
        runner_profile_id="pytest",
        registry_version="phase12a",
    )


@contextmanager
def _fake_command():
    with patch("jev.workspace.resolve_test_command", return_value=_cmd()):
        yield


def _evaluate(
    repo: Path,
    head: str,
    subgoal_id: str = "sg",
    *,
    tmp_path: Path,
    monkeypatch,
    recipes: dict,
    target: str | None = None,
    timeout: int | None = None,
    mutate: tuple[str, str] = ("app.py", "value = 2\n"),
    **kwargs,
):
    _configure(tmp_path, monkeypatch, recipes)
    ws = Workspace(repo_dir=repo, test_timeout=timeout)
    ws.stage_file_mutation(*mutate)
    subgoal = Subgoal(id=subgoal_id, description="conformance", scope=[mutate[0]], expects_tests=True)
    with _fake_command():
        return ws, ws.evaluate_tests(
            subgoal,
            base_commit=head,
            test_policy="always",
            test_targets=[target] if target else None,
            **kwargs,
        )


def _pass(text: str = "1 passed in 0.01s\n") -> dict:
    return {"exit": 0, "stdout": text}


def _fail(test_id: str = "tests/test_existing.py::test_existing") -> dict:
    return {"exit": 1, "stdout": f"FAILED {test_id}\n1 failed in 0.01s\n"}


@pytest.mark.parametrize(
    ("case", "recipe", "status", "level"),
    [
        ("counted pass", _pass(), MechStatus.PASS, EvidenceLevel.COUNTED),
        ("exit-only pass", {"exit": 0, "stdout": "FAKE PASS\n"}, MechStatus.PASS, EvidenceLevel.EXIT_ONLY),
        ("empty output exit 0", {"exit": 0, "stdout": ""}, MechStatus.PASS, EvidenceLevel.EXIT_ONLY),
        ("zero tests exit 0", {"exit": 0, "stdout": "0 passed, 2 skipped\n"}, MechStatus.PASS, EvidenceLevel.EXIT_ONLY),
        ("all skipped", {"exit": 0, "stdout": "2 skipped in 0.01s\n"}, MechStatus.PASS, EvidenceLevel.EXIT_ONLY),
        ("zero tests exit 1", {"exit": 1, "stdout": "no tests ran\n"}, MechStatus.UNMEASURED, EvidenceLevel.NONE),
    ],
)
def test_fake_runner_basic_verdicts(tmp_path, monkeypatch, case, recipe, status, level):
    repo, head = _git_repo(tmp_path)
    _, evidence = _evaluate(repo, head, tmp_path=tmp_path, monkeypatch=monkeypatch, recipes={"candidate": recipe})
    assert evidence.mech_status == status, case
    assert evidence.evidence_level == level, case
    if case == "zero tests exit 1":
        assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL


def test_named_pass_and_candidate_fail_base_pass(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, named = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _fail()], "base": [_fail(), _fail()]},
    )
    assert named.mech_status == MechStatus.PASS
    assert named.evidence_level == EvidenceLevel.NAMED

    repo2, head2 = _git_repo(tmp_path / "candidate-fail")
    _, failure = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "candidate-fail",
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _fail()], "base": _pass()},
        target="tests/test_existing.py",
    )
    assert failure.mech_status == MechStatus.FAIL
    assert failure.base_status == BaseStatus.PASS


def test_fail_fail_and_base_unavailable_are_not_false_strikes(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, unavailable = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        timeout=1,
        recipes={"candidate": [_fail(), _fail()], "base": {"sleep": 2, "exit": 0}},
    )
    assert unavailable.mech_status == MechStatus.UNMEASURED
    assert unavailable.reason == MechReason.BASELINE_UNAVAILABLE


def test_new_target_absent_at_base_and_modified_existing_test(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, new_target = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail("tests/test_new.py::test_new"), _fail("tests/test_new.py::test_new")]},
        target="tests/test_new.py",
        mutate=("tests/test_new.py", "def test_new(): assert False\n"),
        compile_res={"passed": True, "outcome": "passed"},
    )
    assert new_target.mech_status == MechStatus.FAIL
    assert new_target.base_status == BaseStatus.NOT_APPLICABLE
    assert new_target.target_rule == "provisional"

    repo2, head2 = _git_repo(tmp_path / "modified-test")
    _, modified = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "modified-test",
        monkeypatch=monkeypatch,
        recipes={"candidate": _pass(), "scratch": _fail()},
        mutate=("tests/test_existing.py", "def test_existing(): assert False\n"),
    )
    assert modified.mech_status == MechStatus.FAIL
    assert modified.verifier_modified is True


def test_flaky_candidate_and_flaky_base_are_unmeasured(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, candidate = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _pass()]},
    )
    assert candidate.mech_status == MechStatus.UNMEASURED
    assert candidate.reason == MechReason.FLAKY

    repo2, head2 = _git_repo(tmp_path / "flaky-base")
    _, base = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "flaky-base",
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _fail()], "base": [_fail(), _pass()]},
    )
    assert base.mech_status == MechStatus.UNMEASURED
    assert base.reason == MechReason.FLAKY


def test_repeated_and_changed_failure_fingerprints_are_distinct_conformance_cases(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, stable = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail("tests/test_existing.py::test_a"), _fail("tests/test_existing.py::test_a")], "base": _pass()},
    )
    assert stable.mech_status == MechStatus.FAIL

    repo2, head2 = _git_repo(tmp_path / "changed-fingerprint")
    _, changed = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "changed-fingerprint",
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail("tests/test_existing.py::test_a"), _fail("tests/test_existing.py::test_b")]},
    )
    assert changed.mech_status == MechStatus.UNMEASURED
    assert changed.reason == MechReason.FLAKY


def test_timeout_kills_descendants_and_missing_or_absent_commands_are_unmeasured(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    config, _ = _configure(
        tmp_path,
        monkeypatch,
        {"candidate": {"sleep": 30, "child_pid_file": str(child_pid_file)}},
        log=False,
    )
    result = run_command([sys.executable, str(FAKE_RUNNER)], cwd=tmp_path, timeout=2.0, env=os.environ.copy())
    assert result.timed_out is True
    assert result.descendants_remaining == 0
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    time.sleep(0.1)
    with pytest.raises((ProcessLookupError, OSError)):
        os.kill(child_pid, 0)

    repo, head = _git_repo(tmp_path / "missing")
    ws = Workspace(repo_dir=repo)
    ws.stage_file_mutation("app.py", "value = 2\n")
    subgoal = Subgoal(id="missing", description="missing", scope=["app.py"], expects_tests=True)
    missing = ResolvedTestCommand("pytest", ["jev-no-such-runner-12a"], "fake", runner_profile_id="pytest")
    with patch("jev.workspace.resolve_test_command", return_value=missing):
        missing_ev = ws.evaluate_tests(subgoal, base_commit=head, test_policy="always")
    assert missing_ev.reason == MechReason.MISSING_TOOL

    no_command = ResolvedTestCommand("pytest", None, "untrusted_repo_config", runner_profile_id="pytest")
    ws_no_command = Workspace(repo_dir=repo)
    ws_no_command.stage_file_mutation("app.py", "value = 3\n")
    with patch("jev.workspace.resolve_test_command", return_value=no_command):
        no_cmd_ev = ws_no_command.evaluate_tests(subgoal, base_commit=head, test_policy="always")
    assert no_cmd_ev.reason == MechReason.NO_COMMAND


def test_malformed_output_scrambling_and_fingerprints_are_conservative(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    malformed = {"exit": 1, "stdout": "runner crashed: malformed output\n"}
    _, evidence = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [malformed, malformed], "base": [malformed, malformed]},
    )
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.BASELINE_UNAVAILABLE

    clean = parse_test_counts_summary(None, "1 passed in 0.01s\n", exit_code=0)
    scrambled = parse_test_counts_summary(None, "output order changed\n", exit_code=0)
    assert clean["has_positive_pass"] is True
    assert scrambled["has_positive_pass"] is False
    assert compute_failure_fingerprint("FAILED test_x at line 10\n") == compute_failure_fingerprint("FAILED test_x at line 99\n")
    assert compute_failure_fingerprint("FAILED test_x\n") != compute_failure_fingerprint("FAILED test_y\n")


def test_heuristic_target_failure_widens_once_without_a_strike(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _configure(tmp_path, monkeypatch, {"candidate": _pass()})
    ws = Workspace(repo_dir=repo)
    ws.stage_file_mutation("app.py", "value = 2\n")
    subgoal = Subgoal(id="heuristic", description="heuristic target", scope=["app.py"], expects_tests=True)
    with patch(
        "jev.runner_registry.build_targeted_command",
        return_value=([sys.executable, str(FAKE_RUNNER)], "heuristic_target", TargetConfidence.HEURISTIC),
    ), patch.object(ws, "run_tests", side_effect=[TestOutcome.FAILED, TestOutcome.PASSED]):
        evidence = ws.evaluate_tests(
            subgoal,
            base_commit=head,
            test_policy="always",
            test_targets=["tests/test_existing.py"],
        )
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert evidence.target_rule == "target_selection_failure"


def test_lockfile_and_command_identity_changes_invalidate_comparison(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, lock = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _fail()], "base": _fail()},
        mutate=("Cargo.lock", "version = 3\n"),
    )
    assert lock.reason == MechReason.BASELINE_UNAVAILABLE
    assert lock.lockfile_changed is True

    repo2, head2 = _git_repo(tmp_path / "command-change")
    _, command = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "command-change",
        monkeypatch=monkeypatch,
        recipes={"candidate": _fail()},
        mutate=("pytest.ini", "[pytest]\naddopts = -x\n"),
    )
    assert command.reason == MechReason.COMMAND_IDENTITY_CHANGED
    assert command.verifier_modified is True


def test_verifier_modification_test_surface_collision_and_generated_mutation(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    _, verifier = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": _pass(), "scratch": _pass()},
        mutate=("pytest.ini", "[pytest]\naddopts = -q\n"),
    )
    assert verifier.mech_status == MechStatus.PASS
    assert verifier.verifier_modified is True

    repo2, head2 = _git_repo(tmp_path / "surface")
    _, surface = _evaluate(
        repo2,
        head2,
        tmp_path=tmp_path / "surface",
        monkeypatch=monkeypatch,
        recipes={"candidate": _fail()},
        mutate=("tests/conftest.py", "# changed shared fixture\n"),
    )
    assert surface.reason == MechReason.TEST_SURFACE_CHANGED

    repo_generated, head_generated = _git_repo(tmp_path / "generated-input")
    _, generated_input = _evaluate(
        repo_generated,
        head_generated,
        tmp_path=tmp_path / "generated-input",
        monkeypatch=monkeypatch,
        recipes={"candidate": _fail()},
        mutate=("tests/fixtures/generated.proto", "message Changed {}\n"),
    )
    assert is_shared_surface_path("tests/fixtures/generated.proto") is True
    assert generated_input.reason == MechReason.TEST_SURFACE_CHANGED

    repo3, head3 = _git_repo(tmp_path / "collision")
    _configure(tmp_path / "collision", monkeypatch, {"candidate": _pass()})
    ws = Workspace(repo_dir=repo3)
    ws.stage_file_mutation("app.py", "value = 2\n")
    ws.record_engine_touched("app.py")
    subgoal = Subgoal(id="collision", description="collision", scope=["app.py"], expects_tests=True)
    with _fake_command():
        collision = ws.evaluate_tests(subgoal, base_commit=head3, test_policy="always")
    assert collision.reason == MechReason.ENGINE_WORKER_PATH_COLLISION

    repo4, head4 = _git_repo(tmp_path / "generated")
    ws_generated = Workspace(repo_dir=repo4)
    ws_generated.create_subgoal_worktree("generated", base_ref=head4)
    generated = ws_generated.worktree_dir / "build" / "generated.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated\n", encoding="utf-8")
    ws_generated.record_engine_touched("build/generated.py")
    ws_generated.discard_engine_touched()
    assert not generated.exists()


def test_stale_cache_identity_and_unsupported_profile(tmp_path, monkeypatch):
    repo, head = _git_repo(tmp_path)
    ws, first = _evaluate(
        repo,
        head,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        recipes={"candidate": [_fail(), _fail(), _fail(), _fail()], "base": _fail()},
    )
    assert first.mech_status == MechStatus.PASS
    cache_size = len(ws._base_test_cache)
    ws.stage_file_mutation("requirements.txt", "pytest==2\n")
    with _fake_command():
        second = ws.evaluate_tests(
            Subgoal(id="sg-2", description="cache", scope=["requirements.txt"], expects_tests=True),
            base_commit=head,
            test_policy="always",
        )
    assert second.mech_status == MechStatus.PASS
    assert len(ws._base_test_cache) > cache_size

    profile = RunnerProfile(
        profile_id="unsupported-profile",
        registry_version="12a",
        language="fictional",
        runner="fake",
        platforms=[sys.platform],
        marker_files=[],
        executable_rule="path",
        test_argv_template=["fake"],
    )
    command, rule, confidence = build_targeted_command(profile, ["fake"], ["tests/test_existing.py"])
    assert command is None
    assert rule == "unsupported_target"
    assert confidence.value == "heuristic"
