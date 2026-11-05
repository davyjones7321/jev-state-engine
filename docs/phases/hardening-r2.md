# Phase R2 Report: Test Scope in Planning

Base ref: `origin/master` (tip: `0a80755`)  
Branch: `hardening-run-a`  
Phase: `R2` (Test scope in planning)

---

## 1. Files and Lines Changed

### 1. New Helper Module
- `src/jev/test_scope.py` (new, 245 lines):
  - `DiscoveryResult`: Dataclass holding `exact: List[str]` and `ambiguous: List[str]` matches.
  - `is_test_path(path_str: str) -> bool`: Heuristic identifying test files across multi-language conventions.
  - `is_directory_or_glob(item: str) -> bool`: Detects directory or wildcard glob patterns in scope items.
  - `discover_related_tests(worktree_dir, scope_files) -> DiscoveryResult`:
    - Discovers matching test files on disk for declared source files.
    - Ecosystem support: Python, JS/TS, Go, Rust (files + inline `#[cfg(test)]`), Java/Kotlin, .NET, Ruby, PHP, Elixir.
    - Conservative: Classifies into exact vs ambiguous candidates without silent truncation or arbitrary selection.
  - `validate_subgoal_test_scope(subgoals, worktree_dir) -> Tuple[List[Subgoal], List[Dict[str, Any]]]`:
    - Rejects directories and glob patterns.
    - If `expects_tests=False`, accepts and notes event.
    - If scope already names a test file (existing or newly authored), accepts.
    - If no test file in scope, attempts exact discovery.
    - If ambiguous (>1 look-alike or >5 candidates), rejects with explicit problem list.
    - If no test discovered and none named, rejects with `missing_test_scope`.

### 2. Models
- `src/jev/models.py`:
  - Added optional field `no_tests_reason: Optional[str] = None` to `Subgoal`.

### 3. Engine Planning Integration
- `src/jev/engine.py`:
  - Planner prompt updated (~lines 2310): Added instruction that when `expects_tests` is true, relevant test file paths must be included in scope alongside source files (prompt-only guidance).
  - Validation hook (~lines 2370–2385): Called `validate_subgoal_test_scope` immediately following JSON parsing and plan grounding validation.
  - Retry & Escalation (~lines 2440–2450):
    - On first validation failure, formats structured error feedback and prompts planner to retry.
    - On retry failure, if due to test scope issues, escalates with `gate_status="missing_test_scope"` and records trajectory event `missing_test_scope` before implementation starts.
  - Telemetry (~lines 2465–2475):
    - Emits `no_tests_declared` trajectory events for subgoals with `expects_tests=False`.

### 4. Tests
- `tests/test_hardening_r2_test_scope.py` (new, 385 lines):
  - 9 comprehensive unit and integration tests covering all R2 acceptance criteria.

---

## 2. How It Works

1. **Mechanical Enforcement Over Prompting**:
   - The planner prompt instructs the model to include test files in scope, but the engine does not rely on model compliance.
   - `validate_subgoal_test_scope` mechanically checks every subgoal before execution begins.

2. **Conservative Multi-Ecosystem Discovery**:
   - When a subgoal touches code files and expects tests, but does not explicitly name a test file, the validator scans for existing test files corresponding to the source files:
     - **Python**: `tests/test_calc.py`, `calc_test.py`
     - **TypeScript/JavaScript**: `button.test.tsx`, `button.spec.ts`, `__tests__/`
     - **Go**: `calc_test.go` in the same package directory
     - **Rust**: `tests/*.rs` or inline `#[cfg(test)]` inside the source file itself
     - **Java/Kotlin**: `src/test/...` mirror
     - **.NET**: `*Tests.cs`, `*Test.cs`
     - **Ruby**: `spec/*_spec.rb`, `test/*_test.rb`
     - **PHP**: `tests/*Test.php`
     - **Elixir**: `test/*_test.exs`
   - Exact 1-to-1 matches are automatically appended to the subgoal scope, allowing the worker to edit both source and tests.

3. **Ambiguity and Missing Test Gating**:
   - If multiple plausible test candidates exist (or >5 look-alikes), the validator refuses to guess silently.
   - It issues a structured error back to the planner with all candidates listed, requiring the planner to explicitly select the intended test file.
   - If no existing test is discoverable and no new test file is named, the plan is retried once. If the second attempt is still missing test scope, the engine escalates immediately with `gate_status="missing_test_scope"` before any implementation worker runs.

4. **Directory and Glob Rejection**:
   - Blanket directories (`tests`, `tests/`) and globs (`**`, `tests/*`, `*.py`) in scope are strictly rejected, enforcing atomic file path boundaries.

---

## 3. Rule Enforcement Table

| Requirement / Rule | Enforcement Type | Test Name / Mechanism |
| :--- | :--- | :--- |
| Exact test file added automatically to subgoal scope | Mechanical | `tests/test_hardening_r2_test_scope.py::test_1_exact_test_scope_added_for_subgoal` |
| Unrelated test files are never added | Mechanical | `tests/test_hardening_r2_test_scope.py::test_2_unrelated_test_files_never_added` |
| Planner-named new test file preserved for worker creation | Mechanical | `tests/test_hardening_r2_test_scope.py::test_3_planner_names_new_test_file_keeps_it` |
| Missing test scope retries once then escalates before implement | Mechanical | `tests/test_hardening_r2_test_scope.py::test_4_missing_test_scope_escalates_before_implementation` |
| Directory and glob scope entries rejected | Mechanical | `tests/test_hardening_r2_test_scope.py::test_5_scope_entries_as_directories_or_globs_rejected` |
| Ambiguous test candidates never silently selected | Mechanical | `tests/test_hardening_r2_test_scope.py::test_6_ambiguous_and_many_candidates_never_silently_added` |
| `expects_tests=False` accepted with trajectory warning event | Mechanical | `tests/test_hardening_r2_test_scope.py::test_7_expects_tests_false_accepted_with_trajectory_warning` |
| Multi-ecosystem naming conventions supported | Mechanical | `tests/test_hardening_r2_test_scope.py::test_8_multi_ecosystem_naming_conventions` |
| Worker cannot write outside final amended scope | Mechanical | `tests/test_hardening_r2_test_scope.py::test_9_worker_cannot_write_outside_amended_scope` |
| Planner prompt instruction to include test files in scope | Prompt-Only | `src/jev/engine.py` line ~2310 (guidance only; mechanical validation enforces) |

---

## 4. Failing-Then-Passing Test Output

### Failing Output Before Fix (R2.1)
Running `python -m pytest tests/test_hardening_r2_test_scope.py`:
```
=================================== ERRORS ====================================
___________ ERROR collecting tests/test_hardening_r2_test_scope.py ____________
ImportError while importing test module 'tests/test_hardening_r2_test_scope.py'.
Traceback:
tests/test_hardening_r2_test_scope.py:27: in <module>
    from jev.test_scope import (
E   ModuleNotFoundError: No module named 'jev.test_scope'
=========================== short test summary info ===========================
ERROR tests/test_hardening_r2_test_scope.py
============================== 1 error in 1.13s ===============================
```

### Passing Output After Fix (R2.2)
Running `python -m pytest tests/test_hardening_r2_test_scope.py`:
```
============================= test session starts =============================
platform win32 -- Python 3.13.12, pytest-9.1.1, pluggy-1.5.0
rootdir: D:\edge-downloades\d\intership-projects\jev-state-engine
configfile: pytest.ini
collected 9 items

tests\test_hardening_r2_test_scope.py .........                          [100%]

======================== 9 passed, 1 warning in 3.09s =========================
```

---

## 5. Unrequested Changes

- None. Only the following files were modified or created:
  - `src/jev/test_scope.py` (new)
  - `src/jev/models.py` (added optional `no_tests_reason` on `Subgoal`)
  - `src/jev/engine.py` (prompt instruction, validator call, retry/escalate logic, trajectory events)
  - `tests/test_hardening_r2_test_scope.py` (new test suite)
  - `docs/phases/hardening-r2.md` (this report)

---

## 6. Honest Open Problems

1. **Custom / Exotic Monorepo Layouts**: In unconventional monorepos where tests are located in separate packages without direct naming correspondence or standard directory mirroring, automated discovery will safely treat them as missing or ambiguous. The planner must explicitly name the test file in scope or set `expects_tests=false` if exempt.
