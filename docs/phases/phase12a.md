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
