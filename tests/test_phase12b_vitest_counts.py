"""Tests for Vitest summary parsing (Step 1)."""
from pathlib import Path

from jev.runner_registry import load_registry, parse_test_counts_summary


def test_vitest_real_fixture_gives_passed_1():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    fixture_path = Path("tests/fixtures/runners/real/vitest_1.6.0_success.txt")
    output = fixture_path.read_text(encoding="utf-8")
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is True
    assert parsed["passed"] == 1


def test_vitest_test_files_and_tests_gives_passed_5():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    output = "Test Files  2 passed (2)\n      Tests  5 passed (5)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is True
    assert parsed["passed"] == 5


def test_vitest_failed_and_passed_matches_jest_semantics():
    reg = load_registry()
    v_profile = reg.get_profile("vitest")
    j_profile = reg.get_profile("jest")
    assert v_profile is not None and j_profile is not None
    v_out = "Tests  1 failed | 2 passed (3)\n"
    j_out = "Tests: 1 failed, 2 passed, 3 total\n"
    v_parsed = parse_test_counts_summary(v_profile, v_out, exit_code=1)
    j_parsed = parse_test_counts_summary(j_profile, j_out, exit_code=1)
    assert v_parsed["passed"] == 2
    assert v_parsed["failed"] == 1
    assert v_parsed["has_positive_pass"] == j_parsed["has_positive_pass"]
    assert v_parsed["all_skipped"] == j_parsed["all_skipped"]


def test_vitest_passed_and_skipped_is_positive():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    output = "Tests  2 passed | 1 skipped (3)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["passed"] == 2
    assert parsed["skipped"] == 1
    assert parsed["has_positive_pass"] is True
    assert parsed["all_skipped"] is False


def test_vitest_all_skipped_is_not_positive():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    output = "Tests  3 skipped (3)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["all_skipped"] is True
    assert parsed["has_positive_pass"] is False


def test_vitest_test_files_alone_is_not_counted():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    output = "Test Files  1 passed (1)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["parsed"] is False
    assert parsed["has_positive_pass"] is False
    assert parsed["passed"] == 0


def test_vitest_noise_is_not_positive():
    reg = load_registry()
    profile = reg.get_profile("vitest")
    assert profile is not None
    output = "Tests 3 passed the gate\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is False
