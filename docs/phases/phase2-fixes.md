# Phase 2 Fixes Report: Behavior Preservation & Runner Fixtures (2A + 2B)

## 1. What Changed

### Part A: Behavior Preservation (Phase 2A Fixes)
1. **Precedence Field for Runner Profiles**:
   - Added an integer `"precedence"` field to all 21 profiles in `src/jev/runner_profiles.json` and validated in `RunnerProfile` schema (`src/jev/runner_registry.py`).
   - Derived directly from the old cascade order:
     - `go-test`: 2
     - `cargo-test`: 3
     - `maven-surefire`: 4
     - `gradle-test`: 5
     - `pytest`: 6
     - `terraform`: 7
     - `ansible`: 8
     - `helm`: 9
     - All 13 other profiles: 100
   - Used strictly as a tie-breaker for layouts the old cascade resolved (where all matching profiles have `precedence <= 9`).
2. **Explicit Ambiguity Resolution & Evidence Recording**:
   - When precedence breaks a tie, `resolve_from_registry` records the other matching profile IDs in `also_matched: List[str]` and sets `ambiguity_resolved_by = "precedence"`.
   - Resolution is never silent: both `also_matched` and `ambiguity_resolved_by` are propagated through `ResolvedTestCommand`, `Workspace.last_test_run`, `cmd_identity`, and `MechanicalEvidence`.
   - New ambiguities (e.g. multiple JS runner configs or new runner configs with `precedence == 100`) continue to return `NO_COMMAND` with candidate profile IDs listed.
3. **Differential Resolution Testing**:
   - Added extensive differential tests comparing `resolve_test_command` against the old cascade resolver across a table of layouts (single markers, `package.json` variants, and the four polyglot pairs).
   - Separately listed and asserted the 7 intentional new-coverage cases.

### Part B: Fixtures (Phase 2B Fixes)
4. **Partitioned Platform Fixture Layout**:
   - Reorganized fixture storage to `tests/fixtures/runners/<sys.platform>/<id>.json`.
   - Moved all committed fixture files to `tests/fixtures/runners/win32/`.
   - Updated `tests/test_runner_fixtures.py` to test against the host platform fixture directory, cleanly reporting skips when fixtures or toolchains are not available.
5. **Fixture Normalization**:
   - Normalized all fixture files to remove usernames, home directories, temporary directories, timings, durations, and variable hashes, replacing them with `<PYTHON>`, `<HOME>`, `<TMP>`, `<USER>`, `<DUR>`, `<HASH>`.
   - Added `test_fixtures_are_normalized` to mechanically assert that `"Users\\"`, `"/home/"`, or the current username never appear anywhere in any committed fixture file.
6. **Fixture Capture and CI Verification**:
   - Added `scripts/capture_runner_fixtures.py` to regenerate normalized fixtures and support `--verify-only` mode.
   - Updated `.github/workflows/ci.yml` on `ubuntu-latest` and `windows-latest` to capture fixtures, upload them as workflow artifacts (without committing from CI), and fail the job if live exit codes differ from committed fixtures of the same platform.
7. **Registry Zero-Test Exit Behavior**:
   - Set `zero_test_exit_behavior` in `src/jev/runner_profiles.json` where real captures prove it: `node-native-test = 0`, `cargo-test = 0`, `pytest = 5`, `bun-test = 1`.
   - Left unmeasured profiles as unknown (`null` / `None`).

---

## 2. Layout Resolution Comparison: Registry vs Old Resolver

Every layout whose resolution differs from the old resolver (and why):

| Layout / Markers | Old Resolver Behavior | Registry Behavior | Why It Differs | Test Name |
| :--- | :--- | :--- | :--- | :--- |
| **`jest.config.js` only** | `NO_COMMAND` (no test framework) | `["npx", "jest"]` (`jest`) | **Intentional New Coverage**: Modern JS projects with Jest config without a `test` script in `package.json`. | `test_seven_intentional_new_coverage_cases` |
| **`vitest.config.ts` only** | `NO_COMMAND` (no test framework) | `["npx", "vitest", "run"]` (`vitest`) | **Intentional New Coverage**: Modern Vite projects with Vitest config without a `test` script in `package.json`. | `test_seven_intentional_new_coverage_cases` |
| **`.NET` (`App.csproj`)** | `NO_COMMAND` (no test framework) | `["dotnet", "test"]` (`dotnet-test`) | **Intentional New Coverage**: .NET SDK test runner recognized from project files. | `test_seven_intentional_new_coverage_cases` |
| **`Elixir` (`mix.exs`)** | `NO_COMMAND` (no test framework) | `["mix", "test"]` (`mix-test`) | **Intentional New Coverage**: Elixir Mix test runner recognized from manifest. | `test_seven_intentional_new_coverage_cases` |
| **`Ruby` (`Rakefile`)** | `NO_COMMAND` (no test framework) | `["bundle", "exec", "rake", "test"]` (`ruby-rake`) | **Intentional New Coverage**: Ruby projects with test Rakefile. | `test_seven_intentional_new_coverage_cases` |
| **`PHP` (`phpunit.xml`)** | `NO_COMMAND` (no test framework) | `["vendor/bin/phpunit"]` (`phpunit`) | **Intentional New Coverage**: PHPUnit XML configuration recognized. | `test_seven_intentional_new_coverage_cases` |
| **`Deno` (`deno.json`)** | `NO_COMMAND` (no test framework) | `["deno", "test"]` (`deno-test`) | **Intentional New Coverage**: Deno configuration recognized. | `test_seven_intentional_new_coverage_cases` |
| **Polyglot New Ambiguity (e.g. `jest.config.js` + `vitest.config.ts`)** | `NO_COMMAND` (unrecognized) | `NO_COMMAND` (`source="ambiguous"`, candidates: `["jest", "vitest"]`) | **Explicit Ambiguity**: Multiple JS runner configs detected without precedence; fail-closed returning candidates. | `test_ambiguity_returns_no_command_for_unresolved_new_profiles` |
| **New runner + Old runner outside old cascade (e.g. `go.mod` + `jest.config.js`)** | `["go", "test", "./..."]` (old cascade did not see `jest`) | `NO_COMMAND` (`source="ambiguous"`, candidates: `["go-test", "jest"]`) | **Explicit Ambiguity**: Old cascade was blind to `jest.config.js`; registry detects both and safely flags polyglot ambiguity. | `test_ambiguity_returns_no_command_for_unresolved_new_profiles` |

### Layouts with Preserved Behavior (Same Command via Precedence Tie-Breaking)

| Layout | Old Resolver Command | Registry Command | Precedence Tie-Breaker | Evidence Recorded |
| :--- | :--- | :--- | :--- | :--- |
| `go.mod` + `Cargo.toml` | `["go", "test", "./..."]` | `["go", "test", "./..."]` | `go-test` (2) < `cargo-test` (3) | `also_matched=["cargo-test"]`, `ambiguity_resolved_by="precedence"` |
| `pyproject.toml` (pytest) + `go.mod` | `["go", "test", "./..."]` | `["go", "test", "./..."]` | `go-test` (2) < `pytest` (6) | `also_matched=["pytest"]`, `ambiguity_resolved_by="precedence"` |
| `pom.xml` + `build.gradle` | `["mvn", "test"]` (or mvnw) | `["mvn", "test"]` (or mvnw) | `maven-surefire` (4) < `gradle-test` (5) | `also_matched=["gradle-test"]`, `ambiguity_resolved_by="precedence"` |
| `main.tf` + pytest markers | `[python, "-m", "pytest", "-q"]` | `[python, "-m", "pytest", "-q"]` | `pytest` (6) < `terraform` (7) | `also_matched=["terraform"]`, `ambiguity_resolved_by="precedence"` |
| `go.mod` + `package.json` (with test script) | `["npm", "test"]` | `["npm", "test"]` | `package.json` script precedence | None (unambiguous script precedence) |
| `go.mod` + `package.json` (without test script) | `["go", "test", "./..."]` | `["go", "test", "./..."]` | Single profile match (`go-test`) | None |
| `pytest.ini` + `package.json` (with test script) | `["npm", "test"]` | `["npm", "test"]` | `package.json` script precedence | None |
| `pytest.ini` + `package.json` (without test script) | `[python, "-m", "pytest", "-q"]` | `[python, "-m", "pytest", "-q"]` | Single profile match (`pytest`) | None |

---

## 3. Captured vs Skipped Profiles per Platform

### Platform: `win32`
Directory: `tests/fixtures/runners/win32/`

- **Captured Profiles (4)**:
  1. `pytest`: Python 3.13.15, pytest 9.1.1. Captured 5 cases (`pass: 0`, `fail: 1`, `zero_tests: 5`, `filter_matches_nothing: 5`, `all_skipped: 0`).
  2. `node-native-test`: Node.js v20.18.0. Captured 5 cases (`pass: 0`, `fail: 1`, `zero_tests: 0`, `filter_matches_nothing: 0`, `all_skipped: 0`).
  3. `bun-test`: Bun 1.3.1. Captured 5 cases (`pass: 0`, `fail: 1`, `zero_tests: 1`, `filter_matches_nothing: 1`, `all_skipped: 0`).
  4. `cargo-test`: Cargo 1.83.0 / rustc 1.83.0. Captured 5 cases (`pass: 0`, `fail: 101`, `zero_tests: 0`, `filter_matches_nothing: 0`, `all_skipped: 0`).

- **Skipped Profiles (17)**:
  1. `jest`: `"npm install timed out for jest"`
  2. `mocha`: `"npm install timed out for mocha"`
  3. `vitest`: `"npm install timed out for vitest"`
  4. `deno-test`: `"deno not found"`
  5. `go-test`: `"go not found"`
  6. `maven-surefire`: `"mvn not found"`
  7. `gradle-test`: `"gradle not found"`
  8. `dotnet-test`: `"dotnet not found"`
  9. `cmake-ctest`: `"ctest not found"`
  10. `ruby-rake`: `"ruby not found"`
  11. `phpunit`: `"php not found"`
  12. `swiftpm-test`: `"Profile 'swiftpm-test' not supported on win32 (platforms: ['linux', 'darwin'])"`
  13. `mix-test`: `"mix not found"`
  14. `xcode-test`: `"Profile 'xcode-test' not supported on win32 (platforms: ['darwin'])"`
  15. `terraform`: `"terraform not found"`
  16. `ansible`: `"ansible-lint not found"`
  17. `helm`: `"helm not found"`

---

## 4. Rule-to-Test Table

| Rule | Status | Test Name |
| :--- | :--- | :--- |
| Precedence tie-breaking for 4 old cascade layouts | MECHANICALLY ENFORCED | `test_precedence_tie_breaking_for_four_layouts` |
| `also_matched` and `ambiguity_resolved_by="precedence"` recorded in evidence and command identity | MECHANICALLY ENFORCED | `test_evidence_records_precedence_tie_breaking` |
| New ambiguities outside old cascade return NO_COMMAND with candidates | MECHANICALLY ENFORCED | `test_ambiguity_returns_no_command_for_unresolved_new_profiles` |
| Zero matches returns NO_COMMAND | MECHANICALLY ENFORCED | `test_zero_matches_returns_no_command` |
| Differential test across table of layouts (single markers, package.json variants, polyglot pairs) | MECHANICALLY ENFORCED | `test_differential_resolution_across_layouts_table[18 layouts]` |
| 7 intentional new-coverage cases resolved by registry while old cascade returned None | MECHANICALLY ENFORCED | `test_seven_intentional_new_coverage_cases[7 cases]` |
| Fixture organization by platform (`tests/fixtures/runners/<sys.platform>/<id>.json`) | MECHANICALLY ENFORCED | `test_all_registry_profiles_have_fixture_file` |
| Fixture normalization: no `Users\`, `/home/`, or username | MECHANICALLY ENFORCED | `test_fixtures_are_normalized` |
| Fixture zero-test exit codes match registry behavior | MECHANICALLY ENFORCED | `test_fixture_zero_test_exit_matches_registry` |
| Known measured values in fixtures confirmed | MECHANICALLY ENFORCED | `test_canonical_known_measured_values_in_fixtures` |
| Live runner execution or skip with specific reason in pytest summary | MECHANICALLY ENFORCED | `test_live_runner_execution_or_skip[21 profiles]` |
| CI captures fixtures, uploads as artifact, fails on live exit code mismatch | MECHANICALLY ENFORCED | `.github/workflows/ci.yml` (`scripts/capture_runner_fixtures.py --verify-only`) |
| Unmeasured profiles left as unknown in registry | MECHANICALLY ENFORCED | `test_fixture_zero_test_exit_matches_registry` |

---

## 5. Existing Tests Changed or Touched

- Only allowed test files were modified:
  - `tests/test_runner_registry.py`: added precedence tie-breaker tests, differential layout tests, and new coverage tests.
  - `tests/test_runner_fixtures.py`: updated platform fixture loader and added normalization check.
- NO other existing tests in the repository were modified.

---

## 6. Unrequested Changes

- **NONE**. Strictly modified only allowed files:
  - `src/jev/runner_registry.py`
  - `src/jev/runner_profiles.json`
  - `src/jev/workspace.py` (resolution only)
  - `tests/test_runner_registry.py`
  - `tests/test_runner_fixtures.py`
  - `tests/fixtures/runners/**`
  - `scripts/capture_runner_fixtures.py` (new)
  - `.github/workflows/ci.yml`
  - `docs/phases/phase2-fixes.md`

---

## 7. Phase Verification Script Output

```text
=== Verification Report for base: origin/phase2b-real-fixtures ===
Branch: phase2-fixes
Commit: 5739848e9da521d5a6938698ba9b54ebf8475c54

--- Diff stat vs origin/phase2b-real-fixtures ---
.github/workflows/ci.yml                           |  14 +
 docs/phases/phase2-fixes.md                        | 152 +++++
 scripts/capture_runner_fixtures.py                 | 650 ++++++++++++++++++++
 src/jev/runner_profiles.json                       | 655 ++++++++++++++++-----
 src/jev/runner_registry.py                         | 135 ++---
 src/jev/workspace.py                               |  12 +
 tests/fixtures/runners/bun-test.json               |  68 ---
 tests/fixtures/runners/cargo-test.json             |  65 --
 tests/fixtures/runners/node-native-test.json       |  65 --
 tests/fixtures/runners/{ => win32}/ansible.json    |   8 +-
 tests/fixtures/runners/win32/bun-test.json         |  68 +++
 tests/fixtures/runners/win32/cargo-test.json       |  65 ++
 .../fixtures/runners/{ => win32}/cmake-ctest.json  |   6 +-
 tests/fixtures/runners/{ => win32}/deno-test.json  |   7 +-
 .../fixtures/runners/{ => win32}/dotnet-test.json  |   6 +-
 tests/fixtures/runners/{ => win32}/go-test.json    |   7 +-
 .../fixtures/runners/{ => win32}/gradle-test.json  |   6 +-
 tests/fixtures/runners/{ => win32}/helm.json       |   6 +-
 tests/fixtures/runners/{ => win32}/jest.json       |   2 +-
 .../runners/{ => win32}/maven-surefire.json        |   8 +-
 tests/fixtures/runners/{ => win32}/mix-test.json   |   6 +-
 tests/fixtures/runners/{ => win32}/mocha.json      |   2 +-
 tests/fixtures/runners/win32/node-native-test.json |  65 ++
 tests/fixtures/runners/{ => win32}/phpunit.json    |   6 +-
 tests/fixtures/runners/{ => win32}/pytest.json     |  20 +-
 tests/fixtures/runners/{ => win32}/ruby-rake.json  |   6 +-
 .../fixtures/runners/{ => win32}/swiftpm-test.json |   6 +-
 tests/fixtures/runners/{ => win32}/terraform.json  |   6 +-
 tests/fixtures/runners/{ => win32}/vitest.json     |   2 +-
 tests/fixtures/runners/{ => win32}/xcode-test.json |   6 +-
 tests/test_runner_fixtures.py                      | 152 +++--
 tests/test_runner_registry.py                      | 335 ++++++++++-
 32 files changed, 2051 insertions(+), 566 deletions(-)

--- Test Changes ---
Added tests:
  + test_fixtures_are_normalized
  + test_precedence_tie_breaking_for_four_layouts
  + test_evidence_records_precedence_tie_breaking
  + test_ambiguity_returns_no_command_for_unresolved_new_profiles
  + test_differential_resolution_across_layouts_table
  + test_seven_intentional_new_coverage_cases

--- Collected Test Counts ---
  tests/test_runner_fixtures.py: 28 tests collected in 0.85s
  tests/test_runner_registry.py: 73 tests collected in 0.80s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4772:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1822:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3756:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1172:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1205:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1236:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
SKIPPED [1] tests\test_runner_fixtures.py:327: gradle not found
SKIPPED [1] tests\test_runner_fixtures.py:327: dotnet not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ctest not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ruby not found
SKIPPED [1] tests\test_runner_fixtures.py:327: php not found
SKIPPED [1] tests\test_runner_fixtures.py:291: Profile 'swiftpm-test' not supported on win32 (platforms: ['linux', 'darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:327: mix not found
SKIPPED [1] tests\test_runner_fixtures.py:291: Profile 'xcode-test' not supported on win32 (platforms: ['darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:327: terraform not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ansible-lint not found
SKIPPED [1] tests\test_runner_fixtures.py:327: helm not found
851 passed, 18 skipped, 2 deselected, 2 warnings in 676.91s (0:11:16)
```
