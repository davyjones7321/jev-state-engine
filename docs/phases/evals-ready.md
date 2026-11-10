# Phase Report: evals-ready

**Repository:** `davyjones7321/jev-state-engine`  
**Phase:** evals-ready  
**Base Commit / Ref:** `master` (`d44754dd4e9be038100e47087f98b630b91df54d`, containing `hardening-env-scrub`)  
**Target Branch:** `evals-ready` (derived from `origin/agent-evaluation-poc`)  
**Review Branch for Runtime Changes:** `eval-worktree-inputs` (not merged to master or evals-ready)  
**Date:** 2026-11-10  
**Status:** All tasks completed, offline test suite passed, ready for review.

---

## 1. Summary of Changes

This phase establishes the evaluation framework readiness without altering the core JEV production runtime:

1. **Separation of Runtime Changes onto `eval-worktree-inputs`:**
   - Commits `183935c..42d2b02` (and `463c466`) previously modified `src/jev/engine.py`, `src/jev/workspace.py`, and `tests/test_worktree.py` to seed local inputs into subgoal worktrees and force-stage ignored files.
   - Per `evals/PROTOCOL.md`, runtime changes are prohibited for evaluation convenience.
   - All runtime modifications have been isolated on the standalone review branch `eval-worktree-inputs` and completely reverted on `evals-ready`.
   - `git diff master -- src tests` is completely empty.

2. **Normal Non-Eval Runtime Behavior Impact Statement:**
   - **Exact behavior change for normal non-eval runs: NONE.**
   - In `master` and on `evals-ready`, `src/jev/engine.py`, `src/jev/workspace.py`, and `tests/` are byte-for-byte identical.
   - Normal runs do not target `evals/local/` paths, do not force-stage `.gitignore` entries, and do not alter worktree creation semantics.

3. **Freezing MBPP+ Task IDs & Metadata in Pilot Manifest:**
   - Manifest `evals/manifests/pilot.json` updated with 10 deterministically frozen MBPP+ task IDs:
     `["Mbpp/56", "Mbpp/102", "Mbpp/108", "Mbpp/129", "Mbpp/252", "Mbpp/266", "Mbpp/282", "Mbpp/616", "Mbpp/752", "Mbpp/775"]`
   - Selection methodology: Fixed random seed (`42`) sampled over canonical sorted MBPP+ task IDs (from official release `v0.2.0`), explicitly excluding development smoke tasks `Mbpp/2`, `Mbpp/3`, and `Mbpp/4`.
   - Pinned metadata recorded:
     - Dataset version: `v0.2.0`
     - EvalPlus version: `0.3.1`
     - Evaluator version: `jev-mbpp-evaluator:0.3.1`
     - Selection seed: `42`

4. **GAIA and AssistantBench Status Set to `not_built`:**
   - In `evals/manifests/pilot.json`, candidate entries for `gaia` and `assistantbench` are marked with status `"not_built"`.
   - No mock tasks or synthetic adapters were invented.
   - `evals/README.md` and `evals/PROTOCOL.md` updated so the first scored pilot run consists exclusively of MBPP+ (10 distinct tasks, up to 30 runs total across 3 agents).

5. **Credential-Boundary Check (`evals/runner.py`):**
   - Implemented `check_credential_boundary()`, which spawns an isolated probe child process (`python -c ...`) before any task run.
   - Inspects environment variables for API keys and secrets (`JEV_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `*_API_KEY`, `*_SECRET`, `*_TOKEN`, `*_AUTH*`).
   - If any API key is detected in the child environment, execution fails closed immediately.
   - Result is recorded in the `run.schema.json` compliant field `execution.credential_boundary_verified`.

6. **Dry-Run Mode (`evals/runner.py`):**
   - Implemented `execute_dry_run()` executing the 3 non-scored development tasks (`Mbpp/2`, `Mbpp/3`, `Mbpp/4`) end-to-end.
   - Validates credential boundaries before execution.
   - Uses offline bundled fixtures to synthesize solutions and validates Python syntax via `py_compile`.
   - Guarantees 0 network calls (no socket, HTTP, or remote API connections) and 0 JEV API calls.
   - Emits valid run records meeting `run.schema.json` constraints with `credential_boundary_verified: true` and `dry_run: true`.
   - Integrated into `evals/benchmarks/mbpp_plus/adapter.py` via `dry-run` and `check-boundary` subcommands.

7. **Test Suite for Credential Boundary and Dry-Run (`evals/tests/test_runner.py`):**
   - Unit and integration tests covering:
     - Clean environment boundary checks (passing).
     - Leaked API key detection (`JEV_API_KEY`, etc.) triggering fail-closed errors.
     - Subprocess probe failure handling.
     - End-to-end dry-run execution of all 3 non-scored tasks.
     - Socket/network monkeypatch assertions verifying zero network traffic.
     - `run.schema.json` compliance validation for generated run records.

8. **Unrequested Changes:**
   - **None.** No files outside `evals/` and documentation were modified on `evals-ready`.

---

## 2. File Changes

- `evals/manifests/pilot.json`: Frozen 10 MBPP+ tasks, recorded versions and seed, marked GAIA/AssistantBench as `not_built`.
- `evals/README.md`: Documented 10-task MBPP+ first scored run, `not_built` status for GAIA/AssistantBench, dry-run mode.
- `evals/PROTOCOL.md`: Updated protocol to reflect MBPP+ only for first scored run (10 tasks, 30 runs max).
- `evals/runner.py`: Implemented probe-based credential boundary checking, environment sanitization, and offline dry-run runner.
- `evals/benchmarks/mbpp_plus/adapter.py`: Added CLI subcommands `dry-run` and `check-boundary`.
- `evals/tests/test_runner.py`: 7 comprehensive test functions covering boundary checks and offline dry-run execution.
- `docs/phases/evals-ready.md`: This report.
