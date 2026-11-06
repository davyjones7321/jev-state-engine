# Phase R1 Report: Retry Attempt Identity

Base ref: `origin/master` (tip: `0a80755`)  
Branch: `hardening-run-a`  
Phase: `R1` (Retry attempt identity)

---

## 1. Files and Lines Changed

### 1. New Helper Module
- `src/jev/retry_identity.py`:
  - `create_execution_record(...)`: Constructs a typed dictionary representing a test execution record:
    - `execution_id` (UUID4 string)
    - `attempt` (integer attempt number >= 1)
    - `phase` (`"initial"` or `"flaky_rerun"`)
    - `process_launched` (boolean indicating if a subprocess was executed)
    - `argv` (command line arguments list)
    - `exit_code` (integer or None)
    - `started_at` / `ended_at` (epoch timestamps)
    - `duration_s` (floating duration in seconds)
    - `reason` (string reason e.g. "timeout", "launch_failed", or None)
    - `run_id` (string ticket run identifier)
  - `create_event(...)`: Constructs a typed trajectory event dictionary:
    - `event_name` (e.g. `"retry_launched"`, `"flaky_rerun_launched"`, `"retry_skipped_exact_duplicate"`, `"test_process_timed_out"`, `"test_process_failed"`)
    - `subgoal_id` (string ID of current subgoal)
    - `attempt` (integer attempt number)
    - `phase` (optional execution phase)
    - `timestamp` (epoch timestamp)
    - additional contextual keyword arguments

### 2. Workspace Updates
- `src/jev/workspace.py`:
  - `Workspace.__init__` (~lines 1136–1150):
    - Added `run_id: str = str(uuid.uuid4())`
    - Added `test_executions: List[Dict[str, Any]] = []`
    - Added `events: List[Dict[str, Any]] = []`
    - Added `_attempt_result_cache: Dict[str, Any] = {}`
    - Initialized `last_base_run: Optional[dict] = None`
  - Helpers added (~lines 1289–1343):
    - `_emit_event(...)`: Helper to append typed events to `self.events` and return them.
    - `_record_execution_item(...)`: Builds execution record via `create_execution_record`, appends to `self.test_executions`, attaches record to `last_run`, and emits failure/timeout events.
  - Base Worktree Execution (`_run_original_tests_in_scratch`, ~lines 2603–2660):
    - Added `attempt: int = 1` parameter.
    - Partitioned `_base_run_counts` by `f"{run_id}#{subgoal_id}#{attempt}"`.
    - Maintained loop index `base_try` for retry on fail while correctly preserving attempt identity.
  - Test Evaluation (`evaluate_tests` & `_evaluate_tests_internal`, ~lines 2960–3800):
    - Added `attempt: int = 1` parameter to `evaluate_tests` and `_evaluate_tests_internal`.
    - Keyed `_candidate_run_counts` by composite identity: `f"{run_id}#{subgoal_id}#{attempt}"`.
    - Maintained execution cap of 2 runs per attempt (initial run + 1 flaky rerun).
    - Caching / Deduplication: If candidate run count for the attempt has reached its limit (>= 2) and an entry exists in `_attempt_result_cache`, `evaluate_tests` returns the recorded result without launching a subprocess and emits `retry_skipped_exact_duplicate`.
    - Recorded `_record_execution_item` for both initial candidate run and flaky rerun.
    - Emitted `retry_launched` when `attempt > 1` and `flaky_rerun_launched` when flaky rerun executes.
    - Populated `_attempt_result_cache[attempt_key]` on completion.
  - Mechanical Checks Entrypoint (`run_mechanical_checks`, ~lines 5390–5580):
    - Added `attempt: int = 1` parameter and propagated it directly to `evaluate_tests`.

### 3. Engine Orchestration Updates
- `src/jev/engine.py`:
  - `node_gate` (~lines 3880–3920):
    - Computed `current_attempt` from `failed_attempts` / strikes (`failed_attempts + 1`).
    - Propagated `kwargs["attempt"] = current_attempt` into `workspace.run_mechanical_checks`.
    - Synchronized `workspace.run_id = str(state.get("thread_id"))` if set.
    - Drained pending events from `workspace.events` into `state["trajectory"]`.
  - `node_implement` (~lines 2490–2500):
    - Synchronized `workspace.run_id = str(state.get("thread_id"))`.

### 4. Tests
- `tests/test_hardening_r1_retry_identity.py` (new, 373 lines):
  - 12 comprehensive unit tests covering all R1 acceptance criteria.

---

## 2. How It Works

1. **Attempt Identity Model**:
   - Every mechanical check execution is keyed by a 4-part identity tuple:
     `(run_id, subgoal_id, attempt, phase)`
     where `phase ∈ {"initial", "flaky_rerun"}`.
   - `run_id` defaults to a UUID4 on the Workspace and is aligned with `state["thread_id"]` by the engine during a ticket run.
   - `attempt` is explicitly tracked by the engine (derived from strike count and failed attempt history) and passed down through `run_mechanical_checks(..., attempt=attempt)` to `evaluate_tests(attempt=attempt)`.

2. **Decoupled Execution Counters**:
   - `_candidate_run_counts` is keyed by `f"{run_id}#{subgoal_id}#{attempt}"`.
   - Each attempt is capped at 2 subprocess executions:
     - 1 initial candidate run
     - 1 flaky rerun (if the initial run fails or produces suspect results)
   - Retries (attempt 2, attempt 3, etc.) have distinct identity keys. They never inherit or share execution count caps with attempt 1.
   - A sequence of `broken -> broken -> fixed` successfully executes the test subprocess on attempt 1 (2 runs: fail + rerun fail), attempt 2 (2 runs: fail + rerun fail), and attempt 3 (1 run: pass), yielding verdicts `FAIL`, `FAIL`, and `PASS`.

3. **Exact Duplicate Deduplication**:
   - If an exact duplicate request arrives for an attempt that has already reached its execution limit (`_candidate_run_counts >= 2`):
     - The workspace returns the recorded result from `_attempt_result_cache`.
     - Zero new subprocesses are launched.
     - The result is NOT an erroneous `UNMEASURED(TOOLING_ERROR)`.
     - A `retry_skipped_exact_duplicate` event is emitted.

4. **Execution Telemetry & Events**:
   - Every test subprocess execution records a structured `execution_record` attached to `last_test_run` and collected in `workspace.test_executions`.
   - Standardized trajectory events (`retry_launched`, `flaky_rerun_launched`, `retry_skipped_exact_duplicate`, `test_process_timed_out`, `test_process_failed`) are recorded and drained into `state["trajectory"]`.

---

## 3. Rule Enforcement Table

| Requirement / Rule | Enforcement Type | Test Name / Mechanism |
| :--- | :--- | :--- |
| Original execution launches 1 process and records it | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_1_original_execution_launches_one_process_and_records` |
| Flaky rerun launches a second process with distinct phase and is recorded independently | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_2_flaky_rerun_launches_second_process_and_recorded_independently` |
| Engine retry 1 after failing attempt launches at least 1 process | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_3_engine_retry_1_after_failure_launches_at_least_one_process` |
| Engine retry 2 launches at least 1 process; sequence broken, broken, fixed gives FAIL, FAIL, PASS | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_4_engine_retry_2_launches_process_broken_broken_fixed_sequence` |
| Two different subgoals never share counters | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_5_two_different_subgoals_never_share_counters` |
| Two ticket runs (new Workspace or different run_id) never share counters | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_6_two_ticket_runs_never_share_counters` |
| Runner exception recorded with `process_launched` accurate and next retry still runs | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_7_runner_raises_exception_recorded_and_next_retry_runs` |
| Timeout recorded as timeout and next retry still runs | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_8_timeout_recorded_as_timeout_and_next_retry_runs` |
| Exact duplicate request after completion returns recorded result, launches no process, and is NOT UNMEASURED(TOOLING_ERROR) | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_9_exact_duplicate_request_returns_recorded_result_and_no_process_and_not_tooling_error` |
| Legitimate retry with identical test command and identical subgoal still launches | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_10_legitimate_retry_identical_test_command_and_subgoal_still_launches` |
| One attempt can never exceed 2 executions (initial plus one flaky rerun): no infinite loop | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_11_one_attempt_never_exceeds_two_executions` |
| Identity can never silently fall back to attempt 1 in an internal retry path | Mechanical | `tests/test_hardening_r1_retry_identity.py::test_12_identity_never_silently_falls_back_to_attempt_1` |

---

## 4. Failing-Then-Passing Test Output

### Failing Output Before Fix (R1.1)
Running `python -m pytest tests/test_hardening_r1_retry_identity.py`:
```
FAILED tests/test_hardening_r1_retry_identity.py::test_3_engine_retry_1_after_failure_launches_at_least_one_process - AssertionError: Attempt 2 launched 0 processes, expected >= 1
FAILED tests/test_hardening_r1_retry_identity.py::test_4_engine_retry_2_launches_process_broken_broken_fixed_sequence - AssertionError: Attempt 2 launched 0 processes, expected >= 1
FAILED tests/test_hardening_r1_retry_identity.py::test_9_exact_duplicate_request_returns_recorded_result_and_no_process_and_not_tooling_error - AssertionError: assert 'tests_unmeasured' != 'tests_unmeasured'
============================== 3 failed, 9 passed in 172.93s ==============================
```

### Passing Output After Fix (R1.2)
Running `python -m pytest tests/test_hardening_r1_retry_identity.py tests/test_fix7_weakened_deleted_tests.py`:
```
tests\test_hardening_r1_retry_identity.py ............                   [ 70%]
tests\test_fix7_weakened_deleted_tests.py .....                          [100%]
================== 17 passed, 1 warning in 187.94s (0:03:07) ==================
```

---

## 5. Unrequested Changes

- None. Only the following files were modified or created:
  - `src/jev/retry_identity.py` (new)
  - `src/jev/workspace.py` (localized edits: run_id, attempt propagation, cache/counters, events)
  - `src/jev/engine.py` (localized edits: pass attempt from node_gate, reset in node_implement, drain events)
  - `tests/test_hardening_r1_retry_identity.py` (new test suite)
  - `docs/phases/hardening-r1.md` (this report)

---

## 6. Honest Open Problems

1. **Worktree base test caching**: Base runs in detached worktrees (`_run_original_tests_in_scratch`) reuse results for identical commands against the same base commit via `_base_test_cache`. While `_base_run_counts` is now correctly isolated per attempt, identical base runs across subgoals share cache entries to avoid redundant git worktree checkouts.
2. **Windows File Locking & Subprocess Latency**: On Windows (`win32`), launching test subprocesses in temporary git repositories takes noticeably longer (~15 seconds per test) due to directory cleanup and file handle releases.

---

## 7. Audit & Verification of Execution Cache and Phase Identity (Targeted Correction Review)

Following owner review, the execution cache and phase identity architecture was audited against eight specific integrity invariants:

1. **Distinction between Initial and Flaky Rerun Phases**:
   - Every individual test subprocess execution is captured via `create_execution_record` and recorded in `workspace.test_executions` with its full 4-part identity: `(run_id, subgoal_id, attempt, phase)` where `phase ∈ {"initial", "flaky_rerun"}`.
   - The initial execution record and flaky rerun record have distinct `execution_id`s, distinct `phase` strings, distinct start/end timestamps, and distinct exit codes.
2. **Initial Execution Cannot Be Mistaken for Flaky Rerun**:
   - `_evaluate_tests_internal` runs the initial candidate test first (`phase="initial"`).
   - If that initial run exits with non-zero, it does not populate the cache or return; it explicitly transitions into the flaky rerun phase (`phase="flaky_rerun"`).
3. **Flaky Rerun Cannot Be Mistaken for Initial Execution**:
   - The flaky rerun is guarded by `if cand_runs_rerun >= 2` and strictly logs `phase="flaky_rerun"` with event `flaky_rerun_launched`.
4. **Execution Cap of 2 Executions per `(run_id, subgoal_id, attempt)`**:
   - Keyed by `f"{run_id}#{subgoal_id}#{attempt}"`. Initial run increments counter from 0 to 1; flaky rerun increments counter from 1 to 2.
   - Any further execution request within that same attempt sees counter >= 2 and returns `TOOLING_ERROR` to guarantee termination.
5. **Exact Duplicate Requests Return Cached Results**:
   - When an attempt has completed (counter >= 2) and `attempt_key` exists in `_attempt_result_cache`:
     - Re-requesting `evaluate_tests` returns the recorded `MechanicalEvidence` immediately.
     - Launches 0 subprocesses (`process_launched=False`).
     - Emits `retry_skipped_exact_duplicate`.
     - Tested by `test_9_exact_duplicate_request_returns_recorded_result_and_no_process_and_not_tooling_error`.
6. **Legitimate Executions with Identical Commands Never Deduplicated Prematurely**:
   - Flaky rerun re-executes the exact same test command as the initial run, but because it is an explicit retry phase within the attempt, it runs as a fresh process and is recorded independently.
   - Tested by `test_2_flaky_rerun_launches_second_process_and_recorded_independently`.
7. **Engine Retries with Different Attempt Numbers Always Receive Distinct Identities**:
   - Attempt 2 produces `attempt_key = f"{run_id}#{subgoal_id}#2"`. Attempt 3 produces `attempt_key = f"{run_id}#{subgoal_id}#3"`.
   - Counters start at 0, no cache collision occurs, and subprocesses launch for every engine retry attempt.
   - Tested by `test_3`, `test_4`, `test_10`, and `test_12`.
8. **Separate Runs and Subgoals Never Share Cached Results**:
   - Distinct `run_id` (ticket runs) and distinct `subgoal_id` partition both `_candidate_run_counts` and `_attempt_result_cache`.
   - Tested by `test_5_two_different_subgoals_never_share_counters` and `test_6_two_ticket_runs_never_share_counters`.

All 12 tests in `tests/test_hardening_r1_retry_identity.py` pass cleanly (12 passed in 193.23s).



---

## 8. Final Audit Correction (Exact Duplicate Passing Attempt Deduplication)

### Defect Identified During Final Security & Correctness Audit
- **Defect**: In `Workspace.evaluate_tests` (and `_evaluate_tests_internal`), checking `cand_runs >= 2` before looking up `_attempt_result_cache` prevented exact duplicate requests from being deduplicated when an attempt completed successfully on its initial run (`cand_runs == 1`). Consequently, duplicate requests for passing attempts bypassed the cache and accidentally executed a redundant test subprocess.
- **Correction Applied**: Removed `cand_runs >= 2` requirement from the `_attempt_result_cache` check in `evaluate_tests` and `_evaluate_tests_internal`. Completed attempt results cached in `_attempt_result_cache` now return immediately with 0 process launches regardless of whether candidate run count was 1 (passing initial run) or 2 (flaky rerun).
- **Regression Test Added**: `test_13_exact_duplicate_request_for_passing_attempt_launches_no_process` in `tests/test_hardening_r1_retry_identity.py`.
- **Result**: All 13 tests in `tests/test_hardening_r1_retry_identity.py` and all 5 tests in `tests/test_hardening_r1_engine_attempt.py` pass (18 passed in total).
