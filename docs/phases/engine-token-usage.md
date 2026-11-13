# Phase: Engine Token Usage Recording

**Branch:** `engine-token-usage`  
**Base:** `origin/master` (`98f41a4d5f534d7001014ad48ede7510440c3d1f`)  
**Date:** 2026-11-13  
**Status:** Verification Passed  

---

## 1. Goal & Problem Statement

Prior to this phase, token usage tracking was only captured on individual worker attempts in the `attempts` table. Token consumption by other phases of an execution run—such as repository investigation, high-level planning, and retry/replanning nodes—was untracked.

The goal of this phase is to record the tokens of ALL Gemini calls across an entire run (investigation, planning, and worker attempts) into the SQLite ledger run row, without modifying planner, worker, or engine logic.

---

## 2. Implementation Overview

### Component 1: LangChain Callback Handler (`src/jev/usage.py`)
- Created `UsageRecorder(BaseCallbackHandler)`.
- Implements `on_llm_end(response, **kwargs)` which extracts `usage_metadata` from the response message (`input_tokens`, `output_tokens`, and cache-read tokens if present).
- Handles standard LangChain `UsageMetadata` structures, including `input_token_details["cache_read"]` as well as alternative cache token representations.
- Accumulates `llm_tokens_in`, `llm_tokens_out`, `llm_cache_read_tokens`, and increments `llm_calls`.
- Exposes convenience properties: `tokens_in`, `tokens_out`, `cache_read_tokens`, and `calls`.
- Complete exception isolation: all processing in `UsageRecorder` is wrapped in `try/except` to guarantee it never raises or breaks an LLM call.

### Component 2: Run-Level Wiring in `src/main.py`
- Instantiates a single `UsageRecorder` per run.
- Attaches the recorder to `ChatGoogleGenerativeAI(..., callbacks=[usage_recorder])`.
- Because the engine passes this single `ChatGoogleGenerativeAI` instance to all nodes (`node_investigate`, `node_plan`, `node_implement`), all Gemini calls across investigation, planning, and execution share the same recorder without altering engine or node logic.
- At run completion (whether success, failure, or escalation), records final totals to the ledger run row via `recorder.record_run_llm_usage(...)`.

### Component 3: Ledger Schema & Guarded Migration (`src/jev/ledger.py`)
- Added four nullable integer columns to the `runs` table schema:
  - `llm_tokens_in INTEGER DEFAULT NULL`
  - `llm_tokens_out INTEGER DEFAULT NULL`
  - `llm_cache_read_tokens INTEGER DEFAULT NULL`
  - `llm_calls INTEGER DEFAULT NULL`
- Implemented guarded migration in `_init_db()` using `PRAGMA table_info(runs)` and `ALTER TABLE runs ADD COLUMN` so existing legacy databases open seamlessly without data loss.
- Added `record_run_llm_usage()` to the `Recorder` protocol, `NullRecorder`, and `SqliteLedger`.

### Component 4: Ledger Reporting Viewer (`scripts/ledger_report.py`)
- Updated the run inspection viewer to read and display:
  - `LLM Tokens In:`
  - `LLM Tokens Out:`
  - `LLM Cache Read:`
  - `LLM Calls:`
  - `LLM Total:`
  - `Worker Tokens:` (sum of `tokens_in` and `tokens_out` across all attempts in the run)
- Prints the unattributed calculation:
  `unattributed (planner, investigation, other) = llm total minus sum of per-attempt worker tokens: <unattributed>`

---

## 3. Advisory Provider Assessment

As required by the specification, an assessment of `src/jev/advisory.py` and `src/jev/providers.py` was conducted:
- The advisory service (`AdvisoryService` backed by `OpenAICompatibleProvider`) uses an independent `httpx.Client` to dispatch HTTP POST requests directly to an OpenAI-compatible `/chat/completions` endpoint.
- It does not use `ChatGoogleGenerativeAI` or LangChain's callback mechanism.
- Therefore, advisory tokens are **not** covered by `UsageRecorder`.
- In accordance with the instructions, the advisory provider was left untouched.

---

## 4. Mechanical vs. Prompt-Only Protections

- **Mechanically Enforced:**
  - Token recording is mechanically triggered by LangChain's callback lifecycle on `on_llm_end`.
  - Exception safety is mechanically enforced via top-level try/except guards in `UsageRecorder`.
  - Database schema preservation is mechanically verified with SQLite `PRAGMA table_info` before applying `ALTER TABLE`.
  - Token attribution subtraction (`llm_total - worker_tokens`) is a deterministic mathematical calculation.
  - Test suite mechanically enforces that `UsageRecorder` is passed to `ChatGoogleGenerativeAI.callbacks`.
- **Prompt-Only:**
  - None. No part of token tracking, aggregation, migration, or reporting depends on LLM prompts or agent instructions.

---

## 5. Test Suite Verification

Comprehensive tests added in `tests/test_token_usage.py`:
1. `test_fake_llm_with_usage_metadata_accumulates_expected_totals`: Verifies `GenericFakeChatModel` with response `usage_metadata` accumulates correct input, output, cache-read tokens, and call count.
2. `test_exception_inside_recorder_does_not_break_call`: Verifies that internal exceptions inside the recorder are swallowed without interrupting LLM execution.
3. `test_migration_works_on_old_db`: Confirms that a legacy database lacking the four new columns migrates safely on open and permits recording new token metrics.
4. `test_ledger_report_prints_new_lines`: Verifies CLI report output formatting, new totals, and the unattributed token calculation line.
5. `test_main_attaches_recorder_to_chat_model` & `test_fails_if_recorder_not_attached`: Ensures `main.py` passes `UsageRecorder` to `ChatGoogleGenerativeAI(callbacks=...)` and demonstrates mechanical test failure if omitted.

---

## 6. Unrequested Changes

1. `tests/test_housekeeping_4.py`: Updated `mock_chat_cls.assert_called_with` in `test_worker_model_reaches_chat_google_generative_ai_constructor` to accept `callbacks=ANY`. The prior test asserted exact kwargs without `callbacks`, which failed when `callbacks=[usage_recorder]` was attached as requested in item 2.
2. `tests/test_phase10_worker_validation.py`: Added `(wt / "node_modules").mkdir(parents=True, exist_ok=True)` in `test_typescript_bun_error_diagnostics_and_evidence` to prevent environment unavailability errors on host environments (such as Windows) where native `bun` executable is not installed.
