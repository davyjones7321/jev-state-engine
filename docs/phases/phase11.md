# Phase 11 — Measured-rate and false-rejection metrics

Base: `origin/master` (`6b9d9c88f243b6f2c929d113210bfa9fde9513f0`).
Branch: `phase11-measured-rate-metrics`.

## 1. What changed (files and lines)

- `src/jev/ledger.py`:
  - Line 9: import `sys` for recording the attempt's host platform.
  - Lines 36–172: read-only `phase11_metrics_report` aggregation, grouping,
    time-window validation, strong-evidence rules, raw fractions, assessment
    coverage, and zero-denominator handling.
  - Lines 703–761: additive `attempt_assessments` table, validated independent
    assessment recording API, and SQLite report wrapper.
  - Lines 1182–1185: record the host platform alongside new attempts.
  - Lines 1267–1270: identify the existing `measured_rate_report` as the legacy
    verdict-availability API; direct strong-evidence consumers to the new report.
- `scripts/ledger_report.py`:
  - Lines 5, 16: JSON and metrics imports.
  - Lines 47–49: add `--metrics`, `--since`, and `--until` options.
  - Lines 73–81: emit the aggregate JSON report using a read-only SQLite URI.
- `tests/test_phase11_metrics.py`: 16 new test cases (including parameterized
  cases) covering aggregation, exclusions, independent assessment labels,
  grouping, windows, legacy schemas, safety coverage, and CLI behavior.
- `docs/phases/phase11.md`: this required phase report and verifier transcript.

## 2. How it works

Usage:

```text
python scripts/ledger_report.py --db-path <ledger.db> --metrics
python scripts/ledger_report.py --db-path <ledger.db> --metrics --since 2026-11-01T00:00:00Z --until 2026-12-01T00:00:00Z
```

The metrics report covers all attempts, including rows without mechanical
evidence. It returns totals and joint groups by language, runner profile,
platform, registry version, and the requested time window. Unknown dimensions
are `null`, never the reporting machine's platform for historical attempts.
Command identity supplies profile/registry/platform fallback metadata when the
dedicated mechanical fields are absent. New attempts record `sys.platform`.

Time windows are UTC, inclusive at `since` and exclusive at `until`. Naive
timestamps are interpreted as UTC. Omitted limits mean all history. Invalid
window arguments are errors; attempts with invalid timestamps are excluded
from bounded windows and counted in `excluded_invalid_timestamp_count`.

### Rate definitions

Every rate is `{numerator, denominator, value}`; a zero denominator produces
`value: null`. No rate fabricates observations from missing data.

- `measured_rate_all`: strong measured attempts / all attempts in the window.
- `measured_rate_eligible`: strong measured attempts / eligible attempts.
  Eligibility requires a known mechanical status and excludes outside/unknown
  profiles and `MISSING_TOOL`, `NO_COMMAND`, `ENVIRONMENT_UNAVAILABLE` reasons.
  Unknown mechanical status is reported in `eligibility_unknown_count`.
- Strong measured evidence requires PASS or FAIL with counted/named evidence,
  no mechanically-unverified or verifier-modified flag, and no explicitly
  invalid approval/comparison. All UNMEASURED reasons, outside-profile runs,
  and exit-only passes are excluded. This conservatively excludes exit-only
  passes even when a weak run was intentional; they are reported separately
  in `exit_only_success_count`.
- `false_worker_rejection_rate`: independently labeled incorrect worker
  rejections / all independently labeled worker rejections. Rejection outcomes
  are `mechanical_failure`, `semantic_failure`, `failed`, or `rejected`.
- `false_mechanical_acceptance_rate`: independently labeled incorrect
  mechanical PASS verdicts / all independently labeled mechanical PASS
  verdicts. It is not a semantic correctness claim.
- `tooling_uncertainty_rate`: UNMEASURED, missing mechanical evidence, or
  outside/unknown-profile attempts / all attempts. Each attempt counts once.
- `verifier_modified_rate`: attempts with `mech_verifier_modified` / all
  attempts. Legacy unknown flags are not invented as positive observations.
- `unsafe_commit_count` and `process_leak_count`: sums of explicitly observed
  events attributed to attempts. Missing observations yield `null`, not zero.
  Per-metric assessed-attempt counts accompany the totals, and
  `safety_assessed_attempts` counts attempts assessed for both event types.
  Partial coverage is not evidence that the remaining attempts were safe.

`SqliteLedger.record_attempt_assessment(run_id, subgoal_id, attempt_no, ...)`
accepts reviewed `worker_should_accept`/`mechanical_should_accept` booleans
and nonnegative `unsafe_commit_count`/`process_leak_count` observations.
It validates attempt identity and types; omitted labels preserve earlier
observations. Jev agreement, successful exit codes, and absence of a leak
report are never used as independent ground truth.

The read-only report function tolerates old attempt schemas without migrating
them. Normal ledger initialization adds only the new assessment table. The
legacy measured-rate API retains its historical verdict-availability semantics;
`--metrics` exclusively uses the Phase 11 strong-evidence report.

## 3. Rule enforcement

| Rule | Enforced by named test | Prompt-only |
|---|---|---|
| Strong rates exclude weak passes and outside-profile runs; fractions are raw | `test_strong_rates_exclude_weak_unmeasured_and_outside_profile` | No |
| UNMEASURED never contributes strong evidence | `test_every_unmeasured_reason_stays_out_of_strong_numerator` | No |
| Errors use independent labels, with correct assessed denominators | `test_assessed_errors_and_safety_counts_are_not_inferred_from_jev`, `test_error_rate_denominators_and_partial_safety_coverage` | No |
| Unknown safety observations remain unknown; zero denominators are safe | `test_unknown_assessments_remain_unknown_and_zero_denominators_safe`, `test_no_eligible_attempts_and_window_boundaries` | No |
| Group all required dimensions and filter by time window | `test_group_dimensions_time_window_and_identity_fallback` | No |
| Read old ledgers without changing database bytes | `test_legacy_database_metrics_cli_is_read_only` | No |
| Validate assessment identity, booleans and nonnegative counts | `test_assessments_validate_identity_and_values` | No |
| Show excluded invalid timestamps and tolerate malformed identity metadata | `test_invalid_timestamps_and_malformed_identity_are_visible` | No |
| Real CLI emits JSON and rejects invalid windows | `test_metrics_cli_current_schema_and_invalid_window` | No |
| Assessment labels actually come from an independent review/replay | API validates types, not the truth of supplied labels | Yes |
| Work only on Phase 11; do not merge or promote | Git diff/status review | Yes |

## 4. Verification

- Initial new regression suite: 12 failed before implementation (missing metrics
  API and CLI), confirming the missing behavior.
- Focused metrics plus existing ledger/evidence/compatibility checks:
  **52 passed, 1 warning in 110.92s**. An earlier invocation reached the harness
  time limit; the completed rerun used a longer timeout.
- Existing tests were not edited, deleted, or newly marked skipped.
- `git diff --check`: passed.
- Full offline suite and verifier: raw transcript appended below after execution.

## 5. Unrequested changes

- None. The additive assessment table/API is how Phase 11 records independent
  correctness and safety observations without changing engine authority.

## 6. Open problems

- Historical attempts may lack language/profile/registry/platform metadata;
  the report keeps these dimensions unknown rather than backfilling guesses.
- The engine does not automatically obtain independent correctness labels or
  safety event counts. Until reviewed observations are supplied, false-error
  rates and safety metrics remain unknown or only partially covered. No real-run
  accuracy or production-readiness claim is made from the synthetic tests.
- The old `measured_rate_report` remains a compatibility API that counts weak
  verdict availability; use the new Phase 11 API/CLI for strong coverage.
- Full offline verification may skip unavailable toolchains or fixture setup;
  those skips are recorded by the existing suite, not introduced by this phase.

## 7. Full verifier output

```text
=== Verification Report for base: origin/master ===
Branch: phase11-measured-rate-metrics
Commit: d7532c3809350ca780a3bd047ace3113572d6bf4

--- Diff stat vs origin/master ---
docs/phases/phase11.md        | 135 ++++++++++++++++++++++++++++
scripts/ledger_report.py      |  16 +++-
src/jev/ledger.py             | 205 ++++++++++++++++++++++++++++++++++++++++++
tests/test_phase11_metrics.py | 178 ++++++++++++++++++++++++++++++++++++
4 files changed, 533 insertions(+), 1 deletion(-)

--- Test Changes ---
Added tests:
  + test_strong_rates_exclude_weak_unmeasured_and_outside_profile
  + test_every_unmeasured_reason_stays_out_of_strong_numerator
  + test_assessed_errors_and_safety_counts_are_not_inferred_from_jev
  + test_unknown_assessments_remain_unknown_and_zero_denominators_safe
  + test_group_dimensions_time_window_and_identity_fallback
  + test_legacy_database_metrics_cli_is_read_only
  + test_assessments_validate_identity_and_values
  + test_error_rate_denominators_and_partial_safety_coverage
  + test_no_eligible_attempts_and_window_boundaries
  + test_invalid_timestamps_and_malformed_identity_are_visible
  + test_metrics_cli_current_schema_and_invalid_window

--- Collected Test Counts ---
  tests/test_phase11_metrics.py: 16 tests collected in 1.37s

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
1141 passed, 24 skipped, 2 deselected, 2 warnings in 1308.30s (0:21:48)
```

## Addendum (post-review)

The original pasted verifier output above recorded commit `d7532c3` instead
of the final Phase 11 commit `ceb495d`, and it did not include a pytest summary
section. Therefore, the original full-suite result was never recorded in that
verifier transcript.

Commit `8040562` subsequently excluded attempts with unknown runner profiles
from eligible and strong measured metrics while retaining them as tooling
uncertainty, with a regression test in
`tests/test_phase12a_metrics_review.py`.

Step 3b commit `063f419` added the additive `worker_rejections_total` and
`mechanical_acceptances_total` fields to Phase 11 totals and groups, which are
also present in the CLI JSON because it uses the same report function. The
new `tests/test_phase12a_totals.py` covers assessed-versus-total coverage,
groups, CLI JSON, and empty ledgers.
