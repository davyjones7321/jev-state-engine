# Phase 12B — Real repositories and replays

Base: `origin/phase12a-review-fixes` (`8c5e19a`).
Branch: `phase12b-real-repositories`.

## 1. What changed

- `tests/real/test_phase12b_real_replays.py`: adds 28 collected real-replay
  cases covering available registry profiles, real zero-test behavior, a real
  pre-existing baseline failure, unparseable clean output, Bun-generated output
  cleanup, and twenty repeated descendant-timeout runs.
- `.github/workflows/phase12b-real.yml`: adds Windows and Ubuntu CI jobs with
  Python, Node, Bun, Go, Rust, .NET, and Java setup, dependency installation,
  and the real-replay suite with skip reporting.
- `docs/phases/phase12b.md`: this report and the verifier transcript.

Existing tests were not edited, deleted, or skipped.

## 2. How it works

Each replay creates a disposable Git repository under pytest's temporary
directory, writes only the tiny repository files needed for its profile, makes
an initial commit, and runs the intended binary. Available toolchains must
pass; unavailable toolchains use `pytest.skip` with a specific reason. The
profile matrix covers every registry profile, while dependency-free templates
are provided for pytest, Node native test, Bun, Go, and Cargo.

The zero-test cases execute real pytest, Node, and Bun commands and check both
the native exit code and the parser's non-positive evidence result. The
baseline replay uses a real failing pytest test in the base commit and changes
unrelated production content; the result must remain a same-baseline PASS.
The unparseable replay runs a clean Python process with no test summary and
checks that it is not counted. The Bun replay creates `.bun-build` in a real
subgoal worktree and verifies the engine's generated-file discard path while
preserving a worker file. The timeout replay runs a real parent/child process
twenty times and independently checks child liveness after each timeout.

## 3. Rule enforcement

| Phase 12B rule | Enforced by test | Prompt-only / limitation |
|---|---|---|
| Real repository pass per registry profile or explicit skip | `test_real_profile_repository_passes_or_skips_with_reason` | Profiles without a dependency-free template are explicitly deferred |
| Zero-test exit-1 handling is downgrade-only | `test_real_zero_test_exit_behavior_is_downgrade_only` | None for pytest, Node, and Bun |
| Pre-existing baseline failure is not a worker regression | `test_real_baseline_failure_is_not_misclassified_as_worker_regression` | None |
| Clean pass with unparseable output is not counted | `test_clean_exit_with_unparseable_output_is_not_counted` | None |
| Generated `.bun-build` output is discarded and worker files survive | `test_real_bun_build_output_is_discarded_from_engine_scope` | Bun toolchain required |
| Timeout descendants are absent after at least 20 repetitions | `test_timeout_descendants_are_absent_after_twenty_real_replays` | Host process cleanup implementation is exercised; CI repeats on Windows and Ubuntu |
| Zero unsafe commits | No repository mutation outside disposable replay repositories | Prompt-only; no independent safety auditor is present |
| Old ledgers load | Existing Phase 11 compatibility tests | Prompt-only for this phase's real repository tests |
| Every unavailable toolchain reports a reason | `pytest -rs` skip output from the profile matrix | CI environment determines which profiles run |
| Only Phase 12B files are changed | Git diff and verifier review | Prompt-only |

## 4. Verification

- Phase 12B real-replay suite: **11 passed, 17 skipped**.
- Collected Phase 12B tests: **28**.
- Full offline suite: **1199 passed, 41 skipped, 2 deselected, 2 warnings**.
- Existing tests were not edited, deleted, or skipped.
- `git diff --check`: passed before verification.

## 5. Skipped tests and reasons

The Phase 12B suite skipped these profiles on the current Windows host:

- Missing tools: `ansible-lint`, `ctest`, `deno`, `dotnet`, `go`, `gradle`,
  `helm`, `mvn`, `mix`, `php`, `ruby`, `swift`, `terraform`, and `xcodebuild`.
- No dependency-free template: `jest`, `mocha`, and `vitest`.

The full offline suite additionally retained its existing skips for unavailable
toolchains, live execution, npm setup timeouts, and platform-incompatible
profiles. No existing test was changed or newly skipped.

## 6. Unrequested changes

- `session-1-v2.md` remains intentionally untracked and was not staged.
- No other unrequested changes.

## 7. Open problems

- Full profile replay coverage depends on CI toolchains and package-manager
  setup; Jest, Mocha, and Vitest need dependency-backed repository templates.
- This phase does not provide an independent safety auditor for unsafe commits;
  that requirement remains prompt/CI governance rather than a measured replay.
- The verifier output includes the repository's pre-existing grep findings for
  `FULL_SUITE_PASSED`; this phase does not alter those production paths.

## 8. Full verifier output

```text
=== Verification Report for base: origin/phase12a-review-fixes ===
Branch: phase12b-real-repositories
Commit: eda4524d0751ae0abfa57d28ef1fc973084f7ebd

--- Diff stat vs origin/phase12a-review-fixes ---
.github/workflows/phase12b-real.yml      |  60 +++++++
docs/phases/phase12b.md                  |  89 ++++++++++
tests/real/test_phase12b_real_replays.py | 288 +++++++++++++++++++++++++++++++
3 files changed, 437 insertions(+)

--- Test Changes ---
Added tests:
  + test_real_profile_repository_passes_or_skips_with_reason
  + test_real_zero_test_exit_behavior_is_downgrade_only
  + test_real_baseline_failure_is_not_misclassified_as_worker_regression
  + test_clean_exit_with_unparseable_output_is_not_counted
  + test_real_bun_build_output_is_discarded_from_engine_scope
  + test_timeout_descendants_are_absent_after_twenty_real_replays

--- Collected Test Counts ---
  tests/real/test_phase12b_real_replays.py: 28 tests collected in 1.09s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:5021:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2449:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:5559:                        test_classification = "FULL_SUITE_PASSED"
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
SKIPPED [1] tests\test_runner_fixtures.py:325: Profile 'xcode-test' not supported on win32 (platforms: ['linux', 'darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:361: terraform not found
SKIPPED [1] tests\test_runner_fixtures.py:361: ansible-lint not found
SKIPPED [1] tests\test_runner_fixtures.py:361: helm not found
1199 passed, 41 skipped, 2 deselected, 2 warnings in 1113.42s (0:18:33)
```
