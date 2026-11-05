# Phase 12B — Real repositories and replays

Base: `origin/master` (`7a59a2f`).
Branch: `phase12b-real-repositories`.

## 1. What changed

- `tests/real/test_phase12b_real_replays.py`: adds real-replay cases covering available
  registry profiles, dependency-backed templates for Jest, Mocha, and Vitest, dependency-free
  templates for Deno, Go, and Cargo, passing / failing / zero-test variants, real zero-test
  behavior, a real pre-existing baseline failure, unparseable clean output, Bun-generated
  output cleanup, and twenty repeated descendant-timeout runs with POSIX PermissionError
  treated as alive.
- `.github/workflows/phase12b-real.yml`: adds Windows and Ubuntu CI jobs with Python, Node,
  Bun, Deno, Go, Rust, .NET, Java, Ruby, PHP, Elixir, CMake, and Terraform toolchain setups,
  dependency installation, and the real-replay suite with skip reporting.
- `docs/phases/phase12b.md`: this report, captured real outputs, profile coverage breakdown,
  and the verifier transcript.

Existing tests outside `tests/real` were not edited, deleted, or skipped.

## 2. How it works

Each replay creates a disposable Git repository under pytest's temporary directory, writes only
the tiny repository files needed for its profile, makes an initial commit, and runs the intended binary.
Available toolchains must pass; unavailable toolchains use `pytest.skip` with a specific reason.

### Profile execution matrix

The registry defines 21 runner profiles:
- **Run and pass locally on this host (Windows with Node/npm, Python, Cargo)**:
  `pytest`, `node-native-test`, `cargo-test`, `jest`, `mocha`, `vitest` (and `bun-test` when bun is installed).
- **Skip locally on this host due to missing toolchains**:
  `ansible`, `cmake-ctest`, `deno-test`, `dotnet-test`, `go-test`, `gradle-test`, `helm`,
  `maven-surefire`, `mix-test`, `phpunit`, `ruby-rake`, `swiftpm-test`, `terraform`, and `xcode-test`.
- **Run and pass in CI (toolchain + repository template present)**:
  `pytest`, `node-native-test`, `bun-test`, `jest`, `mocha`, `vitest`, `cargo-test`, `go-test`, and `deno-test`.
- **Still skip in CI**:
  1. *Toolchain installed in CI, but no repository template implemented*:
     `dotnet-test` (dotnet installed, no template),
     `maven-surefire` (java/mvn installed, no template),
     `gradle-test` (java/gradle installed, no template),
     `cmake-ctest` (cmake installed, no template),
     `ruby-rake` (ruby installed, no template),
     `phpunit` (php installed, phpunit toolchain/template missing),
     `mix-test` (elixir/mix installed, no template),
     `terraform` (terraform installed, no template).
  2. *No CI toolchain action installed or platform-incompatible*:
     `swiftpm-test` (no swift toolchain action installed),
     `xcode-test` (darwin-only runner required),
     `ansible` (no ansible-lint toolchain action installed),
     `helm` (no helm toolchain action installed).

### Dependency-backed templates (Jest, Mocha, Vitest)

Installed with `npm install --no-audit --no-fund` using exact pinned versions:
- `jest`: 29.7.0 (v29 stable text reporter)
- `mocha`: 10.8.2 (v10 stable spec reporter)
- `vitest`: 1.6.0 (v1 stable reporter)

Each runner is verified with:
1. **Passing test**: parsed positive by `parse_test_counts_summary` with `passed == 1`.
2. **Failing variant**: non-zero exit code, parsed `has_positive_pass is False`.
3. **Zero-test variant**: parsed `has_positive_pass is False`.

### Captured real success outputs

#### Jest (29.7.0)
```text
PASS ./real.test.js
  √ real repository (5 ms)

Test Suites: 1 passed, 1 total
Tests:       1 passed, 1 total
Snapshots:   0 total
Time:        1.76 s
Ran all test suites.
```
*Parser verification*: `passed: 1`, `failed: 0`, `skipped: 0`, `has_positive_pass: True`.

#### Mocha (10.8.2)
```text
  √ real repository

  1 passing (3ms)
```
*Parser verification*: `passed: 1`, `failed: 0`, `skipped: 0`, `has_positive_pass: True`.

#### Vitest (1.6.0)
```text
 RUN  v1.6.0 <TMP>

 ✓ real.test.js  (1 test) 5ms

 Test Files  1 passed (1)
      Tests  1 passed (1)
   Start at  09:52:19
   Duration  922ms (transform 50ms, setup 0ms, collect 42ms, tests 5ms, environment 0ms, prepare 237ms)
```
*Parser verification*: `passed: 1`, `failed: 0`, `skipped: 0`, `has_positive_pass: True`.

## 3. Rule enforcement

| Phase 12B rule | Enforced by test | Prompt-only / limitation |
|---|---|---|
| Real repository pass per registry profile or explicit skip | `test_real_profile_repository_passes_or_skips_with_reason` | Limitation: Profiles without templates or missing toolchains skip with explicit reason |
| Real npm-backed profiles (Jest, Mocha, Vitest) fail/zero variants | `test_real_npm_profile_failing_variant_is_not_positive`, `test_real_npm_profile_zero_test_variant_is_not_positive` | None; verified against pinned npm packages |
| Real dependency-free profiles (Deno, Go, Cargo) fail/zero variants | `test_real_dependency_free_profile_failing_variant_is_not_positive`, `test_real_dependency_free_profile_zero_test_variant_is_not_positive` | Limitation: Requires respective toolchain installed |
| Zero-test exit-1 handling is downgrade-only | `test_real_zero_test_exit_behavior_is_downgrade_only` | Tested for pytest, node-native-test, and bun-test |
| Pre-existing baseline failure is not a worker regression | `test_real_baseline_failure_is_not_misclassified_as_worker_regression` | None |
| Clean pass with unparseable output is not counted | `test_clean_exit_with_unparseable_output_is_not_counted` | None |
| Generated `.bun-build` output is discarded and worker files survive | `test_real_bun_build_output_is_discarded_from_engine_scope` | Limitation: Bun toolchain required |
| Timeout descendants are absent after at least 20 repetitions | `test_timeout_descendants_are_absent_after_twenty_real_replays` | Host process cleanup exercised; POSIX treats PermissionError as alive |
| Zero unsafe commits | Verification review | Limitation: Prompt-only convention; engine does not mutate repository outside disposable worktrees |
| Old ledgers load | `tests/test_ledger_phase11.py` compatibility tests | Limitation: Covered by Phase 11 offline tests, not in `tests/real` |
| Every unavailable toolchain reports a reason | `test_real_profile_repository_passes_or_skips_with_reason` | `pytest.skip` reports missing toolchain name or missing template |
| Only Phase 12B files are changed | `git diff --check` and `scripts/verify_phase.py origin/master` | None |

## 4. Verification

- Phase 12B real-replay suite: **22 passed, 18 skipped**.
- Collected Phase 12B tests: **40**.
- Full offline suite: **1270 passed, 42 skipped, 2 deselected, 2 warnings**.
- Existing tests outside `tests/real` were not edited, deleted, or skipped.
- `git diff --check`: passed.

## 5. Skipped tests and reasons

The Phase 12B suite skipped these profiles on the current Windows host:
- Missing tools: `ansible-lint`, `ctest`, `deno`, `dotnet`, `go`, `gradle`, `helm`,
  `mvn`, `mix`, `php`, `ruby`, `swift`, `terraform`, and `xcodebuild`.

The full offline suite additionally retained its existing skips for unavailable
toolchains, live execution, npm setup timeouts, and platform-incompatible
profiles. No existing test was changed or newly skipped.

## 6. Unrequested changes

- `session-1-v2.md` remains intentionally untracked and was not staged.
- No other unrequested changes.

## 7. Open problems

- Profiles with no CI toolchain (e.g. Swift, Xcode, Ansible, Helm) still have no real-output verification in CI.
- Profiles with CI toolchains installed but no repository template (Dotnet, Maven, Gradle, CMake, Ruby, PHPUnit, Mix, Terraform) still skip real-output verification in CI.
- Independent safety auditor for unsafe commits remains prompt/CI governance rather than a measured replay.

## 8. Full verifier output

```text
=== Verification Report for base: origin/master ===
Branch: phase12b-real-repositories
Commit: ccbf76089f8c55a5af04a1ed4146abf4f2577c83

--- Diff stat vs origin/master ---
 .github/workflows/phase12b-real.yml      |  89 ++++++
 docs/phases/phase12b.md                  | 151 +++++++++
 tests/real/test_phase12b_real_replays.py | 505 +++++++++++++++++++++++++++++++
 3 files changed, 745 insertions(+)

--- Test Changes ---
Added tests:
  + test_real_profile_repository_passes_or_skips_with_reason
  + test_real_zero_test_exit_behavior_is_downgrade_only
  + test_real_baseline_failure_is_not_misclassified_as_worker_regression
  + test_clean_exit_with_unparseable_output_is_not_counted
  + test_real_bun_build_output_is_discarded_from_engine_scope
  + test_timeout_descendants_are_absent_after_twenty_real_replays
  + test_real_npm_profile_failing_variant_is_not_positive
  + test_real_npm_profile_zero_test_variant_is_not_positive
  + test_real_dependency_free_profile_failing_variant_is_not_positive
  + test_real_dependency_free_profile_zero_test_variant_is_not_positive

--- Collected Test Counts ---
  tests/real/test_phase12b_real_replays.py: 40 tests collected in 1.01s

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
1270 passed, 42 skipped, 2 deselected, 2 warnings in 1350.61s (0:22:30)
```
