# Phase 12A — Conformance suite (fake tools)

Base: `origin/phase11-measured-rate-metrics` (`ceb495d`).
Branch: `phase12a-conformance-suite`.

## 1. What changed (files and lines)

- `tests/conformance/fake_runner_script.txt:1-86`: adds a tiny JSON-scripted runner.
  It emits configured stdout/stderr, returns configured exit codes, supports
  per-kind candidate/base/scratch/rerun recipes, records invocations, sleeps
  for timeout cases, and can create a child process for descendant cleanup.
- `tests/conformance/test_fake_runner_conformance.py:1-434`: adds 17 collected
  conformance tests (15 test functions, including a six-case parameterization)
  covering the full Phase 12A scenario list and the output-scrambling property.
- `docs/phases/phase12a.md`: this required phase report and the raw verifier
  transcript appended below.

Existing tests were not edited, deleted, or skipped. No existing-test exception
list is therefore applicable.

## 2. How it works

The tests create temporary Git repositories with a committed baseline, then
use `Workspace` and a patched registry command that invokes `fake_runner.py`.
Each test writes a JSON recipe through `JEV_FAKE_RUNNER_CONFIG`. The fake tool
does not decide whether a run passed: it only supplies deterministic process
behavior. Jev's existing parser, preflight, comparison, fingerprint, target,
provenance, cache, and evidence code produces the verdict under test.

Recipe selection is based on the temporary worktree name (`candidate`, `base`,
`scratch`, or `candidate_rerun`). A list recipe is consumed sequentially using
a state file, which makes flaky and repeated-fingerprint cases reproducible.
The timeout case launches a real child process and asserts that the process
runner reports timeout, zero remaining descendants, and that the child PID is
gone. The tests also exercise missing-tool and untrusted/no-command paths,
malformed output, target widening, lockfile and command identity changes,
shared/generated test surface changes, verifier edits, engine/worker collision,
generated-artifact cleanup, stale cache identity, and unsupported profiles.

The property-style scrambling check holds exit code fixed, compares a positive
count summary with scrambled output, and asserts that scrambling cannot create
positive measured evidence. Fingerprint checks assert that normalized repeated
failures remain stable while changed failure identities differ.

## 3. Rule enforcement

| Phase 12A rule | Enforced by test | Prompt-only / limitation |
|---|---|---|
| Counted pass, named pass, exit-only pass | `test_fake_runner_basic_verdicts`, `test_named_pass_and_candidate_fail_base_pass` | None |
| Empty output exit 0, zero tests exit 0, all skipped, zero tests exit 1 | `test_fake_runner_basic_verdicts` | None |
| Candidate fail/base pass, fail/fail, base unavailable | `test_named_pass_and_candidate_fail_base_pass`, `test_fail_fail_and_base_unavailable_are_not_false_strikes` | None |
| New target absent at base | `test_new_target_absent_at_base_and_modified_existing_test` | None |
| Modified existing test | `test_new_target_absent_at_base_and_modified_existing_test` | None |
| Heuristic target failure then widened pass | `test_heuristic_target_failure_widens_once_without_a_strike` | None |
| Flaky candidate and flaky base | `test_flaky_candidate_and_flaky_base_are_unmeasured` | None |
| Repeated and changed fingerprints | `test_repeated_and_changed_failure_fingerprints_are_distinct_conformance_cases` | None |
| Timeout with descendants | `test_timeout_kills_descendants_and_missing_or_absent_commands_are_unmeasured` | OS-specific cleanup implementation is exercised on this host; CI must repeat it on each OS |
| Missing tool and no command | `test_timeout_kills_descendants_and_missing_or_absent_commands_are_unmeasured` | None |
| Malformed output | `test_malformed_output_scrambling_and_fingerprints_are_conservative` | None |
| Scrambled output with unchanged exit code is downgrade-only | `test_malformed_output_scrambling_and_fingerprints_are_conservative` | None |
| Lockfile identity change | `test_lockfile_and_command_identity_changes_invalidate_comparison` | None |
| Command identity change | `test_lockfile_and_command_identity_changes_invalidate_comparison` | None |
| Test-surface change | `test_verifier_modification_test_surface_collision_and_generated_mutation` | None |
| Verifier modification | `test_verifier_modification_test_surface_collision_and_generated_mutation` | None |
| Worker/engine collision | `test_verifier_modification_test_surface_collision_and_generated_mutation` | None |
| Generated file mutation and generated-artifact cleanup | `test_verifier_modification_test_surface_collision_and_generated_mutation` | None |
| Stale cache identity | `test_stale_cache_identity_and_unsupported_profile` | None |
| Unsupported profile | `test_stale_cache_identity_and_unsupported_profile` | None |
| All required cases use a deterministic fake tool, not a live service | All tests in `tests/conformance/test_fake_runner_conformance.py` | The fake tool cannot establish behavior of an unavailable real toolchain; real repositories belong to Phase 12B |
| Do only Phase 12A and leave promotion/push/merge to later work | Branch/status review and allowed-path diff | Prompt-only |

## 4. Unrequested changes

- None.

## 5. Open problems

- This phase validates the existing engine against deterministic process and
  output recipes. It does not replace Phase 12B's real repositories, CI
  toolchains, cross-platform replay, or the required repeated timeout runs.
- The descendant assertion exercises the host's process cleanup path. A Linux
  run would exercise the process-group branch rather than Windows Job Objects;
  both still need CI coverage in Phase 12B.
- The fake runner intentionally does not emulate every runner's native output
  grammar. Profile-specific parser captures and real tool behavior remain
  outside Phase 12A.

## 6. Verification

- Initial conformance run before the final additions: 15 passed.
- Final conformance run: **17 passed in 46.91s**.
- `git diff --check`: passed before verification.
- Full offline verification and its complete raw output are appended below.

## 7. Full verifier output

```text
=== Verification Report for base: origin/phase12a-conformance-suite ===
Branch: phase12a-review-fixes
Commit: 8040562238ed646124bd8b64abd9d092214143f9

--- Diff stat vs origin/phase12a-conformance-suite ---
src/jev/ledger.py                                 |   8 +-
src/jev/runner_registry.py                        | 288 +++++++++++++---------
tests/conformance/test_fake_runner_conformance.py | 154 +++++++++++-
tests/test_phase12a_metrics_review.py             |  26 ++
4 files changed, 351 insertions(+), 125 deletions(-)

--- Test Changes ---
Added tests:
  + test_scrambled_output_many_fixed_seed_variants_only_preserve_positive_summary
  + test_summary_parser_rejects_noise_phrases_for_every_registry_profile
  + test_summary_parser_accepts_anchored_true_summary_per_profile
  + test_profiles_without_safe_count_shape_remain_exit_only
  + test_truncated_pytest_count_remains_positive_when_count_is_intact
  + test_unknown_runner_profile_is_not_strong_measured_evidence

--- Collected Test Counts ---
  tests/conformance/test_fake_runner_conformance.py: 46 tests collected in 1.12s
  tests/test_phase12a_metrics_review.py: 1 test collected in 0.71s

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
1188 passed, 24 skipped, 2 deselected, 2 warnings in 1007.93s (0:16:47)
```
=== Verification Report for base: origin/phase11-measured-rate-metrics ===
Branch: phase12a-conformance-suite
Commit: 40fb6a8b7d8b752f7eccd1fe08353508e50136a5

--- Diff stat vs origin/phase11-measured-rate-metrics ---
docs/phases/phase12a.md                           |  97 +++++
tests/conformance/fake_runner_script.txt          |  86 +++++
tests/conformance/test_fake_runner_conformance.py | 434 ++++++++++++++++++++++
3 files changed, 617 insertions(+)

--- Test Changes ---
Added tests:
  + test_fake_runner_basic_verdicts
  + test_named_pass_and_candidate_fail_base_pass
  + test_fail_fail_and_base_unavailable_are_not_false_strikes
  + test_new_target_absent_at_base_and_modified_existing_test
  + test_flaky_candidate_and_flaky_base_are_unmeasured
  + test_repeated_and_changed_failure_fingerprints_are_distinct_conformance_cases
  + test_timeout_kills_descendants_and_missing_or_absent_commands_are_unmeasured
  + test_malformed_output_scrambling_and_fingerprints_are_conservative
  + test_heuristic_target_failure_widens_once_without_a_strike
  + test_lockfile_and_command_identity_changes_invalidate_comparison
  + test_verifier_modification_test_surface_collision_and_generated_mutation
  + test_stale_cache_identity_and_unsupported_profile

--- Collected Test Counts ---
  tests/conformance/test_fake_runner_conformance.py: 17 tests collected in 1.74s

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
1158 passed, 24 skipped, 2 deselected, 2 warnings in 1179.08s (0:19:39)
```

## 8. Review-fix addendum

This addendum records the review fixes completed on `phase12a-review-fixes`.

### Step 1b — profile-anchored summary parsing

- Commit: `b1d76b9` (`fix 12a-1b: summary lines anchored per runner profile`)
- `src/jev/runner_registry.py`: positive count evidence now requires a
  profile-specific summary-line grammar. Narrative phrases containing `N passed`
  remain exit-only. Existing accepted summaries remain positive; documented
  Mocha, Vitest, Deno, .NET, PHPUnit, Ruby/Minitest, SwiftPM/Xcode, Ansible,
  and Helm formats are additive. Terraform remains exit-code-only because no
  safe test-count summary is defined.
- `tests/conformance/test_fake_runner_conformance.py`: added table-driven noise
  rejection, per-profile true-summary checks, unsupported-profile checks, and
  the truncated pytest summary case.
- Pre-fix evidence: the new checks produced **14 failures**, including false
  positives for narrative `N passed` lines.
- Post-fix checks: **25 passed, 21 deselected**; existing parser, fixture, and
  Phase 6 checks: **106 passed, 17 skipped**.
- Full offline suite: **1187 passed, 24 skipped, 2 deselected, 2 warnings**.

### Step 1c — documented real success formats

- New regression table: `tests/test_phase12a_parser_real_formats.py` covers
  every registry profile and preserves the positive result of every existing
  Step 1b sample while adding documented real formats.
- Before the parser fix, the new table produced **8 failures**: Ansible,
  Deno, .NET, Helm, Mocha, PHPUnit, Ruby/Minitest, and Vitest.
- After the fix: **21 passed** for the new table; existing parser, fixture,
  conformance, and Phase 6 checks: **152 passed, 17 skipped**.
- Full offline suite: **1209 passed, 24 skipped, 2 deselected, 2 warnings**.
- The one-time old-module comparison loaded `runner_registry.py` from
  `git show b1d76b9~1` outside the repository; that temporary module was not
  retained.
- The formats below are from tool documentation or existing captured fixture
  conventions, not new live captures. Seventeen of the 21 registry profiles
  still have no captured real output in the repository and remain unverified
  against real execution.

| Profile | Old positive | New positive | Old counts | New counts |
|---|---:|---:|---:|---:|
| pytest | true | true | 3/0/0 | 3/0/0 |
| node-native-test | true | true | 3/0/0 | 3/0/0 |
| jest | true | true | 1/0/0 | 3/0/0 |
| mocha | true | true | 3/0/0 | 3/0/0 |
| vitest | true | true | 1/0/0 | 2/0/0 |
| bun-test | true | true | 3/0/0 | 3/0/0 |
| deno-test | true | true | 3/0/0 | 3/0/0 |
| go-test | true | true | 1/0/0 | 1/0/0 |
| cargo-test | true | true | 3/0/0 | 3/0/0 |
| maven-surefire | true | true | 3/0/0 | 3/0/0 |
| gradle-test | false | true | 0/0/0 | 3/0/0 |
| dotnet-test | true | true | 3/0/0 | 6/0/0 |
| cmake-ctest | false | true | 0/0/0 | 3/0/0 |
| ruby-rake | false | true | 0/0/0 | 3/0/0 |
| phpunit | false | true | 0/0/0 | 1/0/0 |
| swiftpm-test | false | true | 0/0/0 | 3/0/0 |
| mix-test | false | true | 0/0/0 | 3/0/0 |
| xcode-test | false | true | 0/0/0 | 3/0/0 |
| terraform | false | false | 0/0/0 | 0/0/0 |
| ansible | false | true | 0/0/0 | 3/0/0 |
| helm | false | true | 0/0/0 | 1/0/0 |

### Step 2 — held for owner review

Step 2 was not started. Its required replacement of the existing timeout test's
child-liveness assertion conflicts with the standing rule forbidding edits to
existing tests. Production timeout code was not changed.

### Step 3 — unknown profiles excluded from metrics

- Commit: `8040562` (`fix 12a-3: exclude unknown profiles from metrics`)
- `src/jev/ledger.py`: Phase 11 metrics now treat a missing runner profile as
  outside/unknown-profile evidence, excluding it from strong measured and
  eligible numerators while counting it as tooling uncertainty.
- `tests/test_phase12a_metrics_review.py`: added a regression test that failed
  before the fix (`measured_rate_all` was `1/1`) and passes after the fix.
- Focused checks: **19 passed, 9 deselected**.
- Full offline suite: **1188 passed, 24 skipped, 2 deselected, 2 warnings**.

### Step 4 — documentation and verification

This report is updated with the review-fix history, exact commits, test results,
the Step 2 hold, and the raw verifier transcript below. Existing tests were not
edited, deleted, or skipped. The intentionally untracked `session-1-v2.md`
remains outside the commits.

## 9. Review-fix verifier output

### Correction

The Step 2 status above is historical as recorded at commit `8c5e19a`.
After review, Step 2 was completed additively in `aa78e9e` by adding a new
cross-platform child-liveness test; the existing timeout test was left
untouched. Step 1c recognizes the documented additive formats listed above,
but those formats are not verified against captured real output for 17 of the
21 profiles; the table is parser regression evidence, not a live-tool claim.

```text
