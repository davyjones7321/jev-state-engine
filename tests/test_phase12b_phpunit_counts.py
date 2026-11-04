"""Tests for PHPUnit summary parsing (Step 2)."""
from jev.runner_registry import load_registry, parse_test_counts_summary


def test_phpunit_ok_summary_line_gives_passed_3():
    reg = load_registry()
    profile = reg.get_profile("phpunit")
    assert profile is not None
    output = "OK (3 tests, 3 assertions)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is True
    assert parsed["passed"] == 3
    assert parsed["failed"] == 0
    assert parsed["skipped"] == 0


def test_phpunit_failures_summary_line():
    reg = load_registry()
    profile = reg.get_profile("phpunit")
    assert profile is not None
    output = "Tests: 3, Assertions: 3, Failures: 1.\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=1)
    assert parsed["passed"] == 2
    assert parsed["failed"] == 1
    assert parsed["skipped"] == 0
    assert parsed["has_positive_pass"] is True


def test_phpunit_errors_and_skipped_summary_line():
    reg = load_registry()
    profile = reg.get_profile("phpunit")
    assert profile is not None
    output = "Tests: 3, Assertions: 3, Errors: 1, Skipped: 1.\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=1)
    assert parsed["passed"] == 1
    assert parsed["failed"] == 1
    assert parsed["skipped"] == 1
    assert parsed["has_positive_pass"] is True


def test_phpunit_noise_line_rejected():
    reg = load_registry()
    profile = reg.get_profile("phpunit")
    assert profile is not None
    output = "OK (3 tests) from cache\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["parsed"] is False
    assert parsed["has_positive_pass"] is False
    assert parsed["passed"] == 0


def test_phpunit_existing_sample_still_positive():
    reg = load_registry()
    profile = reg.get_profile("phpunit")
    assert profile is not None
    output = "OK (3 tests, 3 assertions)\n"
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is True
