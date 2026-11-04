# Parser Count Fix Report: Vitest and PHPUnit Test Counts

**Branch**: `fix-vitest-phpunit-counts`  
**Base**: `origin/master` (`22611e6`)

---

## 1. Files and Lines Changed

### Implementation
- `src/jev/runner_registry.py`:
  - Lines 866–883: Added `_add_phpunit_counts` and `_add_phpunit_ok` helpers implementing `passed = tests - failures - errors - skipped - incomplete` and `failed = failures + errors`.
  - Lines 922–929: Updated `vitest` branch to match only the `Tests` line (`Tests  <n> failed | <n> passed | <n> skipped | <n> todo (<total>)`), ignoring `Test Files` lines to eliminate double counting. Retained Jest-style `Tests: N passed, N total` line matching for backward compatibility.
  - Lines 1008–1018: Updated `phpunit` branch to parse `OK (<N> tests, <M> assertions)` with full test counts (`passed += N`) and calculate issue counts from `Tests: N, Assertions: M...`.

### Tests and Fixtures Added
- `tests/fixtures/runners/real/vitest_1.6.0_success.txt`: Real captured output from Vitest 1.6.0 with temporary path normalized to `<TMP>`.
- `tests/test_phase12b_vitest_counts.py`: 7 tests verifying real fixture output, multi-line output, failed/passed parity with Jest semantics, skipped tests, all-skipped, exclusion of lone `Test Files`, and noise rejection.
- `tests/test_phase12b_phpunit_counts.py`: 5 tests verifying OK line test counts, Failures line, Errors & Skipped line, noise rejection, and existing sample preservation.

### Documentation
- `docs/phases/phase12a-review-fixes.md`: Appended Addendum (post-fix) documenting the resolution of the Vitest/PHPUnit count issues and real outputs from Jest 29.7.0 and Mocha 10.8.2.
- `docs/phases/parser-count-fix.md`: This report.

---

## 2. How It Works

### Vitest Parser (`fix vitest-1`)
1. **Root Cause**: Previously, `parse_test_counts_summary` matched `(?:Test Files|Tests)` with a fixed `_add_one_pass()` (+1). Because Vitest outputs both `Test Files 1 passed (1)` and `Tests 1 passed (1)`, `passed` was erroneously incremented twice (+1 + 1 = 2) for a single test. Furthermore, counts on `Tests` lines with failed or skipped tests were unparsed.
2. **Resolution**:
   - The regex was refined to `r"^\s*Tests\s+(?P<body>(?:(?:\d+\s+(?:failed|passed|skipped|todo))\s*(?:\|\s*)?)+\s*\(\d+\))\s*$"`.
   - It matches exclusively lines starting with `Tests` (ignoring `Test Files`) and extracts all count tokens (`passed`, `failed`, `skipped`) via `add_word_counts`.
   - The legacy Jest-style `r"^\s*Tests:\s*(?P<body>.*\btotal\b.*)\s*$"` scanner remains intact.
   - `has_positive_pass` and `all_skipped` semantics mirror the Jest branch.

### PHPUnit Parser (`fix phpunit-2`)
1. **Root Cause**: Previously, `OK (<N> tests, <M> assertions)` called `_add_one_pass()`, incorrectly reporting `passed = 1` regardless of `<N>`.
2. **Resolution**:
   - `OK (<N> tests, <M> assertions)` now captures `total = N` and increments `passed += total`.
   - Issues lines (`Tests: N, Assertions: M, Failures: F, Errors: E, Skipped: S, Incomplete: I.`) parse all fields and compute:
     - `failed += failures + errors`
     - `skipped += skipped + incomplete`
     - `passed += max(0, total - failures - errors - skipped - incomplete)`
   - Note: PHPUnit output remains documentation-based (per https://docs.phpunit.de/) and is not verified against real PHP toolchain executions.

---

## 3. Rules and Mechanical Enforcement

| Rule | Mechanically Enforced by Test | Prompt-Only |
| :--- | :--- | :--- |
| Vitest real 1.6.0 output parsed with right passed count (1) | `test_vitest_real_fixture_gives_passed_1` | No |
| Vitest output with `Test Files` and `Tests` parses only `Tests` count | `test_vitest_test_files_and_tests_gives_passed_5` | No |
| Vitest failed + passed matches Jest semantics | `test_vitest_failed_and_passed_matches_jest_semantics` | No |
| Vitest passed + skipped reports positive with correct counts | `test_vitest_passed_and_skipped_is_positive` | No |
| Vitest all skipped sets `all_skipped=True` and not positive | `test_vitest_all_skipped_is_not_positive` | No |
| Vitest `Test Files` line alone is not parsed | `test_vitest_test_files_alone_is_not_counted` | No |
| Vitest noise line rejected | `test_vitest_noise_is_not_positive` | No |
| PHPUnit `OK (<N> tests, ...)` returns passed count = N | `test_phpunit_ok_summary_line_gives_passed_3` | No |
| PHPUnit failures line correctly computes passed and failed | `test_phpunit_failures_summary_line` | No |
| PHPUnit errors and skipped line correctly computes counts | `test_phpunit_errors_and_skipped_summary_line` | No |
| PHPUnit noise line rejected | `test_phpunit_noise_line_rejected` | No |
| PHPUnit existing sample remains positive | `test_phpunit_existing_sample_still_positive` | No |
| Existing parser, fixture, and conformance tests pass unmodified | Full test suite execution | No |

---

## 4. Unrequested Changes

- None. Only `src/jev/runner_registry.py`, test fixtures, test files, and doc reports were modified.

---

## 5. Honest Open Problems

1. **14 profiles remain unverified against real outputs**: Only 8 runner profiles have been validated against real runtime executions (`pytest`, `node-native-test`, `bun-test`, `cargo-test`, `go-test`, `jest`, `mocha`, `vitest`). The remaining 14 profiles still rely on documentation or synthesized fixtures.
2. **PHPUnit remains documentation-based**: The updated parser format for PHPUnit reflects https://docs.phpunit.de/ specifications but has not been verified against a real PHP runtime on this host.

---

## 6. Raw `scripts/verify_phase.py` Output

```text
=== Verification Report for base: origin/master ===
Branch: fix-vitest-phpunit-counts
Commit: 1d52f1da2d7a86a65d52b9e65e072d877d24d0ed

--- Diff stat vs origin/master ---
docs/phases/parser-count-fix.md                    | 86 ++++++++++++++++++++++
 docs/phases/phase12a-review-fixes.md               | 33 +++++++++
 src/jev/runner_registry.py                         | 37 ++++++++--
 .../fixtures/runners/real/vitest_1.6.0_success.txt |  6 ++
 tests/test_phase12b_phpunit_counts.py              | 58 +++++++++++++++
 tests/test_phase12b_vitest_counts.py               | 82 +++++++++++++++++++++
 6 files changed, 294 insertions(+), 8 deletions(-)

--- Test Changes ---
Added tests:
  + test_phpunit_ok_summary_line_gives_passed_3
  + test_phpunit_failures_summary_line
  + test_phpunit_errors_and_skipped_summary_line
  + test_phpunit_noise_line_rejected
  + test_phpunit_existing_sample_still_positive
  + test_vitest_real_fixture_gives_passed_1
  + test_vitest_test_files_and_tests_gives_passed_5
  + test_vitest_failed_and_passed_matches_jest_semantics
  + test_vitest_passed_and_skipped_is_positive
  + test_vitest_all_skipped_is_not_positive
  + test_vitest_test_files_alone_is_not_counted
  + test_vitest_noise_is_not_positive

--- Collected Test Counts ---
  tests/test_phase12b_phpunit_counts.py: 5 tests collected in 0.84s
  tests/test_phase12b_vitest_counts.py: 7 tests collected in 0.77s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:5109:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2449:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:5559:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1662:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1699:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
SKIPPED [1] tests\test_runner_fixtures.py:361: gradle not found
SKIPPED [1] tests\test_runner_fixtures.py:361: dotnet not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ctest not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ruby not found
SKIPPED [1] tests\test_runner_fixtures.py:361: php not found
SKIPPED [1] tests\test_runner_fixtures.py:325: Profile 'swiftpm-test' not supported on win32 (platforms: ['linux', 'darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:361: mix not found
SKIPPED [1] tests\test_runner_fixtures.py:325: Profile 'xcode-test' not supported on win32 (platforms: ['darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:361: terraform not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ansible-lint not found
SKIPPED [1] tests\test_runner_fixtures.py:361: helm not found
1248 passed, 24 skipped, 2 deselected, 2 warnings in 979.10s (0:16:19)
```
