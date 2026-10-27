# Phase 0A: Close Step 7 Defects

## 1. What Changed (File + Lines)

- **`src/jev/workspace.py`**:
  - **Lines 1303–1307**: Restored original guard in `_discard_engine_touched`: `if self.worktree_dir == self.repo_dir: return` (removed `and not getattr(self, "strict_provenance", False)`, ensuring `git checkout/clean` is never run in `repo_dir`).
  - **Lines 1335–1339**: Added guard `if self.worktree_dir == self.repo_dir: return` to public helper `discard_engine_touched()`.
  - **Lines 3274–3279**: `check_compile` scope filtering supports directory-prefix scopes (`any(Path(f).as_posix().startswith(f"{s.rstrip('/')}/") for s in scope_set)`).
- **`src/jev/engine.py`**:
  - **Lines 272–302**: Added `_extract_declarations_and_head(content, max_lines=40)` to extract generic declaration patterns (`export`, `module.exports`, `class`, `def`, `func`, `fn`, `pub`, `public`, `interface`, `type`, `const`, `struct`) with line numbers first, then fill up to 40 lines total from the head of the file.
  - **Lines 450–466**: Updated `_build_source_hints` for referenced files over 40 lines (or where AST retriever is unsupported / yields no structure) to use `_extract_declarations_and_head`.
  - **Lines 2707–2714**: Updated `node_implement` `_validate_changes` `finally:` block to invoke `workspace.discard_engine_touched()`.
- **`tests/test_step7_feedback.py`**:
  - **Lines 507–537**: Added regression test `test_discard_engine_touched_in_repo_dir_preserves_user_files`.
  - **Lines 540–587**: Added `test_check_compile_directory_prefix_scope`.
  - **Lines 590–617**: Added `test_source_hints_referenced_file_over_40_lines_with_license_header`.
- **`scripts/verify_phase.py`**:
  - Added phase transition verification script checking branch/commit, diff stat vs base ref, added/modified tests, collected test counts, key greps, and pytest offline suite summary.

---

## 2. How It Works

1. **Guard Restoration**:
   `Workspace._discard_engine_touched()` and `Workspace.discard_engine_touched()` strictly enforce `if self.worktree_dir == self.repo_dir: return`. This guarantees that git clean/checkout operations never modify or delete untracked user files or user edits in the primary repository, even with `strict_provenance=True`.
2. **Directory-Prefix Scope Matching in `check_compile`**:
   `check_compile(scope=...)` matches files whose normalized posix path is in `scope_set` or starts with `{prefix}/`. This allows directory scopes like `src/` to match `src/index.ts` and `src/a.tsx`, while exact path scopes like `src/a.ts` do not match `src/a.tsx`, and files outside the scope (e.g. `outside/other.ts`) are excluded from compile checks.
3. **Declaration-First Source Hints for Referenced Files**:
   When error outputs reference external files longer than 40 lines (or non-Python files unsupported by `AstWorkspaceRetriever`), `_extract_declarations_and_head` extracts lines matching declaration patterns with line numbers, placing them at the top of the snippet, followed by lines from the file head up to 40 lines total. Even if a file has a lengthy license header, classes, functions, and interfaces are surfaced to the model.

---

## 3. Rule Enforcement: Mechanically Enforced vs. Prompt-Only

| Rule | Enforcement Type | Enforcing Test / Mechanism |
|---|---|---|
| Never run `git checkout/clean` in `repo_dir` (untracked files and edits survive) | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_discard_engine_touched_in_repo_dir_preserves_user_files` |
| `check_compile` scope treats directory entries as prefixes (`src/` matches `src/index.ts`) | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_check_compile_directory_prefix_scope` |
| Exact file scope excludes differing extensions (`src/a.ts` excludes `src/a.tsx`) | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_check_compile_directory_prefix_scope` |
| Referenced files >40 lines extract declarations before file head | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_source_hints_referenced_file_over_40_lines_with_license_header` |
| 4-call cap per implement attempt on `validate_changes` (5th returns skipped) | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_validate_changes_cap_fifth_call_skipped_and_in_trajectory` |
| Worker staged files remain intact after `validate_changes` | **MECHANICALLY ENFORCED** | `tests/test_step7_feedback.py::test_validate_changes_no_artifacts_left_and_worker_staged_intact` |
| Worker advised to run `validate_changes` before submit | **PROMPT-ONLY** | Line 3023 worker system prompt instruction |

---

## 4. Open Problems

- `scripts/verify_phase.py` is formally scheduled in `session-1-v2.md` for Phase 0B, but was authored here to verify Phase 0A and produce the required transition report.
- Pre-existing skipped test in entire suite: `tests/test_verify_untested_pass.py::test_replaces_all_staged_content_without_duplication` (marked `@pytest.mark.skip(reason="Legacy untracked workspace baseline test")`).

---

## 5. Verification Output (`python scripts/verify_phase.py c7b97a2`)

```text
=== Verification Report for base: c7b97a2 ===
Branch: phase0a-close-step7-defects
Commit: c7b97a2c738be55cb91c0eaf06f5481c3161f74a

--- Diff stat vs c7b97a2 ---
 src/jev/engine.py            |  57 +++++++++++++++++----
 src/jev/workspace.py         |   4 +-
 tests/test_step7_feedback.py | 115 +++++++++++++++++++++++++++++++++++++++++++
 3 files changed, 166 insertions(+), 10 deletions(-)

--- Test Changes ---
Added tests:
  + test_discard_engine_touched_in_repo_dir_preserves_user_files
  + test_check_compile_directory_prefix_scope
  + test_source_hints_referenced_file_over_40_lines_with_license_header

--- Collected Test Counts ---
  tests/test_step7_feedback.py: 11 tests collected in 2.50s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4196:            detail_text = (mech_result.detail or "").replace("FULL_SUITE_PASSED", "PASSED_EXIT_ONLY")
  src/jev/engine.py:4741:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1922:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3838:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1273:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1306:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1337:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
src\jev\models.py:38
  D:\edge-downloades\d\intership-projects\jev-state-engine\src\jev\models.py:38: PytestCollectionWarning: cannot collect test class 'TestPolicy' because it has a __init__ constructor (from: tests/test_evidence.py)
    class TestPolicy(str, Enum):
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
757 passed, 1 skipped, 2 deselected, 2 warnings in 725.86s (0:12:05)
```
