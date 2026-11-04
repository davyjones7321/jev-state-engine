# Phase 12A Review Fixes Report

Base: `origin/phase12a-conformance-suite` (`a5785f3`).  
Branch: `phase12a-review-fixes`.

## 1. Changes by commit

All seven commits above the base are included below. Line references are to
the final files on this branch.

### `a5785f3` — `fix 12a-1: scrambled-output rule checked over many inputs`

- `tests/conformance/test_fake_runner_conformance.py:342–422`: added the
  fixed-seed scrambling, truncation, duplication, noise, and case-variation
  matrix. It preserves a positive result only when a complete anchored summary
  remains and rejects narrative `N passed` phrases.
- The test covers both successful and failing source output with exit codes 0
  and 1.

### `b1d76b9` — `fix 12a-1b: summary lines anchored per runner profile`

- `src/jev/runner_registry.py:780–1048`: replaced broad count heuristics with
  profile-specific, complete-line grammars and conservative evidence flags.
- `tests/conformance/test_fake_runner_conformance.py:425–489`: added the
  per-profile noise rejection, true-summary, unsupported-profile, and
  truncated-pytest checks.
- The parser scans only anchored runner summary shapes, so ordinary prose such
  as `step 3 passed the lint check` cannot create counted evidence.

### `8040562` — `fix 12a-3: exclude unknown profiles from metrics`

- `src/jev/ledger.py:95–113`: marks missing or explicitly unknown runner
  profiles outside the eligible/strong-measured population while retaining
  them in tooling uncertainty.
- `tests/test_phase12a_metrics_review.py:7–26`: verifies that an unknown
  profile is not strong measured evidence.

### `8c5e19a` — `fix 12a-4: phase report addendum and final verification`

- `docs/phases/phase12a.md:205–304`: added the original review-fix history,
  test results, and verifier transcript. The later Correction paragraph in
  section 9 records that the historical Step 2 hold was superseded by the
  additive test in `aa78e9e` and restates the real-output limitation.

### `f733cc7` — `fix 12a-1c: real success formats per runner profile, regression table`

- `src/jev/runner_registry.py:918–1026`: added documented Mocha, Vitest,
  Deno, current and older .NET, PHPUnit, Ruby/Minitest, SwiftPM/Xcode,
  Ansible, and Helm formats as unions with the existing grammars. Terraform
  remains exit-code-only.
- `tests/test_phase12a_parser_real_formats.py:10–56`: table-drives one
  documented multi-line success format for every registry profile and checks
  positive recognition without removing existing positives.
- `docs/phases/phase12a.md:227–267`: records the pre-fix failures, the full
  old-versus-new table, and the real-capture limitation.
- The one-time old parser was loaded from `git show b1d76b9~1` in a temporary
  file outside the repository and was deleted afterward.

### `aa78e9e` — `fix 12a-2: independent cross-platform child-liveness test`

- `tests/conformance/test_fake_runner_conformance.py:277–318`: added
  `_pid_is_alive_cross_platform`, using POSIX `os.kill(pid, 0)` or Windows
  `OpenProcess`/`GetExitCodeProcess` with `STILL_ACTIVE = 259`, and a new
  timeout test retaining the `descendants_remaining == 0` assertion.
- The required failure proof temporarily disabled the two POSIX `os.killpg`
  calls. Because this run was on Windows, the equivalent Windows
  `TerminateJobObject` cleanup and final handle close were also temporarily
  disabled to exercise the same cleanup path. The failure was at the
  independent liveness assertion itself; `src/jev/process_runner.py` was then
  restored exactly and no `src/` change is in this commit.
- `psutil` was not in `requirements.txt`, so no dependency was added.

### `063f419` — `fix 12a-3b (phase 11): show total rejections and acceptances next to assessed counts`

- `src/jev/ledger.py:67–75, 120–138`: adds the two additive accumulator keys
  and counts all four rejection outcomes and all mechanical `PASS` verdicts.
  The shared `finish` path places them in totals and groups; the CLI JSON uses
  that same report path (`scripts/ledger_report.py:73–77`). Existing assessed
  counts, values, and rate denominators are unchanged.
- `tests/test_phase12a_totals.py:13–74`: verifies two rejections with one
  label, two acceptances with one label, both groups and CLI JSON, and zero
  totals for an empty ledger.

## 2. Operation

The parser uses profile-specific anchored grammars and reports counted evidence
only for complete recognized summaries. The liveness test launches the same
fake runner as the existing timeout case, independently checks the recorded
child PID, and also checks the process-runner descendant count. The Phase 11
report accumulates coverage totals independently from optional assessment
labels, then serializes totals and groups through the existing CLI JSON path.

## 3. Rule enforcement

| Rule | Enforced by named test | Prompt-only |
|---|---|---|
| Scrambled output preserves positive evidence only when a valid summary remains | `test_scrambled_output_many_fixed_seed_variants_only_preserve_positive_summary` | No |
| Narrative `N passed` noise is rejected for every profile | `test_summary_parser_rejects_noise_phrases_for_every_registry_profile` | No |
| Existing profile positives remain positive | `test_summary_parser_accepts_anchored_true_summary_per_profile`, `test_documented_real_success_formats_are_additive` | No |
| Unknown runner profiles are excluded from strong measured metrics | `test_unknown_runner_profile_is_not_strong_measured_evidence` | No |
| New liveness test independently proves child death and retains descendant cleanup | `test_timeout_independently_proves_child_liveness_cleanup` | No |
| Disabled-cleanup proof fails at the independent liveness assertion | Recorded in this report from the required temporary mutation | Yes |
| Rejection and acceptance totals coexist with assessed counts | `test_totals_groups_and_metrics_cli_show_assessment_coverage` | No |
| Empty ledgers report zero coverage totals | `test_empty_ledger_has_zero_coverage_totals` | No |
| New totals appear in groups and CLI JSON | `test_totals_groups_and_metrics_cli_show_assessment_coverage` | No |
| Existing tests are not edited or deleted | Git diff review | Yes |
| Work remains on `phase12a-review-fixes`, uses explicit staging, and is pushed only to that branch | Git status/history review | Yes |

## 4. Verification before final documentation

- Step 1c new regression table: **21 passed**.
- Step 1c existing parser, fixture, conformance, and Phase 6 checks:
  **152 passed, 17 skipped**.
- Step 1c full offline suite: **1209 passed, 24 skipped, 2 deselected,
  2 warnings**.
- Step 2 focused test: **1 passed, 46 deselected**.
- Step 2 full offline suite: **1210 passed, 24 skipped, 2 deselected,
  2 warnings**.
- Step 3b focused Phase 11/review/new checks: **19 passed**.
- Step 3b full offline suite: **1212 passed, 24 skipped, 2 deselected,
  2 warnings**.
- Existing tests were not edited, deleted, or skipped. `session-1-v2.md`
  remains intentionally untracked.

## 5. Unrequested changes

- No unrequested committed changes were made.
- The Step 2 proof used temporary, uncommitted cleanup disables in
  `src/jev/process_runner.py` because this verification host was Windows;
  that file was restored exactly and is absent from `aa78e9e`.

## 6. Open problems

- Seventeen of the 21 runner profiles have no captured real output. Their
  success formats come from documentation or existing fixture conventions,
  not real runs.
- The Windows liveness helper was only run on Windows, the platform available
  for this verification.
- The Vitest passed count reports test files instead of tests, and the PHPUnit
  count reports `1` instead of the real number of tests. The verdict is
  unaffected, but those counts are inexact.
- Step 2 originally conflicted with the no-edit rule for the existing timeout
  test and was completed by adding a new test instead.
- Offline verification still skips unavailable toolchains and fixture setup;
  the suite records those environmental skips.

## 7. Raw `verify_phase.py` output

The following transcript was produced by running the verifier with a clean
committed tree against `origin/phase12a-conformance-suite`:

```text
=== Verification Report for base: origin/phase12a-conformance-suite ===
Branch: phase12a-review-fixes
Commit: 90a7850214a8c45c3c1a864ddb861c7670d3b2f0

--- Diff stat vs origin/phase12a-conformance-suite ---
docs/phases/phase11.md                            |  18 ++
docs/phases/phase12a-review-fixes.md              | 154 +++++++++
docs/phases/phase12a.md                           | 152 +++++++++
src/jev/ledger.py                                 |  15 +-
src/jev/runner_registry.py                        | 360 ++++++++++++++--------
tests/conformance/test_fake_runner_conformance.py | 198 +++++++++++-
tests/test_phase12a_metrics_review.py             |  26 ++
tests/test_phase12a_parser_real_formats.py        |  56 ++++
tests/test_phase12a_totals.py                     |  74 +++++
9 files changed, 926 insertions(+), 127 deletions(-)

--- Test Changes ---
Added tests:
  + test_timeout_independently_proves_child_liveness_cleanup
  + test_scrambled_output_many_fixed_seed_variants_only_preserve_positive_summary
  + test_summary_parser_rejects_noise_phrases_for_every_registry_profile
  + test_summary_parser_accepts_anchored_true_summary_per_profile
  + test_profiles_without_safe_count_shape_remain_exit_only
  + test_truncated_pytest_count_remains_positive_when_count_is_intact
  + test_unknown_runner_profile_is_not_strong_measured_evidence
  + test_documented_real_success_formats_are_additive
  + test_totals_groups_and_metrics_cli_show_assessment_coverage
  + test_empty_ledger_has_zero_coverage_totals

--- Collected Test Counts ---
  tests/conformance/test_fake_runner_conformance.py: 47 tests collected in 1.27s
  tests/test_phase12a_metrics_review.py: 1 test collected in 1.01s
  tests/test_phase12a_parser_real_formats.py: 21 tests collected in 0.85s
  tests/test_phase12a_totals.py: 2 tests collected in 0.96s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:5021:                verify_classification = "FULL_SUITE_PASSED"
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
1212 passed, 24 skipped, 2 deselected, 2 warnings in 1044.53s (0:17:24)
```


## 8. Addendum (post-fix)

The open problem identified in Section 6:
> *"The Vitest passed count reports test files instead of tests, and the PHPUnit count reports `1` instead of the real number of tests. The verdict is unaffected, but those counts are inexact."*

is resolved by the two follow-up commits on branch `fix-vitest-phpunit-counts`:
1. `fix vitest-1: count tests from the Tests line only, parse failed and skipped`
2. `fix phpunit-2: count tests from the documented summary lines`

### Validation Status
- **Vitest**: Now validated against REAL Vitest 1.6.0 runner output (fixture `tests/fixtures/runners/real/vitest_1.6.0_success.txt`), parsing only the `Tests` line rather than double-counting with `Test Files`.
- **Jest 29.7.0 & Mocha 10.8.2**: Validated against real runner output during the Phase 12B dependency-backed runner replay work:

#### Real Output: Jest 29.7.0
```text
PASS ./real.test.js
  √ real repository (3 ms)
Test Suites: 1 passed, 1 total
Tests:       1 passed, 1 total
Snapshots:   0 total
Time:        0.985 s
Ran all test suites.
```

#### Real Output: Mocha 10.8.2
```text
  √ real repository
  1 passing (3ms)
```

- **PHPUnit**: Still documentation-based (per https://docs.phpunit.de/), parsing documented success `OK (<N> tests, <M> assertions)` and issues `Tests: <total>, Assertions: <assertions>...` lines. Real toolchain execution remains unverified in this environment.
