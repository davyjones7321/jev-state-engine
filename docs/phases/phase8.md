# Phase 8 Report: Same-Command Evidence Authority (Verify and Close Gaps)

## 1. What Changed (Files and Lines)

### Files Modified / Created:
- `tests/test_phase8_same_command_authority.py` (New file, lines 1–355):
  - Added comprehensive test suite verifying the authoritative same-command comparison rules and closing gaps:
    1. `test_candidate_pass_needs_no_base` (lines 60–92): verifies that when a candidate passes cleanly, 0 base tests are run, no detached base worktree is created, and evidence is `PASS` with `comparison == Comparison.NOT_COMPARED`.
    2. `test_candidate_fail_with_unavailable_base_is_never_a_regression` (lines 95–148): verifies that when a candidate fails but base tests are unavailable, evidence is `UNMEASURED(BASELINE_UNAVAILABLE)`, and in `node_gate`, this sets `gate_status == "unmeasured"` with 0 strikes (never a regression).
    3. `test_new_target_absent_at_base_is_not_applicable` (lines 151–189): verifies that when a candidate fails on a newly authored target absent at base (`present_at_base == 0`), `base_status` is explicitly set to `BaseStatus.NOT_APPLICABLE` (distinct from `BaseStatus.UNMEASURED` and `MechReason.BASELINE_UNAVAILABLE`) with `comparison == Comparison.CANDIDATE_ONLY`.
    4. `test_legacy_classification_cannot_add_a_strike` (lines 192–239): verifies that legacy classification strings (such as `test_classification="REGRESSION_DETECTED"` or `test_classification="FAILED"`) cannot add a strike when mechanical evidence is `UNMEASURED`.
    5. `test_failed_attempt_never_lands` (lines 242–305): verifies that when an attempt fails at `node_gate`, `_teardown_worktree` removes the worktree, nothing is merged into the integration branch, and the integration branch commit SHA remains unchanged.
    6. `test_grep_production_gate_code` (lines 308–355): AST and grep verification confirming that `run_baseline_tests(` is absent from `src/jev/engine.py` and that no production strike or merge decision branches on `baseline_failing_ids`, `new_failing_ids`, `baseline_failures`, `full_suite_clean`, or `test_classification`.

- Code Verification in `src/jev/engine.py` & `src/jev/workspace.py`:
  - Verified that `run_baseline_tests(` is not invoked anywhere in `src/jev/engine.py`.
  - Verified that `node_gate` mechanical failure adjudication is strictly based on `not mech_result.passed and not is_unmeasured and failed_check not in ("no_compile_command", "env_not_ready")`, never checking legacy classification strings or parser counts for strike decisions.
  - Verified that `BaseStatus.NOT_APPLICABLE` is distinct from `BaseStatus.UNMEASURED` and `MechReason.BASELINE_UNAVAILABLE`.
  - Verified that authoritative detached base test runs use identical command argv, cwd, target scope, command identity, profile version, relevant config and lockfile identity, and cleanly torn-down detached base worktrees.
  - Verified that legacy columns (`baseline_failures`, `new_failures`, `full_suite_clean`) in `src/jev/ledger.py` remain intact for legacy databases and historical queries without being used for gating decisions.

---

## 2. How It Works

1. **Candidate Pass Needs No Base**:
   - When candidate tests pass (`cand_exit == 0`), the engine immediately returns `MechanicalEvidence(mech_status=MechStatus.PASS, comparison=Comparison.NOT_COMPARED, base_status=None)`.
   - `_run_detached_base_tests` is never invoked, saving time and resources.

2. **Unavailable Base Is Never a Regression**:
   - If candidate tests fail and detached base tests cannot run, fail to start, timeout, or return unmeasured, the engine returns `UNMEASURED(BASELINE_UNAVAILABLE)`.
   - In `node_gate`, `is_unmeasured` is `True`, so `gate_status` is set to `"unmeasured"` (or `"verification_inconclusive"` upon retry exhaustion) and `state["mechanical_strike_count"]` is NOT incremented.

3. **Target Absent at Base as NOT_APPLICABLE**:
   - Before executing detached base tests, `_check_targets_at_commit` checks if targeted files existed at `base_commit`.
   - If total targets at base is 0, base status is set to `BaseStatus.NOT_APPLICABLE` and comparison is `Comparison.CANDIDATE_ONLY`.
   - This remains clearly distinguished from `BaseStatus.UNMEASURED` and `MechReason.BASELINE_UNAVAILABLE`.

4. **Legacy Strings Cannot Add Strikes**:
   - Legacy columns (`test_classification`, `baseline_failures`, `new_failures`) are preserved solely for telemetry and backward-compatible history in the SQLite ledger.
   - Production gate decisions in `node_gate` evaluate `mech_evidence.mech_status`. `test_classification` is never used to determine whether to strike.

5. **Failed Attempts Never Land**:
   - In `node_gate`, `merge_subgoal_worktree` is only called when `verdict.valid` is `True` (both mechanical checks and semantic gatekeeper approve).
   - If any mechanical check, unmeasured retry cap, or semantic check fails, `_teardown_worktree` is executed, and no merge occurs. The integration branch remains completely unmodified.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| Candidate pass needs no base (0 base runs, comparison=NOT_COMPARED) | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_candidate_pass_needs_no_base` |
| Candidate fail with unavailable base returns UNMEASURED and 0 strikes | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_candidate_fail_with_unavailable_base_is_never_a_regression` |
| New target absent at base sets `base_status = BaseStatus.NOT_APPLICABLE` | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_new_target_absent_at_base_is_not_applicable` |
| Legacy classification cannot add a strike in production gate decisions | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_legacy_classification_cannot_add_a_strike` |
| A failed attempt never lands (worktree torn down, integration branch unchanged) | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_failed_attempt_never_lands` |
| Grep-based test over production gate code verifying invariants | Mechanical (Test) | `tests/test_phase8_same_command_authority.py::test_grep_production_gate_code` |

---

## 4. Skipped Tests and Reasons
- **None**: No existing tests were edited, deleted, or skipped.

---

## 5. Unrequested Changes
- **None**: Only files specified in Phase 8 were modified or created (`tests/test_phase8_same_command_authority.py`, `docs/phases/phase8.md`).

---

## 6. Open Problems
- **None**: All 6 tests in `tests/test_phase8_same_command_authority.py` pass. Full test suite (956 passed, 18 skipped, 2 deselected, 0 failed).

---

## 7. Verification Script Output

```text
=== Verification Report for base: origin/phase7-test-surface-integrity ===
WARNING: Working tree is dirty!

Branch: phase8-same-command-authority
Commit: fea1a7cb2c72d747ecf317b4d858929ab71c22bd

--- Diff stat vs origin/phase7-test-surface-integrity ---
tests/test_phase8_same_command_authority.py | 355 ++++++++++++++++++++++++++++
 1 file changed, 355 insertions(+)

--- Test Changes ---
Added tests:
  + test_repo
  + test_candidate_pass_needs_no_base
  + test_candidate_fail_with_unavailable_base_is_never_a_regression
  + test_new_target_absent_at_base_is_not_applicable
  + test_legacy_classification_cannot_add_a_strike
  + test_failed_attempt_never_lands
  + test_grep_production_gate_code

--- Collected Test Counts ---
  tests/test_phase8_same_command_authority.py: 6 tests collected in 2.02s

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
956 passed, 18 skipped, 2 deselected, 2 warnings in 1186.91s (0:19:46)
```
