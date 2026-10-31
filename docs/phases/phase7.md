# Phase 7 Report: Test-Surface and Verifier Integrity (Corrected Rule)

## 1. What Changed (Files and Lines)

### Allowed Files Modified / Created:
- `src/jev/engine.py` (Modified, lines 4203–4211):
  - In `node_gate`: updated mechanical detail block formatting in `standard_block` to explicitly expose `- verifier_modified: true`, `- test_surface_changed: true`, and `- test_authorship: <value>` to Jev.

- `src/jev/workspace.py` (Modified, lines 88–165, 1423–1480, 1512–1570, 2420–2480, 2800–2920):
  - Added `is_shared_test_surface(path, base_content, candidate_content)`: identifies test configuration, discovery rules, shared fixtures, shared helpers, package scripts, build/test task definitions, and lockfiles. Uses structural comparison for `package.json` and `pyproject.toml` to avoid misclassifying version bumps or dependency additions as test surface changes.
  - Added `is_test_file(path)`: identifies authored/targeted test files (unit/integration tests) across Python, TypeScript/JavaScript, Go, Rust, Java, Kotlin, Ruby, PHP, and C#. Explicitly excludes shared fixtures and helpers.
  - Added `Workspace.classify_touched_paths(staged_paths, base_commit)`: classifies all touched files into the 7 Phase 7 categories:
    1. `pre_existing_code`
    2. `pre_existing_test_surface`
    3. `worker_staged_production_code`
    4. `worker_staged_test_files`
    5. `worker_staged_shared_test_surface`
    6. `engine_generated`
    7. `protected_verifier_command`
  - Added `Workspace.record_engine_touched(path)`: records artifacts created by the engine.
  - Updated `Workspace._discard_engine_touched()` and `Workspace.discard_engine_touched()`: discards engine outputs in worktrees using `git clean -f` (for untracked artifacts) and `git checkout --` (for reverted tracked files), while preserving the safety guard when `worktree_dir == repo_dir`.
  - Updated `Workspace.commit_subgoal()`: detects collisions between `worker_staged_paths` and `engine_touched_paths`, raising `PathCollisionError` to prevent commits when a collision occurs.
  - Updated `Workspace.evaluate_tests()`:
    - **Path collision check**: Detects collision between worker-staged and engine-touched paths upfront, returning `UNMEASURED(ENGINE_WORKER_PATH_COLLISION)` with `mechanically_unverified=True` and `unverified_kind="collision"`.
    - **Shared test surface failure**: When worker changes shared test surface (configuration, discovery rules, shared fixtures, shared helpers) and tests fail without command modification, returns `UNMEASURED(TEST_SURFACE_CHANGED)`. In `node_gate`, this produces 0 strikes.
    - **Targeted test files are not shared surface**: Worker-created or worker-edited targeted test files are NOT shared surface. A stable, reproducible failure triggers candidate rerun and base run comparison, producing a strikable `FAIL`.
    - **Pre-existing test editing**: Editing a pre-existing test sets `verifier_modified=True` (visible to Jev in gate feedback), and a failing run still counts as a strike.
    - **Worker-authored tests alone**: Worker-authored tests alone never produce `counted` or `named` positive evidence (forced to `EvidenceLevel.EXIT_ONLY`), and never excuse a failing pre-existing test.
    - Attached `verifier_modified`, `test_surface_changed`, `worker_staged_paths`, and `test_authorship` across evidence outcomes.

- `tests/test_phase7_test_surface_integrity.py` (New file, lines 1–472):
  - 8 unit and integration tests covering all requirements:
    1. `test_classify_touched_paths_categories`
    2. `test_shared_test_surface_failure_returns_unmeasured_and_zero_strikes`
    3. `test_worker_authored_broken_test_strikes`
    4. `test_editing_preexisting_test_sets_verifier_modified_and_strikes_on_failure`
    5. `test_worker_authored_tests_alone_forced_to_exit_only`
    6. `test_worker_authored_test_does_not_excuse_failing_preexisting_test`
    7. `test_collision_between_worker_staged_and_engine_touched_blocks_commit_and_returns_unmeasured`
    8. `test_engine_outputs_removed_on_discard_engine_touched`

---

## 2. How It Works

1. **Path Classification**:
   - `classify_touched_paths` categorizes touched files based on git base-commit tree inspection and path heuristics.
   - Test files are split into pre-existing test surface (existed at `base_commit`), worker-authored test files (new test files created by worker), and shared test surface (conftest, fixtures, helpers, test configs).
   - `is_shared_test_surface` checks directory structure (`helpers/`, `fixtures/`), fixture naming conventions, and structural diffs of build/test manifests.

2. **Shared Test Surface Failures vs. Targeted Test Failures**:
   - When a worker touches shared test surface (such as editing shared fixtures or test configs) and a test fails, the engine recognizes that test infrastructure was modified and returns `UNMEASURED(TEST_SURFACE_CHANGED)`. In `node_gate`, `UNMEASURED` results never add a mechanical strike.
   - When the worker creates or edits targeted test files (`tests/test_foo.py`), those files are NOT treated as shared surface. A failure of those files triggers candidate rerun and base comparison, returning `FAIL` and resulting in a strike.

3. **Pre-Existing Test Modification Visibility**:
   - If the worker edits an existing test file from `base_commit`, `verifier_modified` is set to `True`.
   - `node_gate` injects `- verifier_modified: true` into the standard feedback block shown to Jev so the model has explicit visibility that test verification infrastructure was altered.

4. **Worker-Authored Tests Alone**:
   - If all test files staged by the worker are newly authored tests, `is_worker_authored_only` is set.
   - Any passing run is forced to `EvidenceLevel.EXIT_ONLY` (preventing `COUNTED` or `NAMED` elevation).
   - If pre-existing tests fail, worker-authored passing tests cannot mask or excuse the failure.

5. **Collision Detection and Clean Engine Discard**:
   - When an engine artifact (e.g. compile artifact, temporary test output) is touched/generated by the engine and recorded in `engine_touched_paths`, any worker attempt to stage that file is detected as a collision.
   - `evaluate_tests` returns `UNMEASURED(ENGINE_WORKER_PATH_COLLISION)` and `commit_subgoal` raises `PathCollisionError`, preventing corrupted commits.
   - `discard_engine_touched` cleanly removes untracked engine outputs via `git clean -f` before worker steps.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| Classification of touched paths into 7 Phase 7 categories | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_classify_touched_paths_categories` |
| Shared surface changes return `UNMEASURED(TEST_SURFACE_CHANGED)` and 0 strikes in `node_gate` | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_shared_test_surface_failure_returns_unmeasured_and_zero_strikes` |
| Targeted worker-created test failure returns strikable `FAIL` and adds 1 strike in `node_gate` | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_worker_authored_broken_test_strikes` |
| Pre-existing test editing sets `verifier_modified=True` (visible to Jev) and strikes on failure | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_editing_preexisting_test_sets_verifier_modified_and_strikes_on_failure` |
| Worker-authored tests alone never produce counted or named evidence (forced to `EXIT_ONLY`) | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_worker_authored_tests_alone_forced_to_exit_only` |
| Worker-authored tests never excuse a failing pre-existing test | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_worker_authored_test_does_not_excuse_failing_preexisting_test` |
| Worker-staged and engine-touched path collision returns `UNMEASURED(ENGINE_WORKER_PATH_COLLISION)` and blocks commit | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_collision_between_worker_staged_and_engine_touched_blocks_commit_and_returns_unmeasured` |
| Engine outputs are removed on `discard_engine_touched()` | Mechanical (Test) | `tests/test_phase7_test_surface_integrity.py::test_engine_outputs_removed_on_discard_engine_touched` |

---

## 4. Skipped Tests and Reasons
- **None**: No existing tests were edited, deleted, or skipped.

---

## 5. Unrequested Changes
- **None**: Only files specified in Phase 7 were modified or created (`src/jev/engine.py`, `src/jev/workspace.py`, `tests/test_phase7_test_surface_integrity.py`, `docs/phases/phase7.md`).

---

## 6. Open Problems
- **None**: All 8 tests in `tests/test_phase7_test_surface_integrity.py` pass. The full offline pytest suite passes (950 passed, 18 skipped, 2 deselected, 0 failed).

---

## 7. Verification Script Output

```text
=== Verification Report for base: origin/phase6-zero-test-evidence ===
WARNING: Working tree is dirty!

Branch: phase7-test-surface-integrity
Commit: 98f3cad20df76b0ae85263c8e781ce215496dd6e

--- Diff stat vs origin/phase6-zero-test-evidence ---
src/jev/engine.py                           |   6 +
 src/jev/workspace.py                        | 345 +++++++++++++++++---
 tests/test_phase7_test_surface_integrity.py | 471 ++++++++++++++++++++++++++++
 3 files changed, 775 insertions(+), 47 deletions(-)

--- Test Changes ---
Added tests:
  + test_classify_touched_paths_categories
  + test_shared_test_surface_failure_returns_unmeasured_and_zero_strikes
  + test_worker_authored_broken_test_strikes
  + test_editing_preexisting_test_sets_verifier_modified_and_strikes_on_failure
  + test_worker_authored_tests_alone_forced_to_exit_only
  + test_worker_authored_test_does_not_excuse_failing_preexisting_test
  + test_collision_between_worker_staged_and_engine_touched_blocks_commit_and_returns_unmeasured
  + test_engine_outputs_removed_on_discard_engine_touched

--- Collected Test Counts ---
  tests/test_phase7_test_surface_integrity.py: 8 tests collected in 1.74s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4785:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2207:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:4661:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1423:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1460:        if self.worktree_dir == self.repo_dir:

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
950 passed, 18 skipped, 2 deselected, 2 warnings in 932.19s (0:15:32)
```
