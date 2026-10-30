"""tests/test_zero_test_evidence.py

Tests for Phase 6: Zero-test and skipped-test evidence.
Verifies:
1. zero_test_downgrade_markers and failure_identity_patterns are separate fields.
2. zero-test exit 1 with matching marker is never FAILED_BY_DIFF (downgrades to UNMEASURED).
3. genuine named failure stays available as diagnostic evidence.
4. failure_identity_patterns are diagnostic/comparison only and never prove a test ran.
5. all-skipped never becomes counted/named PASS (forces EXIT_ONLY).
6. scrambled output only degrades, never upgrades.
7. empty output with exit 0 and expected tests becomes weak or unmeasured by profile policy.
8. Replay of captured fixtures from Phase 2B across profiles.
"""

from __future__ import annotations

import json
import random
import re
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
from jev.runner_registry import (
    RunnerProfile,
    get_zero_test_downgrade_markers,
    is_zero_test_marker_match,
    load_registry,
    parse_test_counts_summary,
)
from jev.workspace import Workspace, parse_failing_test_ids


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


FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "runners" / "win32"


# ==============================================================================
# 1. Separate fields on RunnerProfile
# ==============================================================================

def test_markers_and_failure_identity_patterns_are_separate_fields():
    """zero_test_downgrade_markers and failure_identity_patterns must be separate fields."""
    registry = load_registry()
    profile = registry.get_profile("pytest")
    assert profile is not None

    assert hasattr(profile, "zero_test_downgrade_markers")
    assert hasattr(profile, "failure_identity_patterns")
    assert isinstance(profile.zero_test_downgrade_markers, list)
    assert isinstance(profile.failure_identity_patterns, list)

    # They must have different purposes and different content
    assert set(profile.zero_test_downgrade_markers) != set(profile.failure_identity_patterns)

    # Both must validate regex safety independently
    with pytest.raises(Exception):
        RunnerProfile(
            profile_id="bad-markers",
            registry_version="1.0.0",
            language="python",
            runner="pytest",
            platforms=["win32"],
            marker_files=["bad.ini"],
            executable_rule="path",
            test_argv_template=["pytest"],
            zero_test_downgrade_markers=["(a+)+"],  # Catastrophic backtracking
        )

    with pytest.raises(Exception):
        RunnerProfile(
            profile_id="bad-patterns",
            registry_version="1.0.0",
            language="python",
            runner="pytest",
            platforms=["win32"],
            marker_files=["bad.ini"],
            executable_rule="path",
            test_argv_template=["pytest"],
            failure_identity_patterns=["(a+)+"],  # Catastrophic backtracking
        )


# ==============================================================================
# 2. Zero-test exit 1 with matching marker is never FAILED_BY_DIFF
# ==============================================================================

def test_zero_test_exit_1_with_matching_marker_is_never_failed_by_diff_bun(clean_workspace):
    """Bun exit 1 with 'No tests found!' marker must downgrade to UNMEASURED, never FAILED_BY_DIFF."""
    ws = clean_workspace
    (ws.worktree_dir / "bunfig.toml").write_text("", encoding="utf-8")
    subgoal = Subgoal(id="sg-bun", description="bun test", goal_id="g1", scope=["src/index.ts"])

    # Load real Bun zero_tests fixture
    bun_fixture = json.loads((FIXTURES_ROOT / "bun-test.json").read_text(encoding="utf-8"))
    zero_case = bun_fixture["cases"]["zero_tests"]
    assert zero_case["exit_code"] == 1

    proc = subprocess.CompletedProcess(
        args=["bun", "test"],
        returncode=zero_case["exit_code"],
        stdout=zero_case["stdout"],
        stderr=zero_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="bun"):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    # Crucial rule: zero-test exit 1 is NEVER FAIL / FAILED_BY_DIFF
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert evidence.comparison == Comparison.NOT_COMPARED
    assert evidence.mechanically_unverified is True


def test_zero_test_exit_1_filter_matches_nothing_bun(clean_workspace):
    """Bun exit 1 with filter matching 0 tests must downgrade to UNMEASURED."""
    ws = clean_workspace
    (ws.worktree_dir / "bunfig.toml").write_text("", encoding="utf-8")
    subgoal = Subgoal(id="sg-bun-filter", description="bun test filter", goal_id="g1", scope=["src/index.ts"])

    bun_fixture = json.loads((FIXTURES_ROOT / "bun-test.json").read_text(encoding="utf-8"))
    filter_case = bun_fixture["cases"]["filter_matches_nothing"]
    assert filter_case["exit_code"] == 1

    proc = subprocess.CompletedProcess(
        args=["bun", "test", "-t", "nonexistent"],
        returncode=filter_case["exit_code"],
        stdout=filter_case["stdout"],
        stderr=filter_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="bun"):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
    assert evidence.mechanically_unverified is True


def test_zero_test_exit_5_pytest_marker(clean_workspace):
    """Pytest exit 5 with 'no tests ran' must downgrade to UNMEASURED."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-py", description="pytest", goal_id="g1", scope=["app.py"])

    pytest_fixture = json.loads((FIXTURES_ROOT / "pytest.json").read_text(encoding="utf-8"))
    zero_case = pytest_fixture["cases"]["zero_tests"]
    assert zero_case["exit_code"] == 5

    proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=zero_case["exit_code"],
        stdout=zero_case["stdout"],
        stderr=zero_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.UNMEASURED
    assert evidence.reason == MechReason.NO_TEST_EXECUTION_SIGNAL


def test_zero_test_exit_1_jest_and_vitest(clean_workspace):
    """Jest/Vitest exit 1 with zero tests marker must downgrade to UNMEASURED."""
    ws = clean_workspace
    subgoal = Subgoal(id="sg-js", description="jest", goal_id="g1", scope=["index.js"])

    # Jest
    jest_out = "No tests found, exiting with code 1\nRun with `--passWithNoTests` to exit with code 0"
    reg = load_registry()
    jest_profile = reg.get_profile("jest")
    assert is_zero_test_marker_match(jest_profile, jest_out, exit_code=1) is True

    # Vitest
    vitest_out = "No test files found, exiting with code 1"
    vitest_profile = reg.get_profile("vitest")
    assert is_zero_test_marker_match(vitest_profile, vitest_out, exit_code=1) is True


# ==============================================================================
# 3. Genuine named failure stays available as diagnostic evidence
# ==============================================================================

def test_genuine_named_failure_stays_available_as_diagnostic_evidence(clean_workspace):
    """When a runner produces a genuine failure, failing test IDs are preserved."""
    ws = clean_workspace
    (ws.worktree_dir / "bunfig.toml").write_text("", encoding="utf-8")
    subgoal = Subgoal(id="sg-bun-fail", description="bun fail", goal_id="g1", scope=["src/index.ts"])

    bun_fixture = json.loads((FIXTURES_ROOT / "bun-test.json").read_text(encoding="utf-8"))
    fail_case = bun_fixture["cases"]["fail"]
    assert fail_case["exit_code"] == 1

    proc = subprocess.CompletedProcess(
        args=["bun", "test"],
        returncode=fail_case["exit_code"],
        stdout=fail_case["stdout"],
        stderr=fail_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="bun"):
        ws.run_tests()

    last_run = ws.last_test_run
    assert last_run is not None
    assert last_run["failing_test_ids"] is not None
    # 'fail' test was captured from the failure identity pattern: \(fail\)\s+(.*)
    assert any("fail" in fid for fid in last_run["failing_test_ids"])


def test_failure_identity_patterns_never_prove_a_test_ran():
    """Failure identity patterns are diagnostic/comparison only and never prove a test ran."""
    registry = load_registry()
    bun_profile = registry.get_profile("bun-test")

    # Only failure output yields IDs
    fail_output = "(fail) my_test [1.2ms]\n"
    ids = parse_failing_test_ids(fail_output, exit_code=1, profile=bun_profile)
    assert ids == {"my_test"}

    # On exit 0, failure patterns cannot be used to invent or prove passed tests
    pass_output = "(pass) my_test [1.2ms]\n"
    ids_pass = parse_failing_test_ids(pass_output, exit_code=0, profile=bun_profile)
    assert ids_pass == set()


# ==============================================================================
# 4. All-skipped never becomes counted/named PASS
# ==============================================================================

def test_all_skipped_never_becomes_counted_named_pass_pytest(clean_workspace):
    """Pytest with all tests skipped must result in EXIT_ONLY, never COUNTED or NAMED."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (ws.worktree_dir / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    ws.worker_staged_paths = ["app.py"]
    subgoal = Subgoal(id="sg-py-skip", description="all skipped", goal_id="g1", scope=["app.py"])

    pytest_fixture = json.loads((FIXTURES_ROOT / "pytest.json").read_text(encoding="utf-8"))
    skip_case = pytest_fixture["cases"]["all_skipped"]
    assert skip_case["exit_code"] == 0

    proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=skip_case["exit_code"],
        stdout=skip_case["stdout"],
        stderr=skip_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value=sys.executable):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    # Crucial rule: all-skipped must NEVER be COUNTED or NAMED
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert evidence.full_suite_verified is False
    assert evidence.mechanically_unverified is True
    assert evidence.unverified_kind == "exit_only"


def test_all_skipped_never_becomes_counted_named_pass_bun(clean_workspace):
    """Bun with all tests skipped must result in EXIT_ONLY."""
    ws = clean_workspace
    (ws.worktree_dir / "bunfig.toml").write_text("", encoding="utf-8")
    (ws.worktree_dir / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add bunfig.toml"], cwd=ws.worktree_dir, check=True, capture_output=True)
    ws.worker_staged_paths = ["index.ts"]
    subgoal = Subgoal(id="sg-bun-skip", description="bun all skipped", goal_id="g1", scope=["src/index.ts"])

    bun_fixture = json.loads((FIXTURES_ROOT / "bun-test.json").read_text(encoding="utf-8"))
    skip_case = bun_fixture["cases"]["all_skipped"]
    assert skip_case["exit_code"] == 0

    proc = subprocess.CompletedProcess(
        args=["bun", "test"],
        returncode=skip_case["exit_code"],
        stdout=skip_case["stdout"],
        stderr=skip_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="bun"):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert evidence.full_suite_verified is False
    assert evidence.mechanically_unverified is True


def test_all_skipped_never_becomes_counted_named_pass_cargo(clean_workspace):
    """Cargo with 0 passed, 1 ignored must result in EXIT_ONLY."""
    ws = clean_workspace
    (ws.worktree_dir / "Cargo.toml").write_text("[package]\nname = 'test'\n", encoding="utf-8")
    (ws.worktree_dir / "src").mkdir(parents=True, exist_ok=True)
    (ws.worktree_dir / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add Cargo.toml"], cwd=ws.worktree_dir, check=True, capture_output=True)
    ws.worker_staged_paths = ["src/lib.rs"]
    subgoal = Subgoal(id="sg-cargo-skip", description="cargo skipped", goal_id="g1", scope=["src/lib.rs"])

    cargo_fixture = json.loads((FIXTURES_ROOT / "cargo-test.json").read_text(encoding="utf-8"))
    skip_case = cargo_fixture["cases"]["all_skipped"]
    assert skip_case["exit_code"] == 0

    proc = subprocess.CompletedProcess(
        args=["cargo", "test"],
        returncode=skip_case["exit_code"],
        stdout=skip_case["stdout"],
        stderr=skip_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="cargo"):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert evidence.full_suite_verified is False


def test_all_skipped_never_becomes_counted_named_pass_node(clean_workspace):
    """Node native test with pass 0, skipped 1 must result in EXIT_ONLY."""
    ws = clean_workspace
    (ws.worktree_dir / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8")
    (ws.worktree_dir / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add package.json"], cwd=ws.worktree_dir, check=True, capture_output=True)
    ws.worker_staged_paths = ["index.js"]
    subgoal = Subgoal(id="sg-node-skip", description="node skipped", goal_id="g1", scope=["index.js"])

    node_fixture = json.loads((FIXTURES_ROOT / "node-native-test.json").read_text(encoding="utf-8"))
    skip_case = node_fixture["cases"]["all_skipped"]
    assert skip_case["exit_code"] == 0

    proc = subprocess.CompletedProcess(
        args=["node", "--test"],
        returncode=skip_case["exit_code"],
        stdout=skip_case["stdout"],
        stderr=skip_case["stderr"],
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value="node"):
        evidence = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    assert evidence.evidence_level == EvidenceLevel.EXIT_ONLY
    assert evidence.full_suite_verified is False


# ==============================================================================
# 5. Scrambled output only degrades, never upgrades
# ==============================================================================

def test_scrambled_output_only_degrades_property(clean_workspace):
    """Property test: scrambling output can only degrade an evidence level, never upgrade it."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (ws.worktree_dir / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=ws.worktree_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add pytest.ini"], cwd=ws.worktree_dir, check=True, capture_output=True)
    ws.worker_staged_paths = ["app.py"]
    subgoal = Subgoal(id="sg-scramble", description="scramble", goal_id="g1", scope=["app.py"])

    valid_passing_stdout = "..                                                                       [100%]\n\n2 passed in 0.05s\n"

    # Baseline: valid passing output yields COUNTED pass
    proc_clean = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout=valid_passing_stdout,
        stderr="",
    )
    with patch.object(ws, "_run_test_subprocess", return_value=proc_clean), \
         patch("shutil.which", return_value=sys.executable):
        ev_clean = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert ev_clean.mech_status == MechStatus.PASS
    assert ev_clean.evidence_level == EvidenceLevel.COUNTED

    # Scramble output in various adversarial ways
    random.seed(42)
    scrambled_variants = [
        "asdf8903j24klasfd90234jlksdf",
        "".join(random.sample(valid_passing_stdout, len(valid_passing_stdout))),
        valid_passing_stdout[:10] + "???CORRUPT???" + valid_passing_stdout[-5:],
        "2 pas" + "x" * 20,
        "\x00\x01\x02\x03\xff\xfe",
    ]

    for i, variant in enumerate(scrambled_variants):
        ws._candidate_run_counts.clear()
        proc_scrambled = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=0,
            stdout=variant,
            stderr="",
        )
        with patch.object(ws, "_run_test_subprocess", return_value=proc_scrambled), \
             patch("shutil.which", return_value=sys.executable):
            ev_scrambled = ws.evaluate_tests(subgoal=subgoal, test_policy="always", attempt=i+1)

        # Must only degrade: COUNTED -> EXIT_ONLY (or UNMEASURED / NONE)
        # It must NEVER upgrade to NAMED!
        assert ev_scrambled.evidence_level in (EvidenceLevel.EXIT_ONLY, EvidenceLevel.NONE, None)
        assert ev_scrambled.evidence_level != EvidenceLevel.NAMED
        # Since input is corrupted, it cannot parse valid counts
        assert ev_scrambled.evidence_level != EvidenceLevel.COUNTED


def test_scrambled_failing_output_never_upgrades_to_pass(clean_workspace):
    """Scrambling failing output with exit 1 must never upgrade to PASS."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-fail-scramble", description="scramble fail", goal_id="g1", scope=["app.py"])

    proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=1,
        stdout="???GARBAGE SCRAMBLED OUTPUT???",
        stderr="ERR_UNKNOWN",
    )
    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert ev.mech_status != MechStatus.PASS


# ==============================================================================
# 6. Empty output with exit 0 and expected tests
# ==============================================================================

def test_empty_output_exit_0_weak_policy(clean_workspace):
    """Empty output with exit 0 when tests are expected results in weak (EXIT_ONLY) pass."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-empty", description="empty output", goal_id="g1", scope=["app.py"], expects_tests=True)

    proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="",
        stderr="",
    )

    with patch.object(ws, "_run_test_subprocess", return_value=proc), \
         patch("shutil.which", return_value=sys.executable):
        ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

    assert ev.mech_status == MechStatus.PASS
    # Empty output can NEVER be COUNTED or NAMED
    assert ev.evidence_level == EvidenceLevel.EXIT_ONLY
    assert ev.full_suite_verified is False
    assert ev.mechanically_unverified is True


def test_empty_output_exit_0_unmeasured_policy(clean_workspace):
    """Empty output with exit 0 under an unmeasured profile policy results in UNMEASURED."""
    ws = clean_workspace
    (ws.worktree_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subgoal = Subgoal(id="sg-empty-unm", description="empty output", goal_id="g1", scope=["app.py"], expects_tests=True)

    proc = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=0,
        stdout="",
        stderr="",
    )

    reg = load_registry()
    profile = reg.get_profile("pytest")
    profile.empty_output_behavior = "unmeasured"

    try:
        with patch.object(ws, "_run_test_subprocess", return_value=proc), \
             patch("shutil.which", return_value=sys.executable):
            ev = ws.evaluate_tests(subgoal=subgoal, test_policy="always")

        assert ev.mech_status == MechStatus.UNMEASURED
        assert ev.reason == MechReason.NO_TEST_EXECUTION_SIGNAL
        assert ev.mechanically_unverified is True
    finally:
        profile.empty_output_behavior = "weak"


# ==============================================================================
# 7. Markers are strictly downgrade-only
# ==============================================================================

def test_markers_are_strictly_downgrade_only():
    """zero_test_downgrade_markers can only downgrade a FAIL; they cannot upgrade anything."""
    reg = load_registry()
    bun_profile = reg.get_profile("bun-test")

    # Match zero-test output
    zero_out = "No tests found!\n"
    assert is_zero_test_marker_match(bun_profile, zero_out) is True

    # If exit code is 0 with zero-test marker, it does NOT upgrade to counted or named
    summary = parse_test_counts_summary(bun_profile, zero_out, exit_code=0)
    assert summary["has_positive_pass"] is False
    assert summary["passed"] == 0


# ==============================================================================
# 8. Replay of Phase 2B captured fixtures
# ==============================================================================

@pytest.mark.parametrize("profile_id, fixture_file", [
    ("bun-test", "bun-test.json"),
    ("pytest", "pytest.json"),
    ("cargo-test", "cargo-test.json"),
    ("node-native-test", "node-native-test.json"),
])
def test_phase2b_captured_fixtures_cases(profile_id, fixture_file):
    """Replays the captured cases from Phase 2B fixtures to verify exact classification."""
    fpath = FIXTURES_ROOT / fixture_file
    if not fpath.exists():
        pytest.skip(f"Fixture file {fixture_file} not found")

    data = json.loads(fpath.read_text(encoding="utf-8"))
    cases = data.get("cases", {})
    reg = load_registry()
    profile = reg.get_profile(profile_id)

    # 1. Zero tests case
    if "zero_tests" in cases:
        zt = cases["zero_tests"]
        out = (zt.get("stdout") or "") + "\n" + (zt.get("stderr") or "")
        ec = zt.get("exit_code")
        assert is_zero_test_marker_match(profile, out, ec) is True
        summary = parse_test_counts_summary(profile, out, ec)
        assert summary["has_positive_pass"] is False

    # 2. All skipped case
    if "all_skipped" in cases:
        sk = cases["all_skipped"]
        out = (sk.get("stdout") or "") + "\n" + (sk.get("stderr") or "")
        summary = parse_test_counts_summary(profile, out, sk.get("exit_code"))
        # Must be detected as all_skipped and cannot have positive pass
        assert summary["all_skipped"] is True
        assert summary["has_positive_pass"] is False

    # 3. Passing case
    if "pass" in cases:
        ps = cases["pass"]
        out = (ps.get("stdout") or "") + "\n" + (ps.get("stderr") or "")
        summary = parse_test_counts_summary(profile, out, ps.get("exit_code"))
        assert summary["all_skipped"] is False
        assert summary["has_positive_pass"] is True
        assert summary["passed"] >= 1
