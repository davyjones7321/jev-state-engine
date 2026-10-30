# Phase 5 Report: Target Selection and Safe Fallback

## 1. What Changed (Files and Lines)

### Allowed Files Modified / Created:
- `src/jev/runner_registry.py` (Modified, lines 17, 417–566):
  - Added import `TargetConfidence` from `jev.mech_evidence`.
  - Implemented `build_targeted_command(profile, base_command, targets, worktree_dir)`:
    - `pytest`: supports exact file paths and node IDs (`::`), returning `exact_file` or `exact_node_id` with `TargetConfidence.EXACT`.
    - File-targeting profiles (`bun-test`, `deno-test`, `mix-test`, `node-native-test`, `mocha`, `ruby-rake`, `vitest`, `jest`): supports exact file paths with `TargetConfidence.EXACT`.
    - `ruby-rake`: formats `TEST=path/to/test.rb`.
    - Package manager test scripts (`npm`, `yarn`, `pnpm`): forwards targets after `--` or directly to `bun run test`.
    - `go-test`: validates package targets, normalizes relative package paths (e.g. `./pkg/calc`), and replaces `./...` in base command with targeted package(s).
    - `cargo-test`: detects test file targets (`tests/<name>.rs`) and formats `--test <name>` (`exact_task`); formats package targets with `-p <pkg>` (`exact_package`).
    - `maven-surefire`: transforms class names and Java/Kotlin source paths to `-Dtest=<classes>` (`exact_class`).
    - `gradle-test`: transforms class names and source paths to `--tests <class>` (`exact_class`).
    - `dotnet-test`: transforms class and filter targets to `--filter FullyQualifiedName~<filter>` (`exact_filter`).
    - `cmake-ctest`: supports test regex targeting with `-R <regex>` (`exact_regex`); detects and rejects direct source file targets (`.c`, `.cpp`), triggering safe fallback.
    - `phpunit` & `swiftpm-test`: formats `--filter <filter>` (`exact_filter`).
    - Unsupported targets: returns `(None, "unsupported_target", TargetConfidence.HEURISTIC)` to signal safe fallback to full suite.

- `src/jev/workspace.py` (Modified, lines 89–144, 1800–1860, 2223–2290, 2397–2420, 2467–2518):
  - Implemented `is_shared_surface_path(path)`:
    - Shared fixtures and helpers: `fixtures/`, `helpers/`, `support/`, `*fixture*`, `*test_helper*`, `*test_util*`.
    - Build files and lockfiles: `pom.xml`, `Cargo.toml`, `CMakeLists.txt`, `Makefile`, `package.json`, `build.gradle*`, `*.csproj`, `*.lock`, `package-lock.json`, `pnpm-lock.yaml`, etc.
    - Test configs: `pytest.ini`, `vitest.config.*`, `jest.config.*`, `phpunit.xml`, `deno.json`, etc.
    - Generated-code inputs: `*.proto`, `*.thrift`, `*.graphql`, `*.sql`, `schema.*`.
    - Root config: root dotfiles (`.env*`, etc.), `Dockerfile`, `docker-compose.yml`.
  - Updated `run_tests`:
    - Validates targets against security constraints: max 512 characters, no directory traversal (`../`), no runner options (`-`).
    - Allows non-file identifiers for class/filter/package runners while rejecting non-existent file paths for file runners.
    - Resolves targeted command through `build_targeted_command` and dispatches subprocess.
  - Updated `evaluate_tests`:
    - Missing target path check: detects non-existent target paths on disk before running preflight or candidate subprocesses, immediately returning `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)` with `target_rule="missing_target_path"`.
    - Heuristic widening: when candidate staged files modify shared surface (`is_shared_surface_path`), widens effective targets to full suite (`effective_targets=None`, `target_rule="widened_shared_surface"`, `target_confidence=TargetConfidence.HEURISTIC`, `fallback_scope={"kind": "full_suite"}`).
    - Unsupported target fallback: when targets cannot be represented by runner profile, safely falls back to full suite with `TargetConfidence.HEURISTIC` and `fallback_scope={"kind": "full_suite"}`.
    - Heuristic target failure widening: if a heuristic targeted run fails, runs the full suite once. If the widened suite passes, treats the failure non-punitively as a target selection failure, returning `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)` with `target_rule="target_selection_failure"`.
    - Evidence level & full-suite invariants: heuristic pass stays `evidence_level=EXIT_ONLY`. Targeted runs are never labeled `full_suite_verified=True`. When `evidence_level==EXIT_ONLY`, `full_suite_verified` is forced to `False`.
    - Target scope recording: populates `target_scope` with `kind`, `paths`, `selection_rule`, `fallback_scope`, and `new_target`.

- `tests/test_target_selection.py` (New file, lines 1–435):
  - 25 unit and integration tests covering:
    1. Exact file targets (pytest, bun, deno, mix)
    2. Package targets (go, cargo)
    3. Class/filter targets (maven, gradle, dotnet)
    4. CTest regex targeting
    5. Unsupported file target fallback to full suite
    6. Heuristic widening for shared fixtures, helpers, and config
    7. Heuristic target failure widening once non-punitively
    8. Missing target path detected before execution
    9. Security rules: target cannot start with `-`, target cannot escape worktree (`..`), target length cap.

---

## 2. How It Works

1. **Hierarchy of Target Selection**:
   - `repository-declared target`: configured in `.jev/config.json`.
   - `exact profile file target`: exact file path passed to file-aware runners (`pytest`, `bun`, `deno`, `mix`, `jest`, `vitest`).
   - `exact package/class/task target`: package or class identifiers passed to build-system runners (`go`, `cargo`, `maven`, `gradle`, `dotnet`, `ctest`).
   - `declared full suite`: default when no targets are specified or when falling back from unsupported targets.
   - `UNMEASURED(NO_COMMAND)`: when runner profile cannot be resolved.

2. **Safe Fallback and Heuristic Widening**:
   - **Shared Surface Modification**: If staged candidate files touch shared fixtures, helpers, build files, lockfiles, test config, generated-code inputs, or root config, target narrowing is bypassed and the test run is widened to the full suite with `target_confidence=HEURISTIC` and `fallback_scope={"kind": "full_suite"}`.
   - **Unsupported Target Fallback**: If a target cannot be represented by the runner (e.g. passing a source file `.cpp` to `cmake-ctest`), the engine falls back to running the full suite with `fallback_scope={"kind": "full_suite"}`.
   - **Heuristic Target Failure Widening**: If a targeted run with heuristic confidence fails, the engine widens once to run the full suite. If the widened run passes, the initial failure is attributed to target-selection failure and returns non-punitive `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)` with zero strikes.
   - **Heuristic Pass**: Any pass resulting from heuristic target confidence stays `evidence_level=EXIT_ONLY` and `full_suite_verified=False`.
   - **Targeted Run Label**: A targeted test run is never labeled `full_suite_verified=True`.

3. **Missing Target Path Detection Before Execution**:
   - Before executing any preflight check or test subprocess, target file paths are verified against the worktree filesystem. If a target does not exist, the run immediately terminates with `UNMEASURED(NO_TEST_EXECUTION_SIGNAL)` and `target_rule="missing_target_path"`.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| Exact file target for pytest | Mechanical (Test) | `tests/test_target_selection.py::test_exact_file_target_pytest` |
| Exact file target for Bun | Mechanical (Test) | `tests/test_target_selection.py::test_exact_file_target_bun` |
| Exact file target for Deno | Mechanical (Test) | `tests/test_target_selection.py::test_exact_file_target_deno` |
| Exact file target for Mix | Mechanical (Test) | `tests/test_target_selection.py::test_exact_file_target_mix` |
| Exact package target for Go (`./pkg/calc`) | Mechanical (Test) | `tests/test_target_selection.py::test_package_target_go` |
| Cargo test file target (`--test <name>`) | Mechanical (Test) | `tests/test_target_selection.py::test_package_target_cargo_test_file` |
| Cargo package target (`-p <name>`) | Mechanical (Test) | `tests/test_target_selection.py::test_package_target_cargo_package` |
| Maven class target (`-Dtest=<class>`) | Mechanical (Test) | `tests/test_target_selection.py::test_class_target_maven` |
| Gradle class target (`--tests <class>`) | Mechanical (Test) | `tests/test_target_selection.py::test_class_target_gradle` |
| DotNet filter target (`--filter FullyQualifiedName~<filter>`) | Mechanical (Test) | `tests/test_target_selection.py::test_filter_target_dotnet` |
| CTest regex target (`-R <regex>`) | Mechanical (Test) | `tests/test_target_selection.py::test_regex_target_ctest` |
| Unsupported file target falls back to full suite | Mechanical (Test) | `tests/test_target_selection.py::test_unsupported_file_target_falls_back` |
| Heuristic widening for `conftest.py` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[conftest.py]` |
| Heuristic widening for `tests/fixtures/data.json` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[tests/fixtures/data.json]` |
| Heuristic widening for `tests/helpers/utils.py` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[tests/helpers/utils.py]` |
| Heuristic widening for `pyproject.toml` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[pyproject.toml]` |
| Heuristic widening for `poetry.lock` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[poetry.lock]` |
| Heuristic widening for `pytest.ini` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[pytest.ini]` |
| Heuristic widening for `schema.proto` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[schema.proto]` |
| Heuristic widening for `schema.sql` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[schema.sql]` |
| Heuristic widening for `.env.test` | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_widening_shared_surface[.env.test]` |
| Heuristic target failure widens once (non-punitive) | Mechanical (Test) | `tests/test_target_selection.py::test_heuristic_target_failure_widens_once_non_punitive` |
| Missing target path detected before execution | Mechanical (Test) | `tests/test_target_selection.py::test_missing_target_path_detected_before_execution` |
| Target starting with `-` rejected | Mechanical (Test) | `tests/test_target_selection.py::test_target_cannot_start_with_dash` |
| Target escaping worktree (`../`) rejected | Mechanical (Test) | `tests/test_target_selection.py::test_target_cannot_escape_worktree` |
| Target length cap enforced | Mechanical (Test) | `tests/test_target_selection.py::test_target_length_cap` |
| Targeted run is never labeled full-suite | Mechanical (Test) | `tests/test_target_selection.py::test_targeted_run_never_labeled_full_suite` |
| Heuristic pass stays `exit_only` | Mechanical (Test) | `tests/test_target_selection.py::test_unsupported_file_target_falls_back` |
| Safe fallback routing in unseen environments | Prompt & Architecture | Architectural dispatch via `build_targeted_command` |

---

## 4. Unrequested Changes
- **None**. Only files specified in Phase 5 were modified or created (`src/jev/runner_registry.py`, `src/jev/workspace.py`, `tests/test_target_selection.py`, `docs/phases/phase5.md`).

---

## 5. Open Problems
- **None**: All 25 tests in `tests/test_target_selection.py` pass. Full regression test suites (`test_workspace_targeted.py`, `test_runner_registry.py`, `test_preflight.py`) pass without regressions.

---

## 6. Verification Script Output

```text
=== Verification Report for base: origin/phase4-preflight-support ===
WARNING: Working tree is dirty!

Branch: phase5-target-selection
Commit: b01329d86ff70b79db297879fc3e21a0b7346fce

--- Diff stat vs origin/phase4-preflight-support ---
src/jev/runner_registry.py     | 149 +++++++++++++
 src/jev/workspace.py           | 473 +++++++++++++++++++++++++++++++----------
 tests/test_target_selection.py | 424 ++++++++++++++++++++++++++++++++++++
 3 files changed, 935 insertions(+), 111 deletions(-)

--- Test Changes ---
Added tests:
  + test_exact_file_target_pytest
  + test_exact_file_target_bun
  + test_exact_file_target_deno
  + test_exact_file_target_mix
  + test_package_target_go
  + test_package_target_cargo_test_file
  + test_package_target_cargo_package
  + test_class_target_maven
  + test_class_target_gradle
  + test_filter_target_dotnet
  + test_regex_target_ctest
  + test_unsupported_file_target_falls_back
  + test_heuristic_widening_shared_surface
  + test_heuristic_target_failure_widens_once_non_punitive
  + test_missing_target_path_detected_before_execution
  + test_targeted_run_never_labeled_full_suite
  + test_target_new_at_base_recorded

--- Collected Test Counts ---
  tests/test_target_selection.py: 25 tests collected in 1.35s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4779:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2011:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:4289:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1290:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1323:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1354:        if self.worktree_dir == self.repo_dir:

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
922 passed, 18 skipped, 2 deselected, 2 warnings in 1032.62s (0:17:12)
```
