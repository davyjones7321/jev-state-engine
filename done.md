# Phase 1: Freeze the Evidence Contract (Verify and Close Gaps)

## 1. What Changed (File + Lines)

- **`src/jev/mech_evidence.py` (lines 79, 99–100, 114–115, 123)**:
  - Added `unverified_kind: Optional[str] = None` field to `MechanicalEvidence` to track the specific cause of mechanical non-verification (`"exit_only"` vs `"untested"`).
  - In `validate_invariants()`:
    - Status `UNMEASURED` forces `mechanically_unverified = True` and sets `unverified_kind = "untested"` when not already specified.
    - Status `PASS` with `evidence_level` in `(exit_only, none)` forces `full_suite_verified = False`, `mechanically_unverified = True`, and sets `unverified_kind = "exit_only"`.
    - Status `PASS` with `evidence_level` in `(counted, named)` forces `mechanically_unverified = False` and resets `unverified_kind = None`.

- **`src/jev/models.py` (line 142)**:
  - Added `mechanically_unverified_kinds: Optional[Dict[str, str]]` to the `State` TypedDict to record per-subgoal unverified classification mapping.

- **`src/jev/ledger.py` (lines 175, 306, 495, 533, 782, 820, 845–846, 876, 883, 947, 1024)**:
  - Updated `AttemptsRecorder` Protocol, `NullRecorder`, and `SqliteLedger.record_attempt()` to accept `unverified_kind: Optional[str] = None`.
  - Added `mech_unverified_kind TEXT DEFAULT NULL` column to the `attempts` table schema and dynamic migration list in `SqliteLedger._init_db()`.
  - Persisted `m_unverified_kind` into `attempts` table rows, extracting from explicit parameter or `mech_evidence.unverified_kind`.
  - Restored `unverified_kind` from column `mech_unverified_kind` in `SqliteLedger.get_attempt_mech_evidence()`.

- **`src/jev/engine.py` (lines 3320, 3437, 3828–3833, 4204, 4400–4429, 4446, 4459–4460, 4808–4812)**:
  - In `_snapshot_attempt()`: passed `unverified_kind` through to `safe_rec.record_attempt()`.
  - In `node_gate()`:
    - Ensured `TEST_SURFACE_CHANGED` can be set and persisted as `MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.TEST_SURFACE_CHANGED, test_surface_changed=True)` without triggering strikes.
    - Removed the string-replacement workaround `.replace("FULL_SUITE_PASSED", "PASSED_EXIT_ONLY")` and regex replacement.
    - Classified passing subgoals lacking mechanical test verification into `"untested"` (when tests did not run, `subgoal.expects_tests` is false, or `test_policy` is off/verify-only) vs `"exit_only"` (when `evidence_level` is `exit_only`/`none`).
    - Recorded unverified subgoals in `state["mechanically_unverified_subgoals"]` and their kinds in `state["mechanically_unverified_kinds"]`.
    - Captured `mechanically_unverified` and `unverified_kind` in telemetry and attempt snapshots.
  - In `node_verify()`: preserved both exit-only and untested unverified subgoals from state and trajectory.

- **`src/main.py` (lines 296–314)**:
  - Replaced misleading CLI report line `Evidence: ... (no tests ran: test_policy=auto)` with the real cause:
    - `expects_tests=false` when subgoals do not expect tests.
    - `no test framework configured` when `reason` is `NO_COMMAND` or `MISSING_TOOL`.
    - `subgoals do not expect tests` when subgoals are classified as untested.

- **`tests/test_phase1_evidence_contract.py` (lines 1–441)**:
  - Added 10 tests verifying all Phase 1 invariants and contracts:
    1. `test_old_ledger_fixture_loads_and_legacy_rows_never_verified`: validates old ledger fixture (`tests/fixtures/ledger_pre_step1.sql`) loads cleanly and legacy rows with NULL mech columns are never treated as verified.
    2. `test_evidence_round_trip_with_test_surface_changed`: tests persistence and round-trip of `TEST_SURFACE_CHANGED`.
    3. `test_evidence_round_trip_exit_only`: tests round-trip of `exit_only` evidence and `unverified_kind="exit_only"`.
    4. `test_unmeasured_requires_reason_and_forces_unverified`: tests invariant validation for `UNMEASURED`.
    5. `test_exit_only_never_full_suite`: tests that `exit_only` and `none` evidence levels never allow `full_suite_verified=True`.
    6. `test_execution_path_sets_test_surface_changed`: verifies `node_gate` execution path sets `TEST_SURFACE_CHANGED` without striking.
    7. `test_node_gate_records_untested_and_exit_only_subgoals`: tests both exit-only and untested subgoals are collected in `mechanically_unverified_subgoals` with kinds in ledger and trajectory.
    8. `test_no_string_replacement_of_full_suite_passed`: asserts no string replacement workaround exists in `src/jev/engine.py`.
    9. `test_grep_gate_does_not_read_legacy_classification_for_decisions`: AST & grep test proving `node_gate` and `route_gate` do not read legacy classification strings for decisions.
    10. `test_main_report_line_replaces_misleading_auto_policy`: tests CLI report replaces misleading text with the real cause.

---

## 2. How It Works

1. **Evidence Model & Invariant Enforcement**:
   - `MechanicalEvidence` enforces strict model invariants: any attempt marked `UNMEASURED` forces `mechanically_unverified = True` and sets `unverified_kind = "untested"`. Attempts with `evidence_level` of `exit_only` or `none` can never claim `full_suite_verified = True`, and if marked `PASS`, force `mechanically_unverified = True` and `unverified_kind = "exit_only"`.
2. **Telemetry & Ledger Persistence**:
   - The SQLite ledger schema dynamically migrates to include column `mech_unverified_kind TEXT DEFAULT NULL`. Old rows from pre-step 1 ledgers load with NULL values and are never treated as verified. Every attempt snapshot preserves `unverified_kind` alongside mechanical evidence.
3. **Gate & Verify Unverified Accounting**:
   - In `node_gate()`, passing subgoals without positive mechanical test execution are classified into either `"exit_only"` or `"untested"`. Both kinds are collected into `state["mechanically_unverified_subgoals"]` and mapped in `state["mechanically_unverified_kinds"]`. `node_verify()` preserves this list across the verification stage.
4. **Accurate Reporting**:
   - When no tests run under `test_policy=auto`, `src/main.py` analyzes the subgoals and trajectory to report the actual root cause (`expects_tests=false`, `no test framework configured`, or `subgoals do not expect tests`) rather than generic policy text.
5. **Separation of Legacy Classification**:
   - Gate decision logic (`node_gate`, `route_gate`) relies exclusively on mechanical checks (`failed_check`, `test_surface_changed`, `mech_evidence`), completely severed from legacy classification strings (`test_classification`, `baseline_failures`, `new_failures`, `full_suite_clean`).

---

## 3. Rule Enforcement: Mechanically Enforced vs. Prompt-Only

| Rule | Enforcement Type | Enforcing Test / Mechanism |
|---|---|---|
| Old ledger fixture loads and legacy rows never verified | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_old_ledger_fixture_loads_and_legacy_rows_never_verified` |
| Evidence round-trips with `TEST_SURFACE_CHANGED` | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_evidence_round_trip_with_test_surface_changed` |
| Evidence round-trips with `exit_only` and kind | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_evidence_round_trip_exit_only` |
| `UNMEASURED` always has a reason and forces `mechanically_unverified=True` | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_unmeasured_requires_reason_and_forces_unverified` |
| `exit_only` never allows `full_suite_verified=True` | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_exit_only_never_full_suite` |
| Execution path can set `TEST_SURFACE_CHANGED` without strike | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_execution_path_sets_test_surface_changed` |
| Report & state list untested and exit-only subgoals with kind | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_node_gate_records_untested_and_exit_only_subgoals` |
| No string replacement of `FULL_SUITE_PASSED` in engine | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_no_string_replacement_of_full_suite_passed` |
| Production gate does not read legacy classification for decisions | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_grep_gate_does_not_read_legacy_classification_for_decisions` |
| CLI report line replaces misleading auto policy text | **MECHANICALLY ENFORCED** | `tests/test_phase1_evidence_contract.py::test_main_report_line_replaces_misleading_auto_policy` |
| Protocol constraints (never merge, never push to remote) | **PROMPT-ONLY** | `session-1-v2.md` phase protocol |
| Branch naming convention (`phase1-freeze-evidence-contract`) | **PROMPT-ONLY** | `session-1-v2.md` branch rule |
| Restricting file edits to phase-specified files | **PROMPT-ONLY** | `session-1-v2.md` touched files constraint |

---

## 4. Open Problems

- Phase 7 will build directly upon `TEST_SURFACE_CHANGED` to detect when a worker expands or alters the test surface unexpectedly.
- Pre-existing skipped test in entire suite: `tests/test_live_substep_7a.py:9` (requires live pipeline flag).

---

## 5. Verification Output (`python scripts/verify_phase.py origin/phase0b-ci-and-verify-script`)

```text
=== Verification Report for base: origin/phase0b-ci-and-verify-script ===
Branch: phase1-freeze-evidence-contract
Commit: 4352ce1ae6c7a0eeb894ca8720218c64b23433ab

--- Diff stat vs origin/phase0b-ci-and-verify-script ---
src/jev/engine.py                      |  47 +++-
 src/jev/ledger.py                      |  14 +-
 src/jev/mech_evidence.py               |   6 +
 src/jev/models.py                      |   1 +
 src/main.py                            |  20 +-
 tests/test_phase1_evidence_contract.py | 441 +++++++++++++++++++++++++++++++++
 6 files changed, 520 insertions(+), 9 deletions(-)

--- Test Changes ---
Added tests:
  + test_old_ledger_fixture_loads_and_legacy_rows_never_verified
  + test_evidence_round_trip_with_test_surface_changed
  + test_evidence_round_trip_exit_only
  + test_unmeasured_requires_reason_and_forces_unverified
  + test_exit_only_never_full_suite
  + test_execution_path_sets_test_surface_changed
  + test_node_gate_records_untested_and_exit_only_subgoals
  + test_no_string_replacement_of_full_suite_passed
  + test_grep_gate_does_not_read_legacy_classification_for_decisions
  + test_main_report_line_replaces_misleading_auto_policy

--- Collected Test Counts ---
  tests/test_phase1_evidence_contract.py: 10 tests collected in 2.58s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4772:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1922:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3838:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1273:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1306:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1337:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
................................................                         [100%]
============================== warnings summary ===============================
C:\Users\DavyJ\miniconda3\Lib\site-packages\langgraph\checkpoint\serde\encrypted.py:5
  C:\Users\DavyJ\miniconda3\Lib\site-packages\langgraph\checkpoint\serde\encrypted.py:5: LangChainPendingDeprecationWarning: The default value of `allowed_objects` will change in a future version. Pass an explicit value (e.g., allowed_objects='messages' or allowed_objects='core') to suppress this warning.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
src\jev\models.py:38
  D:\edge-downloades\d\intership-projects\jev-state-engine\src\jev\models.py:38: PytestCollectionWarning: cannot collect test class 'TestPolicy' because it has a __init__ constructor (from: tests/test_evidence.py)
    class TestPolicy(str, Enum):
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ===========================
SKIPPED [1] tests\test_live_substep_7a.py:9: Set RUN_LIVE_PIPELINE=1 to execute live pipeline
767 passed, 1 skipped, 2 deselected, 2 warnings in 601.65s (0:10:01)
```
