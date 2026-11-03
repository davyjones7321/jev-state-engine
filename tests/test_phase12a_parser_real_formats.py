"""Step 1c regression table for documented runner success summaries."""

import pytest

from jev.runner_registry import load_registry, parse_test_counts_summary


# The expected values preserve the positive behavior already established by
# Step 1b and the current parser. New documented formats are additive.
DOCUMENTED_SUCCESS_CASES = {
    "pytest": ("collected 3 items\n3 passed, 1 warning in 0.12s\n", True),
    "node-native-test": ("ℹ tests 3\nℹ pass 3\nℹ fail 0\nℹ skipped 0\n", True),
    "jest": ("Test Suites: 1 passed, 1 total\nTests: 3 passed, 3 total\n", True),
    "mocha": ("  3 passing (12ms)\n", True),
    "vitest": (" Test Files  1 passed (1)\n      Tests  3 passed (3)\n", True),
    "bun-test": (" 3 pass\n 0 fail\n 0 skip\n", True),
    "deno-test": ("ok | 3 passed | 0 failed (12ms)\n", True),
    "go-test": ("ok\texample.test\t0.012s\n", True),
    "cargo-test": (
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.01s\n",
        True,
    ),
    "maven-surefire": ("[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n", True),
    "gradle-test": ("3 test completed, 0 failed\n", True),
    "dotnet-test": (
        "Passed!  - Failed:     0, Passed:     3, Skipped:     0, Total:     3, "
        "Duration: 12 ms - Tests.dll (net8.0)\n"
        "Total tests: 3\n     Passed: 3\n",
        True,
    ),
    "cmake-ctest": ("100% tests passed, 0 tests failed out of 3\n", True),
    "ruby-rake": ("3 runs, 3 assertions, 0 failures, 0 errors, 0 skips\n", True),
    "phpunit": ("OK (3 tests, 3 assertions)\n", True),
    "swiftpm-test": (
        "Executed 3 tests, with 0 failures (0 unexpected) in 0.003 (0.004) seconds\n",
        True,
    ),
    "mix-test": ("3 tests, 0 failures\n", True),
    "xcode-test": (
        "Executed 3 tests, with 0 failures (0 unexpected) in 0.003 (0.004) seconds\n",
        True,
    ),
    "terraform": ("Success! The configuration is valid.\n", False),
    "ansible": ("Passed: 0 failure(s), 0 warning(s) in 3 files processed of 3\n", True),
    "helm": ("1 chart(s) linted, 0 chart(s) failed\n", True),
}


@pytest.mark.parametrize("profile_id, case", sorted(DOCUMENTED_SUCCESS_CASES.items()))
def test_documented_real_success_formats_are_additive(profile_id, case):
    output, expected_positive = case
    profile = load_registry().get_profile(profile_id)
    assert profile is not None
    parsed = parse_test_counts_summary(profile, output, exit_code=0)
    assert parsed["has_positive_pass"] is expected_positive, (profile_id, output, parsed)
