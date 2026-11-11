# Phase Report: evals-ready

**Repository:** `davyjones7321/jev-state-engine`  
**Phase:** evals-ready  
**Base Commit / Ref:** `master` (`d44754dd4e9be038100e47087f98b630b91df54d`)  
**Target Branch:** `evals-ready`  
**Review Branch for Runtime Changes:** `eval-worktree-inputs` (isolated; not merged to master or evals-ready)  
**Date:** 2026-11-11  
**Status:** Verification Completed & Passed — Branch ready for review.

---

## 1. Summary of Work & Requirements

1. **Manifest Status (`pending_credential_boundary`):**
   - In `evals/manifests/pilot.json`, manifest status is set to `"status": "pending_credential_boundary"`.
   - The status `"ready_for_scored_mbpp_plus"` is avoided until the `hardening-env-scrub` branch is merged to `master`.

2. **Reproduction Script (`evals/select_tasks.py`) & Frozen 10 Task Selection:**
   - Added `evals/select_tasks.py` which deterministically reproduces the 10 frozen task IDs from seed 42.
   - **Exact Method:**
     - **Dataset:** Official MBPP+ dataset v0.2.0 (`MbppPlus.jsonl.gz`), containing 378 canonical tasks.
     - **Sorting Order:** Canonical sorted IDs, sorted numerically by the integer suffix of the ID (`key=lambda x: int(x.split('/')[-1])`).
     - **Smoke Tasks Excluded:** Development smoke tasks `{"Mbpp/2", "Mbpp/3", "Mbpp/4"}` are excluded from the candidate pool, leaving 375 eligible tasks.
     - **Random Function:** Python standard library `random.Random(42).sample(pool, 10)`. The selected tasks are then sorted canonically by integer ID.
   - **Output:** `["Mbpp/56", "Mbpp/102", "Mbpp/108", "Mbpp/129", "Mbpp/252", "Mbpp/266", "Mbpp/282", "Mbpp/616", "Mbpp/752", "Mbpp/775"]`.
   - **Verification Test:** Added unit test `test_output_equals_task_ids_selected_in_pilot_manifest` asserting output identically matches `task_ids_selected` in `evals/manifests/pilot.json`.

3. **Credential Boundary Probe Wiring Status:**
   - **Status: NOT WIRED YET.**
   - The credential boundary probe in `evals/runner.py` currently spawns an isolated probe child process via `subprocess.run([sys.executable, ...])`.
   - Wiring the probe to spawn through `src/jev/process_runner.run_command` (to test the real Jev spawn path) is deferred until after the `hardening-env-scrub` branch is merged to `master`, as instructed ("Do not touch src/ or master").

4. **Separation of Runtime Changes onto `eval-worktree-inputs`:**
   - Commits `183935c..42d2b02` and `463c466` previously modified `src/jev/engine.py`, `src/jev/workspace.py`, and `tests/test_worktree.py`.
   - All runtime modifications have been isolated on the standalone review branch `eval-worktree-inputs` (pushed to origin for review; not merged to master).
   - On `evals-ready`, `src/` and existing `tests/` match `master` byte-for-byte (`git diff master -- src tests` is completely empty).
   - **Exact behavior change for normal non-eval runs: NONE.**

5. **Dry-Run Mode (`evals/runner.py`):**
   - Implemented `execute_dry_run()` executing the 3 non-scored development tasks (`Mbpp/2`, `Mbpp/3`, `Mbpp/4`) end-to-end.
   - Validates credential boundaries before execution.
   - Uses offline bundled fixtures to synthesize solutions and validates Python syntax via `py_compile`.
   - Guarantees 0 network calls (no socket, HTTP, or remote API connections) and 0 JEV API calls.
   - Emits valid run records meeting `run.schema.json` constraints with `credential_boundary_verified: true` and `dry_run: true`.
   - Integrated into `evals/benchmarks/mbpp_plus/adapter.py` via `dry-run` and `check-boundary` subcommands.

6. **GAIA and AssistantBench Status:**
   - Marked `"status": "not_built"` in `evals/manifests/pilot.json`. No mock tasks or synthetic adapters invented.
   - `evals/README.md` and `evals/PROTOCOL.md` reflect that the first scored pilot run consists exclusively of MBPP+ (10 distinct tasks, up to 30 runs total across 3 agents).

---

## 2. Test Suite & Coverage

### New Tests Added: 12 tests
Across 2 new test modules in `evals/tests/`:
- **`evals/tests/test_runner.py` (8 new tests):**
  - `test_is_sensitive_env_var_detection`: Tests classification of API keys and secrets.
  - `test_clean_environment_passes_probe`: Verifies probe passes in a clean environment.
  - `test_probe_detects_api_key_in_child_process`: Verifies probe fails when sensitive keys are visible.
  - `test_run_record_credential_boundary_verified_field_in_schema`: Verifies schema enforcement of `credential_boundary_verified`.
  - `test_get_sanitized_environment_strips_all_keys`: Verifies scrubbing helper behavior.
  - `test_dry_run_executes_three_non_scored_tasks_end_to_end`: Runs 3 smoke tasks end-to-end.
  - `test_dry_run_makes_no_network_or_api_calls`: Monkeys-patches socket, urllib, http to prove zero network traffic.
  - `test_dry_run_fails_closed_when_credential_leaked`: Proves fail-closed behavior on credential leakage.
- **`evals/tests/test_select_tasks.py` (4 new tests):**
  - `test_output_equals_task_ids_selected_in_pilot_manifest`: Asserts selection matches manifest `task_ids_selected`.
  - `test_deterministic_reproduction`: Asserts determinism with seed 42.
  - `test_smoke_tasks_excluded`: Asserts `Mbpp/2, 3, 4` are never selected.
  - `test_canonical_integer_sort_order`: Asserts canonical numerical sort order.

### Pytest Output (`evals/tests` only)

```text
============================= test session starts =============================
platform win32 -- Python 3.13.12, pytest-9.1.1, pluggy-1.5.0
rootdir: D:\edge-downloades\d\intership-projects\jev-state-engine
configfile: pytest.ini
plugins: anyio-4.10.0, hypothesis-6.156.6, langsmith-0.8.3, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 12 items

evals/tests/test_runner.py::CredentialBoundaryTests::test_clean_environment_passes_probe PASSED [  8%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_get_sanitized_environment_strips_all_keys PASSED [ 16%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_is_sensitive_env_var_detection PASSED [ 25%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_probe_detects_api_key_in_child_process PASSED [ 33%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_run_record_credential_boundary_verified_field_in_schema PASSED [ 41%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_executes_three_non_scored_tasks_end_to_end PASSED [ 50%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_fails_closed_when_credential_leaked PASSED [ 58%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_makes_no_network_or_api_calls PASSED [ 66%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_canonical_integer_sort_order PASSED [ 75%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_deterministic_reproduction PASSED [ 83%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_output_equals_task_ids_selected_in_pilot_manifest PASSED [ 91%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_smoke_tasks_excluded PASSED [100%]

============================= 12 passed in 11.96s =============================
```

---

## 3. Unrequested Changes

**NONE.**
- `src/` was not modified in any way (`git diff master -- src` is empty).
- `master` was not modified or pushed to.
- All modifications are strictly confined to `evals/`, `docs/phases/evals-ready.md`, and CI workflow `evals-mbpp-adapter.yml`.
