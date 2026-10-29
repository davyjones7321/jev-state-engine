# Phase 3A Fixes Report: External Trust Store, Pass Preservation, & Structural Config Parsing

## 1. Executive Summary & Overview

Phase 3A Fixes addresses five key architectural and reliability requirements for trusted repository configuration:
1. **Trust Lives Outside the Repo**: Trust records are isolated in `<JEV_HOME>/trust/<repo_id>.json` (default `~/.jev/trust/<repo_id>.json`). Any embedded `"trust"` field within `.jev/config.json` is completely ignored and flagged via `ignored_embedded_trust = True`. Untrusted repo configs fail-closed to `NO_COMMAND` with reason `"untrusted_repo_config: run scripts/trust_repo.py"`.
2. **Interactive CLI Tool (`scripts/trust_repo.py`)**: A standalone script that inspects declared commands, displays each argument on its own line, details the working directory, runner profile, and binary executable path and version, and securely records owner trust under `JEV_HOME` upon explicit confirmation (or `--yes`). It never executes the test command.
3. **Pass Preservation on Config / Manifest / Lockfile Edits**: In `Workspace.evaluate_tests`, a candidate worktree test run that exits 0 is preserved as `PASS`, even if test config, lockfiles, or manifests changed. Evidence fields (`verifier_modified`, `test_surface_changed`, `lockfile_changed`) are recorded, and the outcome is marked `mechanically_unverified = True` with `unverified_kind = "verifier_modified"`. Only failing runs are categorized into `UNMEASURED(COMMAND_IDENTITY_CHANGED)`, `UNMEASURED(TEST_SURFACE_CHANGED)`, or `UNMEASURED(BASELINE_UNAVAILABLE)`.
4. **Structural Config Parsing**: `is_build_or_test_config` performs structural diffing between base and candidate files. For `package.json`, only modifications to `"scripts"`, `"workspaces"`, and runner configuration keys (`jest`, `vitest`, `mocha`, `ava`) trigger verifier modifications (dependency bumps, descriptions, version changes do not). For `pyproject.toml`, only `[tool.pytest*]`, `[project.scripts]`, and `[tool.poetry.scripts]` trigger verifier modifications. Dead `diff_text` branches are removed, and unparseable files conservatively count as modified.
5. **Real Integration Tests**: Real Git repositories and subprocess test runs verify all behavior end-to-end without mocks.

---

## 2. Rule Implementations

### Rule 1: Trust Lives Outside the Repo
- **Location**: Trust files are stored exclusively in `<JEV_HOME>/trust/<repo_id>.json`. The environment variable `JEV_HOME` dictates the storage root (falling back to `~/.jev`).
- **Embedded Trust Stripped**: `RepoConfig` parsing in `src/jev/repo_config.py` ignores any `"trust"` field in `.jev/config.json`, marking `ignored_embedded_trust = True`.
- **Trust Record Schema**: `TrustRecord` binds:
  - `repo_id`: Canonical repository identifier.
  - `command_hash`: SHA-256 digest of normalized command fields (`runner_profile_id`, `test_command`, `cwd`, `env`).
  - `profile_id`: Runner profile ID.
  - `registry_version`: Version of the runner registry.
  - `argv`: The exact command arguments list.
  - `cwd`: Configured working directory.
  - `platform`: Operating system platform identifier.
  - `executable_path`: Resolved absolute binary path if detected.
  - `executable_version`: Binary version string if detected.
  - `approved_at`: ISO timestamp of owner approval.
  - `approved_by`: Identity of the approver (e.g., username or "owner").
- **Resolution**: An untrusted or tampered repository configuration causes `resolve_test_command` to return `source="untrusted_repo_config"` with `command=None` and reason `"untrusted_repo_config: run scripts/trust_repo.py"`.

### Rule 2: Non-Executing Inspection & Approval CLI (`scripts/trust_repo.py`)
- Syntax: `python scripts/trust_repo.py <repo_dir> [--yes] [--approved-by <name>]`
- Inspects `.jev/config.json` without running commands.
- Displays declared commands with one argument per line for clear human auditing:
  ```text
  Declared command arguments:
    [0] pytest
    [1] -q
    [2] tests/
  Working directory: .
  Runner profile: pytest
  Executable path: /path/to/pytest
  Executable version: pytest 8.x
  ```
- Prompts `Approve and trust this repository configuration? [y/N]: ` unless `--yes` is specified.
- Writes validated trust record directly to `<JEV_HOME>/trust/<repo_id>.json`.

### Rule 3: Config Edits Never Discard a Pass
- When candidate tests exit with code 0 (`PASS`):
  - `mech_status` remains `MechStatus.PASS`.
  - Evidence flags `verifier_modified`, `test_surface_changed`, and `lockfile_changed` are recorded based on touched files.
  - `mechanically_unverified` is set to `True` with `unverified_kind = "verifier_modified"`.
  - `comparison_valid` is set to `False`, while `approval_valid` remains `True`.
  - Evidence level is set to `EvidenceLevel.EXIT_ONLY` (satisfying Pydantic validation constraints for mechanically unverified passes).
- When candidate tests fail:
  - Command or build/test config modified -> `UNMEASURED(COMMAND_IDENTITY_CHANGED)`.
  - Shared test config or fixtures modified -> `UNMEASURED(TEST_SURFACE_CHANGED)`.
  - Lockfile modified -> `UNMEASURED(BASELINE_UNAVAILABLE)` with `comparison_valid = False` and `approval_valid` unchanged (`True`).

### Rule 4: Structural `is_build_or_test_config`
- Instead of naive regex or raw diff searching:
  - `package.json`: Parsed using `json.loads`. Only keys `"scripts"`, `"workspaces"`, and runner configuration keys (`"jest"`, `"vitest"`, `"mocha"`, `"ava"`) are compared. Edits exclusively affecting `"dependencies"`, `"devDependencies"`, `"version"`, or `"description"` evaluate to `False`.
  - `pyproject.toml`: Parsed using `tomllib` (Python 3.11+ stdlib). Only sections `[tool.pytest*]`, `[project.scripts]`, and `[tool.poetry.scripts]` are compared. Version bumps or dependency updates evaluate to `False`.
  - Dead `diff_text` branches were removed.
  - Any file that fails structural parsing is treated safely as modified (`True`).

### Rule 5: Real Git Subprocess Integration Testing
- Real Git repositories are initialized and committed via Git subprocesses.
- Real candidate mutations are staged and evaluated through `Workspace.evaluate_tests`.
- Isolation of `$JEV_HOME` per test prevents any test pollution.

---

## 3. Table of Changed Tests in `tests/test_repo_config.py`

| Test Name | Action | Reason for Change |
| :--- | :--- | :--- |
| `test_protected_paths_classification` | Retained | Unchanged; verifies protected path rules for `.jev/` and engine internals. |
| `test_protected_paths_cannot_be_staged` | Retained | Unchanged; verifies `Workspace.stage_file_mutation` blocks protected paths. |
| `test_protected_path_in_diff_is_scope_violation` | Retained | Unchanged; verifies scope checker catches protected path modifications in git diff. |
| `test_lockfile_classification` | Updated | Added comprehensive classification of package lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`). |
| `test_build_test_config_classification` | Updated | Added `tsconfig.json`, `pytest.ini`, `setup.cfg`, `tox.ini`, `vitest.config.ts`, `Justfile`, `Makefile`. |
| `test_shared_test_config_or_fixture_classification` | Added (New) | Verifies classification of `conftest.py`, `tests/fixtures/*`, and test setup scripts for Rule 3. |
| `test_command_change_invalidates_approval` | Replaced | Replaced by `test_external_trust_approval_and_command_invalidation`; trust records are now external in `JEV_HOME/trust/<repo_id>.json`. |
| `test_tampered_repo_config_ignored` | Replaced | Replaced by `test_embedded_trust_is_ignored_and_never_runs`; verifies embedded trust is ignored and command is never executed. |
| `test_config_edit_sets_verifier_modified` | Replaced | Replaced by `test_package_scripts_edit_green_passes_flagged` and `test_tsconfig_edit_green_passes_flagged`; Rule 3 mandates that exit 0 is preserved as `PASS` instead of discarded as `UNMEASURED`. |
| `test_package_scripts_edit_sets_verifier_modified` | Replaced | Replaced by `test_package_scripts_edit_green_passes_flagged` (asserts green -> PASS flagged) and `test_package_scripts_edit_failing_unmeasured_command_identity_changed` (asserts fail -> UNMEASURED). |
| `test_lockfile_change_sets_verifier_modified` | Replaced | Replaced by `test_lockfile_only_green_passes_and_failing_baseline_unavailable`; Rule 3 dictates that lockfile edits on green pass are preserved (`PASS`, `lockfile_changed=True`), and only failing runs become `UNMEASURED(BASELINE_UNAVAILABLE)`. |
| `test_trusted_repo_config_precedence` & `test_trust_binding_mismatches` | Subsumed | Subsumed by `test_external_trust_approval_and_command_invalidation` and `test_trust_file_lives_under_jev_home_never_in_repo` using external `TrustRecord`. |
| `test_embedded_trust_is_ignored_and_never_runs` | Added (New) | Rule 1 & Rule 5: Verifies that `.jev/config.json` containing embedded trust is ignored (`ignored_embedded_trust=True`), test command resolves to untrusted `NO_COMMAND`, and the command never executes. |
| `test_external_trust_approval_and_command_invalidation` | Added (New) | Rule 1 & Rule 5: Verifies external trust recorded under `JEV_HOME` enables resolution, while tampering with command fields invalidates approval. |
| `test_trust_file_lives_under_jev_home_never_in_repo` | Added (New) | Rule 1 & Rule 5: Asserts trust file resides under `JEV_HOME/trust/<repo_id>.json` and never inside the target repository. |
| `test_pyproject_version_bump_green_passes_unflagged` | Added (New) | Rule 4 & Rule 5: Verifies structural `pyproject.toml` parsing; version bump + green test yields `PASS` with `verifier_modified=False`. |
| `test_package_scripts_edit_green_passes_flagged` | Added (New) | Rule 3 & Rule 5: Verifies `package.json` scripts edit with green test yields `PASS` with `verifier_modified=True` and `mechanically_unverified=True`. |
| `test_package_scripts_edit_failing_unmeasured_command_identity_changed` | Added (New) | Rule 3 & Rule 5: Verifies `package.json` scripts edit with failing test yields `UNMEASURED(COMMAND_IDENTITY_CHANGED)`. |
| `test_dependency_only_package_json_edit_green_passes` | Added (New) | Rule 4 & Rule 5: Verifies structural `package.json` parsing; dependency-only change yields `PASS` with `verifier_modified=False`. |
| `test_lockfile_only_green_passes_and_failing_baseline_unavailable` | Added (New) | Rule 3 & Rule 5: Verifies lockfile edit on green yields `PASS` (`lockfile_changed=True`), and on failure yields `UNMEASURED(BASELINE_UNAVAILABLE)` with `comparison_valid=False`. |
| `test_tsconfig_edit_green_passes_flagged` | Added (New) | Rule 3 & Rule 5: Verifies `tsconfig.json` edit on green yields `PASS` flagged with `verifier_modified=True`. |
| `test_shared_test_fixture_edit_failing_unmeasured_test_surface_changed` | Added (New) | Rule 3 & Rule 5: Verifies shared fixture edit with failing test yields `UNMEASURED(TEST_SURFACE_CHANGED)`. |
| `test_trust_repo_script_cli` | Added (New) | Rule 2 & Rule 5: Verifies `scripts/trust_repo.py --yes` records trust externally without executing the command. |

---

## 4. Rule-to-Test Mapping Table

| Requirement / Rule | Verification Mechanism | Test Name(s) in `tests/test_repo_config.py` |
| :--- | :--- | :--- |
| **Rule 1: External Trust Storage** | Trust records saved to `<JEV_HOME>/trust/<repo_id>.json`. | `test_trust_file_lives_under_jev_home_never_in_repo`, `test_external_trust_approval_and_command_invalidation` |
| **Rule 1: Embedded Trust Ignored** | Embedded `"trust"` in `.jev/config.json` ignored, flags `ignored_embedded_trust`, resolves `NO_COMMAND`. | `test_embedded_trust_is_ignored_and_never_runs`, `test_external_trust_approval_and_command_invalidation` |
| **Rule 1: Command Change Invalidates** | SHA-256 mismatch of command fields invalidates external trust record. | `test_external_trust_approval_and_command_invalidation` |
| **Rule 2: Trust CLI Tool** | CLI inspects command, prints args, writes trust record under `JEV_HOME`, never runs command. | `test_trust_repo_script_cli` |
| **Rule 3: Config Pass Preservation** | Green test (exit 0) preserved as `PASS` flagged as `mechanically_unverified` (`verifier_modified=True`). | `test_package_scripts_edit_green_passes_flagged`, `test_tsconfig_edit_green_passes_flagged` |
| **Rule 3: Config Failing Tests** | Failing test with config change produces `UNMEASURED(COMMAND_IDENTITY_CHANGED)`. | `test_package_scripts_edit_failing_unmeasured_command_identity_changed` |
| **Rule 3: Shared Fixture Failing Tests** | Failing test with shared fixture edit produces `UNMEASURED(TEST_SURFACE_CHANGED)`. | `test_shared_test_fixture_edit_failing_unmeasured_test_surface_changed` |
| **Rule 3: Lockfile Green Tests** | Green test with lockfile edit produces `PASS` with `lockfile_changed=True`, `mechanically_unverified=True`. | `test_lockfile_only_green_passes_and_failing_baseline_unavailable` |
| **Rule 3: Lockfile Failing Tests** | Failing test with lockfile edit produces `UNMEASURED(BASELINE_UNAVAILABLE)` with `comparison_valid=False`. | `test_lockfile_only_green_passes_and_failing_baseline_unavailable` |
| **Rule 4: Structural `package.json`** | Dependencies edit ignored; only `"scripts"`, `"workspaces"`, and test runners trigger verifier modification. | `test_dependency_only_package_json_edit_green_passes`, `test_package_scripts_edit_green_passes_flagged` |
| **Rule 4: Structural `pyproject.toml`** | Version bumps ignored; only `[tool.pytest*]`, `[project.scripts]`, `[tool.poetry.scripts]` trigger. | `test_pyproject_version_bump_green_passes_unflagged` |
| **Rule 5: Subprocess Isolation & Runs** | Real git commits and subprocess test invocations verifying full behavior end-to-end. | All tests in `tests/test_repo_config.py` |

---

## 5. Unrequested Changes

**None.**
Modifications were strictly limited to the five allowed files:
1. `src/jev/repo_config.py`
2. `src/jev/workspace.py`
3. `scripts/trust_repo.py`
4. `tests/test_repo_config.py`
5. `docs/phases/phase3a-fixes.md`

No other files or tests across the repository were modified.

---

## 6. Verification Report Output

```
=== Verification Report for base: HEAD~1 ===
Branch: phase3a-fixes
Commit: 6e8ed0f44f94ef342f0d92e59e98e4d2bf81dcf3

--- Diff stat vs HEAD~1 ---
 docs/phases/phase3a-fixes.md | 196 +++++++++++++++++++++++++++++++++++++++++++
 scripts/trust_repo.py        | 118 +++++++++
 src/jev/repo_config.py       | 370 +++++++++++++++++-------
 src/jev/workspace.py         | 186 ++++++++++---
 tests/test_repo_config.py    | 512 ++++++++++++++++++++++++++----------
 5 files changed, 1091 insertions(+), 291 deletions(-)

--- Test Changes ---
Added tests:
  + test_lockfile_classification
  + test_build_test_config_classification
  + test_shared_test_config_or_fixture_classification
  + test_embedded_trust_is_ignored_and_never_runs
  + test_external_trust_approval_and_command_invalidation
  + test_trust_file_lives_under_jev_home_never_in_repo
  + test_pyproject_version_bump_green_passes_unflagged
  + test_package_scripts_edit_green_passes_flagged
  + test_package_scripts_edit_failing_unmeasured_command_identity_changed
  + test_dependency_only_package_json_edit_green_passes
  + test_lockfile_only_green_passes_and_failing_baseline_unavailable
  + test_tsconfig_edit_green_passes_flagged
  + test_shared_test_fixture_edit_failing_unmeasured_test_surface_changed
  + test_trust_repo_script_cli

--- Collected Test Counts ---
  tests/test_repo_config.py: 17 passed in 84.53s (17 tests collected)

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4776:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:1898:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:3981:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1231:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1264:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1295:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
SKIPPED [1] tests\test_live_substep_7a.py:9: Set RUN_LIVE_PIPELINE=1 to execute live pipeline
SKIPPED [1] tests\test_runner_fixtures.py:361: npm install timed out for jest
SKIPPED [1] tests\test_runner_fixtures.py:361: npm install timed out for mocha
SKIPPED [1] tests\test_runner_fixtures.py:361: npm install timed out for vitest
SKIPPED [1] tests\test_runner_fixtures.py:361: deno not found
SKIPPED [1] tests\test_runner_fixtures.py:361: go not found
SKIPPED [1] tests\test_runner_fixtures.py:361: mvn not found
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
870 passed, 18 skipped, 2 deselected, 2 warnings in 1682.69s (0:28:02)
```
