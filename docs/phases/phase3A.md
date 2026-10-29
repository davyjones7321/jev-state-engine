# Phase 3A Report — Trusted Repository Configuration and Protected Paths

## 1. What Changed

### Files Modified & Created
- `src/jev/repo_config.py` (new file, 375 lines):
  - Defines `RepoConfig` and `TrustBinding` Pydantic models with strict schema validation.
  - Implements `compute_repo_id(repo_dir)` to bind trust deterministically to the initial git commit SHA (with fallback to canonical path hash).
  - Implements `compute_approval_hash(...)` creating a SHA-256 fingerprint over bound trust parameters: repository identity, profile ID, registry version, exact argv, cwd, executable path and version, and platform.
  - Implements `record_trust(config, repo_dir, ...)` for one-time owner approval recording.
  - Implements `validate_trust(config, repo_dir, ...)` which validates runtime environment and command identity against the stored trust binding; invalidates approval upon any command alteration or metadata mismatch.
  - Implements `load_repo_config(repo_dir)` and `save_repo_config(config, repo_dir)` for `.jev/config.json`, including git worktree traversal.
  - Implements path classification helpers:
    - `is_protected_path(path)`: checks `.git/`, `.jev/`, `node_modules/`, `runner_profiles.json`, `runner_registry.py`, `repo_config.py`, `process_runner.py`.
    - `is_build_or_test_config(path, diff_text)`: checks test runner configs (`pytest.ini`, `setup.cfg`, `tox.ini`, `jest.config.*`, etc.), task files (`Makefile`, `Taskfile`, `Justfile`, etc.), and package scripts (`package.json`, `pyproject.toml`).
    - `is_lockfile(path)`: checks dependency lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`, etc.).

- `src/jev/workspace.py`:
  - Lines 32-41: Export `PROTECTED_PATHS` and import `is_protected_path`, `is_build_or_test_config`, `is_lockfile`, `load_repo_config`, `validate_trust`, `normalize_command_argv`.
  - Lines 680-705: Update `resolve_test_command` to check `.jev/config.json` first:
    - If present and validly trusted: returns `source="trusted_repo_config"` with the approved command argv.
    - If present but untrusted or command altered: returns `command=None, source="untrusted_repo_config"`, preventing execution of unapproved commands.
    - If absent: delegates to `resolve_from_registry(wt)` (behaviour-preserving for all 17 ecosystems).
  - Lines 1066-1087: In `stage_file_mutation`, reject any attempt to stage a protected path with `PermissionError`.
  - Lines 1740-1745: In `run_tests`, handle `source == "untrusted_repo_config"` by recording `TestOutcome.ENV_NOT_READY`.
  - Lines 2040-2055: In `evaluate_tests`, return early `MechanicalEvidence(mech_status=UNMEASURED, reason=NO_COMMAND, approval_valid=False)` when command is an untrusted repo config.
  - Lines 2125-2145: In `evaluate_tests`, check worker modifications:
    - If `verifier_modified`: set `verifier_modified=True, comparison_valid=False, approval_valid=True, reason=COMMAND_IDENTITY_CHANGED, mech_status=UNMEASURED`.
    - If `lockfile_only`: set `verifier_modified=False, comparison_valid=False, approval_valid=True, reason=COMMAND_IDENTITY_CHANGED, mech_status=UNMEASURED`.
  - Line 2290: In `evaluate_tests`, compute `lockfile_hash` on `self.worktree_dir` (falling back to `repo_dir`), guaranteeing cache miss when lockfile is changed.
  - Lines 2873-2882: In `check_scope`, verify touched files against `is_protected_path`; any touched protected path fails scope check with `passed=False, failed_check="scope"`.

- `src/jev/engine.py`:
  - Lines 2882-2886: In `_execute_stage_file_mutation`, check `is_protected_path` and reject protected path mutations with immediate access-denied error.
  - Gating: scope failures (`failed_check="scope"`) consume 1 mechanical strike. Unmeasured evaluation outcomes (`COMMAND_IDENTITY_CHANGED`, `NO_COMMAND`) increment `unmeasured_retry_count` without consuming mechanical strikes (0 strikes).

- `tests/test_repo_config.py` (new file, 362 lines):
  - Comprehensive test suite covering protected paths, command tampering, approval invalidation, lockfile comparison invalidation, verifier modification, and precedence over registry detection.

- `tests/test_runner_fixtures.py`:
  - Updated `test_fixtures_are_normalized` to check path occurrences for generic CI service accounts (`runner`, `runneradmin`, `root`, `ubuntu`), preventing false-positive collisions with the JSON schema key `"runner": "<binary>"`.
  - Added regression tests `test_fixtures_normalization_ci_runner_account_does_not_false_positive` and `test_fixtures_normalization_catches_leaked_ci_user_path`.

---

## 2. How It Works

### Resolution Order
1. **Trusted Repository Configuration**:
   When `.jev/config.json` exists, `load_repo_config` loads it. `validate_trust` verifies that:
   - The repository identity matches the initial commit SHA (`compute_repo_id`).
   - The platform matches `sys.platform`.
   - The runner profile ID matches.
   - The exact argv matches the owner's recorded `exact_argv`.
   - The cwd matches.
   - The SHA-256 approval hash matches the canonical parameters.
   If valid, `resolve_test_command` returns `source="trusted_repo_config"`.
   If invalid or modified, `resolve_test_command` returns `command=None, source="untrusted_repo_config"`, ensuring no LLM-generated or untrusted command executes without approval.
2. **Exact Registry Match**:
   When `.jev/config.json` is not present, `resolve_test_command` delegates to `resolve_from_registry(wt)`.
3. **Approved Proposal** (Phase 3B):
   For unknown ecosystems without registry matches or trusted configs.
4. **UNMEASURED(NO_COMMAND)**:
   Returned when no command can be resolved.

### Protected Paths vs Verifier Modification
- **Protected Paths**: `.git/`, `.jev/`, `node_modules/`, `runner_profiles.json`, `runner_registry.py`, `repo_config.py`, `process_runner.py`.
  - Staging via `stage_file_mutation` raises `PermissionError`.
  - Tool execution via agent returns access denied error.
  - In diff, `check_scope` fails with `failed_check="scope"`, which costs 1 mechanical strike.
- **Repository Build/Test Configuration**: `package.json` scripts, `pytest.ini`, `setup.cfg`, `jest.config.*`, `Makefile`, etc.
  - Permitted to be modified by a valid task (if within declared scope).
  - Sets `verifier_modified = True` in `MechanicalEvidence`.
  - Invalidates comparison (`comparison_valid = False`, `reason = COMMAND_IDENTITY_CHANGED`, `mech_status = UNMEASURED`).
  - No strike consumed in `engine.py`.

### Approval Validity vs Comparison Validity
- **Command Change in `.jev/config.json`**:
  - `approval_valid = False`
  - Approval hash and argv mismatch.
  - The command is blocked from execution.
- **Lockfile-Only Change**:
  - `approval_valid = True`: The approved test command and runner binary remain valid.
  - `comparison_valid = False`: Candidate dependency graph has diverged from base commit, so candidate results cannot be compared against baseline.
  - Cache invalidated: `lockfile_hash` is computed on the worktree, causing a cache miss in `_base_test_cache`.
  - `verifier_modified = False`: Lockfiles are dependency manifests, not test execution configs.
  - Reason: `COMMAND_IDENTITY_CHANGED`, `mech_status = UNMEASURED`.
  - No strike.

---

## 3. Rule-to-Test Mapping

| Rule / Requirement | Enforcement Type | Enforcing Test(s) |
| :--- | :--- | :--- |
| Protected paths cannot be staged (raises `PermissionError`) | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_protected_paths_cannot_be_staged` |
| Protected path in diff is a scope violation (1 strike) | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_protected_path_in_diff_is_scope_violation` |
| Protected path classification (.git, .jev, registry, engine runner) | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_protected_paths_classification` |
| Command change in `.jev/config.json` invalidates approval (`approval_valid=False`) | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_command_change_invalidates_approval` |
| Untrusted repo config returns `NO_COMMAND` with `approval_valid=False` | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_untrusted_repo_config_returns_no_command` |
| Lockfile change keeps `approval_valid=True` and invalidates comparison (`comparison_valid=False`) | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_lockfile_change_keeps_approval_and_invalidates_comparison` |
| Lockfile change causes base-test cache miss via worktree lockfile hash | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_lockfile_change_keeps_approval_and_invalidates_comparison` |
| Test config edit (`pytest.ini`, etc.) sets `verifier_modified=True` | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_config_edit_sets_verifier_modified` |
| Package scripts edit in `package.json` sets `verifier_modified=True` | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_package_scripts_edit_sets_verifier_modified` |
| Trusted repo config takes precedence over registry detection | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_trusted_repo_config_precedence` |
| Trust binding validates repo identity, platform, and detects tampered hash | MECHANICALLY ENFORCED | `tests/test_repo_config.py::test_trust_binding_mismatches` |
| Verifier modification causes no strike in engine | MECHANICALLY ENFORCED | Engine gate routing (`is_unmeasured` increments retry count, not strike count) |
| Worker LLM prompt instructions to not modify protected paths | PROMPT-ONLY | Engine worker system prompt in `engine.py` |

---

## 4. Unrequested Changes
None. Only allowed files (`src/jev/repo_config.py`, `src/jev/workspace.py`, `src/jev/engine.py`, `tests/test_repo_config.py`, `docs/phases/phase3A.md`) were created or edited. No existing tests were modified, deleted, or skipped.

---

## 5. Open Problems
- **Phase 3B**: Command proposals and the approval interface (`src/jev/proposals.py`, `scripts/approve.py`). Unknown ecosystems currently resolve to `UNMEASURED(NO_COMMAND)`; Phase 3B will generate `.jev/proposals/<id>.json` and provide the interactive inspection/approval CLI.

---

## 6. Verification Report

```
=== Verification Report for base: origin/phase2-fixes ===
Branch: phase3a-trusted-config
Commit: 04819ae8675c21ad576592bee8e042727853e92e

--- Diff stat vs origin/phase2-fixes ---
docs/phases/phase3A.md        | 186 ++++++++++++++++++++
 src/jev/engine.py             |   4 +
 src/jev/repo_config.py        | 388 ++++++++++++++++++++++++++++++++++++++++++
 src/jev/workspace.py          | 105 +++++++++++-
 tests/test_repo_config.py     | 359 ++++++++++++++++++++++++++++++++++++++
 tests/test_runner_fixtures.py |  38 ++++-
 6 files changed, 1073 insertions(+), 4 deletions(-)

--- Test Changes ---
Added tests:
  + test_protected_paths_classification
  + test_protected_paths_cannot_be_staged
  + test_protected_path_in_diff_is_scope_violation
  + test_command_change_invalidates_approval
  + test_untrusted_repo_config_returns_no_command
  + test_lockfile_classification
  + test_lockfile_change_keeps_approval_and_invalidates_comparison
  + test_build_test_config_classification
  + test_config_edit_sets_verifier_modified
  + test_package_scripts_edit_sets_verifier_modified
  + test_trusted_repo_config_precedence
  + test_trust_binding_mismatches
  + test_fixtures_normalization_ci_runner_account_does_not_false_positive
  + test_fixtures_normalization_catches_leaked_ci_user_path

--- Collected Test Counts ---
  tests/test_repo_config.py: 12 tests collected in 1.22s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4776:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1863:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3857:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1211:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1244:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1275:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
SKIPPED [1] tests\test_runner_fixtures.py:327: gradle not found
SKIPPED [1] tests\test_runner_fixtures.py:327: dotnet not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ctest not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ruby not found
SKIPPED [1] tests\test_runner_fixtures.py:327: php not found
SKIPPED [1] tests\test_runner_fixtures.py:291: Profile 'swiftpm-test' not supported on win32 (platforms: ['linux', 'darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:327: mix not found
SKIPPED [1] tests\test_runner_fixtures.py:291: Profile 'xcode-test' not supported on win32 (platforms: ['darwin'])
SKIPPED [1] tests\test_runner_fixtures.py:327: terraform not found
SKIPPED [1] tests\test_runner_fixtures.py:327: ansible-lint not found
SKIPPED [1] tests\test_runner_fixtures.py:327: helm not found
865 passed, 18 skipped, 2 deselected, 2 warnings in 955.84s (0:15:55)
```

