# Phase Report: evals-ready

**Repository:** `davyjones7321/jev-state-engine`  
**Phase:** evals-ready  
**Base Commit / Ref:** `origin/master` (`076a92d86a910371adceabb5d174f41428eb72a1`, includes `hardening-env-scrub`)  
**Target Branch:** `evals-ready`  
**Review Branch for Runtime Changes:** `eval-worktree-inputs` (isolated; not merged to master or evals-ready)  
**Date:** 2026-11-11  
**Status:** Verification Completed & Passed — Credential boundary wired and verified through `src/jev/process_runner.run_command`. Branch ready for review.

---

## 1. Summary of Work & Requirements

1. **Merged `origin/master` (tip `076a92d`, including `hardening-env-scrub`):**
   - Merged the latest `origin/master` into `evals-ready` with a normal merge (no rebase, no force).
   - Working tree remained clean; `src/` matches `origin/master` byte-for-byte.

2. **Credential Boundary Probe Wiring (`evals/runner.py`):**
   - **Status: WIRED AND VERIFIED.**
   - The probe in `evals/runner.py` (`probe_child_environment` / `check_credential_boundary`) spawns through `src/jev/process_runner.run_command`, the real Jev command execution path, replacing standalone `subprocess.run`.
   - The probe tests the real process execution path and inherits `run_command`'s built-in environment scrubbing (`get_scrubbed_env()`).
   - The runner invokes `check_credential_boundary` before every Jev run and refuses to start if the boundary check fails closed.
   - The boundary verification outcome is recorded in every run record at `execution.credential_boundary_verified`.

3. **Manifest Status (`ready_for_scored_mbpp_plus`):**
   - With `origin/master` (including `hardening-env-scrub`) merged and all tests passing, `evals/manifests/pilot.json` status is promoted from `"pending_credential_boundary"` to `"ready_for_scored_mbpp_plus"`.

4. **Requirements & CI Workflow:**
   - Added `jsonschema` to `evals/benchmarks/mbpp_plus/requirements.txt`.
   - The CI workflow `.github/workflows/evals-mbpp-adapter.yml` installs `-r evals/benchmarks/mbpp_plus/requirements.txt`.

5. **Reproduction Script (`evals/select_tasks.py`) & Frozen 10 Task Selection:**
   - Deterministically reproduces the 10 frozen task IDs from seed 42 over canonical sorted IDs, excluding smoke tasks `Mbpp/2, 3, 4`.
   - Output: `["Mbpp/56", "Mbpp/102", "Mbpp/108", "Mbpp/129", "Mbpp/252", "Mbpp/266", "Mbpp/282", "Mbpp/616", "Mbpp/752", "Mbpp/775"]`.

6. **Separation of Runtime Changes onto `eval-worktree-inputs`:**
   - Runtime modifications remain isolated on standalone review branch `eval-worktree-inputs`.
   - On `evals-ready`, `src/` and base `tests/` match `master` (`git diff master -- src` is completely empty).

7. **GAIA and AssistantBench Status:**
   - Marked `"status": "not_built"` in `evals/manifests/pilot.json`.

---

## 2. Test Suite & Coverage

### Total Tests in `evals/tests`: 15 tests (3 new tests added)
Across 2 test modules in `evals/tests/`:
- **`evals/tests/test_runner.py` (11 tests, 3 new):**
  - `test_is_sensitive_env_var_detection`: Tests classification of API keys and secrets.
  - `test_clean_environment_passes_probe`: Verifies probe passes in a clean environment.
  - `test_probe_detects_api_key_in_child_process`: Verifies probe fails when sensitive keys are declared/visible.
  - `test_probe_with_parent_secrets_reports_clean_via_run_command` **(NEW)**: Sets `JEV_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, and `MY_SECRET_TOKEN` in parent process, runs probe through `run_command`, and asserts clean report. Fails if reverted to standalone subprocess.
  - `test_probe_fails_closed_when_run_command_leaks_variable` **(NEW)**: Monkeypatches `run_command` to leak `JEV_API_KEY` and asserts probe fails closed. Fails if reverted to standalone subprocess.
  - `test_probe_fails_closed_when_jev_disable_env_scrub_is_set` **(NEW)**: Asserts probe fails closed if `JEV_DISABLE_ENV_SCRUB=1` is set in parent environment. Fails if reverted to standalone subprocess.
  - `test_run_record_credential_boundary_verified_field_in_schema`: Verifies schema enforcement of `credential_boundary_verified`.
  - `test_get_sanitized_environment_strips_all_keys`: Verifies scrubbing helper behavior.
  - `test_dry_run_executes_three_non_scored_tasks_end_to_end`: Runs 3 smoke tasks end-to-end.
  - `test_dry_run_makes_no_network_or_api_calls`: Monkey-patches socket, urllib, http to prove zero network traffic.
  - `test_dry_run_fails_closed_when_credential_leaked`: Proves fail-closed behavior on credential leakage.
- **`evals/tests/test_select_tasks.py` (4 tests):**
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
collected 15 items

evals/tests/test_runner.py::CredentialBoundaryTests::test_clean_environment_passes_probe PASSED [  6%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_get_sanitized_environment_strips_all_keys PASSED [ 13%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_is_sensitive_env_var_detection PASSED [ 20%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_probe_detects_api_key_in_child_process PASSED [ 26%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_probe_fails_closed_when_jev_disable_env_scrub_is_set PASSED [ 33%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_probe_fails_closed_when_run_command_leaks_variable PASSED [ 40%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_probe_with_parent_secrets_reports_clean_via_run_command PASSED [ 46%]
evals/tests/test_runner.py::CredentialBoundaryTests::test_run_record_credential_boundary_verified_field_in_schema PASSED [ 53%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_executes_three_non_scored_tasks_end_to_end PASSED [ 60%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_fails_closed_when_credential_leaked PASSED [ 66%]
evals/tests/test_runner.py::DryRunExecutionTests::test_dry_run_makes_no_network_or_api_calls PASSED [ 73%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_canonical_integer_sort_order PASSED [ 80%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_deterministic_reproduction PASSED [ 86%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_output_equals_task_ids_selected_in_pilot_manifest PASSED [ 93%]
evals/tests/test_select_tasks.py::SelectTasksTests::test_smoke_tasks_excluded PASSED [100%]

============================= 15 passed in 7.48s ==============================
```

---

## 3. Mechanical vs Prompt-Only Enforcement

- **Mechanically Enforced:**
  - Probe execution through `src/jev/process_runner.run_command` invoking `get_scrubbed_env()`: Environment variables matching sensitive keywords (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`) or absent from the allowlist `SAFE_ENV_NAMES` are stripped mechanically before `CreateProcessW`/`exec`.
  - Refusal to start: `execute_dry_run` fails closed and does not execute any task if `check_credential_boundary()` returns `False`.
  - Schema constraint: `evals/schemas/run.schema.json` conditional validation rule requires `execution.credential_boundary_verified: true` when `status: completed`.
  - `JEV_DISABLE_ENV_SCRUB=1` detection: If the opt-out flag is present, unscrubbed variables leak into probe output and cause `check_credential_boundary` to fail closed.
  - Frozen pilot tasks: Checked by unit test asserting exact equality with seed 42 reproduction.
- **Prompt-Only / Policy:**
  - Evaluation manifest status documentation (`ready_for_scored_mbpp_plus`).
  - Budget caps and maximum task limits described in markdown.

---

## 4. Unrequested Changes

**NONE.**
- `src/` was not modified in any way (`git diff master -- src` is empty).
- `master` was not modified or pushed to.
- All modifications are strictly confined to `evals/`, `docs/phases/evals-ready.md`.
