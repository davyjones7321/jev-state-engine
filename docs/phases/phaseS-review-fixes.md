# Phase S Review Fixes Report

## Fix 1 (Phase 3b): No Execution Before Approval

### 1. What Changed (Files and Lines)

- `src/jev/proposals.py`:
  - Lines 206–215: Modified `inspect_executable` to resolve executable paths using `shutil.which` only and return `(exe_path, None)`. Removed `subprocess.run([exe_path, "--version"])`.
  - Updated docstrings for `inspect_executable` and `build_proposal_from_repo` to truthfully state that no command or subprocess is ever executed during proposal creation.
- `tests/test_proposals.py`:
  - Line 22: Imported `patch` from `unittest.mock`.
  - Line 37: Imported `inspect_executable` from `jev.proposals`.
  - Lines 470–498: Added unit test `test_proposal_creation_never_starts_subprocess` verifying that `build_proposal_from_repo` never executes the proposed tool with `--version` and `inspect_executable` never invokes any subprocess.

### 2. How It Works

During command proposal extraction in `build_proposal_from_repo`, the candidate executable extracted from CI workflows, Makefiles, manifests, or README instructions is inspected solely via `shutil.which(exe_name)` to determine whether the binary exists on `PATH`. It leaves `tool_version` empty (`None`), avoiding arbitrary command or version flag execution prior to explicit owner review and approval.

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| Proposal creation never executes tool with `--version` before approval | Yes | No | `test_proposal_creation_never_starts_subprocess` |
| `inspect_executable` resolves path via `shutil.which` without subprocess | Yes | No | `test_proposal_creation_never_starts_subprocess` |
| Proposals never execute command without approval | Yes | No | `test_proposals_never_execute_without_approval` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (999 passed, 24 skipped, 0 failed).

## Fix 2 (Phase 3b): Secrets Never Stored Unredacted

### 1. What Changed (Files and Lines)

- `src/jev/proposals.py`:
  - Lines 13, 98: Imported `logging` and configured module logger.
  - Lines 101–117: Implemented `argv_contains_secrets(argv: Sequence[str]) -> bool` to scan all arguments and concatenated command strings against `SECRET_VALUE_PATTERNS` and `redact_secrets_in_text`.
  - Lines 188–234: Updated `save_proposal` to:
    - Check `argv_contains_secrets(proposal.exact_argv)`. If secrets are present, refuse to save the proposal to disk, log a clear warning to pass secrets via environment variables, and return `None` (or raise `ValueError` when `raise_on_secret=True`).
    - Redact secrets across all stored text fields (`source_evidence`, `expanded_package_script`, `tool_path`, `tool_version`, `network_assumptions`, `scope`, `generated_paths`) using `redact_secrets_in_text`.
    - Redact secrets in `environment_variables` using `redact_secrets_in_env`.
    - Recompute `proposal.identity_hash` with the redacted environment variables so that `proposal.verify_identity()` remains valid upon reload.
  - Lines 402–415: Updated `approve_proposal` to check `argv_contains_secrets(proposal.exact_argv)`. If secrets are detected, refuse to approve the proposal by raising `ValueError("Refusing to approve proposal: exact_argv contains secret token or credentials. A command cannot be safely redacted; pass secrets via environment variables instead.")`, preventing writing secrets into `.jev/config.json` or recording them under `JEV_HOME`.
- `tests/test_proposals.py`:
  - Lines 496–598: Added unit tests:
    - `test_secrets_in_exact_argv_refuses_save_and_approval_and_writes_no_secrets`: verifies that commands containing secrets (e.g. `./run-tests.sh --token ghp_abcdefghijklmnopqrstuvwxyz0123456789`) cause `save_proposal` to refuse to save, `approve_proposal` to refuse approval, prevent writing `.jev/config.json`, and ensure no file under `repo` or `JEV_HOME` contains the token.
    - `test_save_proposal_redacts_stored_fields_and_env_vars`: verifies that sensitive information in `source_evidence`, `expanded_package_script`, `scope`, and `environment_variables` is redacted with `[REDACTED]` before writing `.jev/proposals/<id>.json`, while preserving `verify_identity()` integrity on load.

### 2. How It Works

Because command lines cannot be safely redacted without breaking command syntax and execution semantics, `save_proposal` and `approve_proposal` refuse to persist or approve proposals whose `exact_argv` contains credential tokens matching known secret patterns (e.g. GitHub personal access tokens, OpenAI keys, bearer tokens, passwords). Users and CI pipelines must pass secrets via environment variables instead. When storing approved or pending proposals, `save_proposal` redacts all diagnostic metadata fields and environment variable dictionaries using `redact_secrets_in_text` and `redact_secrets_in_env`, recalculating `identity_hash` so cryptographic binding remains internally consistent.

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| `save_proposal` redacts secrets in stored text fields and environment variables | Yes | No | `test_save_proposal_redacts_stored_fields_and_env_vars` |
| `save_proposal` recomputes identity hash and preserves `verify_identity()` | Yes | No | `test_save_proposal_redacts_stored_fields_and_env_vars` |
| `save_proposal` refuses to save proposal if `exact_argv` contains secrets | Yes | No | `test_secrets_in_exact_argv_refuses_save_and_approval_and_writes_no_secrets` |
| `approve_proposal` refuses to approve proposal if `exact_argv` contains secrets | Yes | No | `test_secrets_in_exact_argv_refuses_save_and_approval_and_writes_no_secrets` |
| Neither repo nor `JEV_HOME` contains secrets from proposed commands | Yes | No | `test_secrets_in_exact_argv_refuses_save_and_approval_and_writes_no_secrets` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (1001 passed, 24 skipped, 0 failed in 833.45s).

## Fix 3 (Phase 7): Classifier Gaps

### 1. What Changed (Files and Lines)

- `src/jev/workspace.py`:
  - Lines 136–161: Enhanced `is_shared_test_surface` to recognize helper and utility files by name (`testing_utils.py`, `test_util*`, `testutils*`, `test_helpers*`) as shared test surface. For files named `test_utils`, if candidate/base content explicitly defines actual test cases (`def test_...`), it allows targeted test classification for a utils module.
  - Lines 165–183: Updated `is_test_file` to:
    - Accept optional `content: Optional[str] = None` and forward it to `is_shared_test_surface(path, candidate_content=content)`.
    - Recognize C# test conventions: files ending in `test.cs` / `tests.cs` (e.g. `FooTests.cs`, `*Test.cs`, `*Tests.cs`) and files under directory paths ending in `.Tests` or `.Test` (e.g. `Foo.Tests/FooTests.cs`).
  - Lines 186–203: Added `target_matches_authored_test(target_path: Union[str, Path], authored_path: Union[str, Path]) -> bool` to perform normalized path comparison: checks exact path equality or verifies that the target path is a parent directory of the authored test file.
  - Lines 1762: Updated `classify_touched_paths` to pass `content=self._get_candidate_content(posix_p)` to `is_test_file`.
  - Line 2766: Replaced loose bidirectional substring matching `auth_t in tp or tp in auth_t` in `is_worker_authored_only` determination with `target_matches_authored_test(tp, auth_t)`.
- `tests/test_phase7_test_surface_integrity.py`:
  - Line 46: Imported `target_matches_authored_test`.
  - Lines 476–542: Added unit tests:
    - `test_csharp_test_file_classifier`: tests `is_test_file("Foo.Tests/FooTests.cs")`, `*Test.cs`, `*Tests.cs`, `.Tests/` and `.Test/` folders, and negative non-test cases.
    - `test_helper_files_classified_as_shared_test_surface`: tests `tests/testing_utils.py`, `test_util*`, `testutils*`, and standard authored test files.
    - `test_target_matches_authored_test_normalized_equality_and_parent_dir`: tests exact path equality, parent directory matching, and negative tests for similar name substrings.

### 2. How It Works

1. **C# Test Recognition**: .NET test naming conventions typically place test classes in projects/folders ending in `.Tests` or `.Test` (such as `Foo.Tests/FooTests.cs`), with file names ending in `Test.cs` or `Tests.cs`. `is_test_file` now checks both parent component names ending with `.tests`/`.test` and file suffixes ending in `test.cs`/`tests.cs`.
2. **Helper File Classification**: Test helper scripts like `tests/testing_utils.py`, `test_utils.py`, and `testutils.py` do not contain standalone test suites; they provide fixtures and utilities shared across tests. They are categorized as shared test surface rather than authored test files, ensuring edits to them trigger shared surface handling rather than worker-authored test paths.
3. **Targeted Authorship Matching**: Instead of substring matching (`auth_t in tp or tp in auth_t`), which erroneously matches unrelated files sharing prefix substrings (e.g. `test_foo` in `test_foo_extra.py`), `target_matches_authored_test` normalizes directory separators and verifies either exact path equality or directory-ancestor hierarchy (`authored_path.startswith(target_dir + "/")`).

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| `is_test_file("Foo.Tests/FooTests.cs")` and C# conventions return True | Yes | No | `test_csharp_test_file_classifier` |
| Helper files (`testing_utils.py`, `test_util*`, `testutils*`) count as shared test surface | Yes | No | `test_helper_files_classified_as_shared_test_surface` |
| Author matching requires normalized path equality or parent directory containment | Yes | No | `test_target_matches_authored_test_normalized_equality_and_parent_dir` |
| Similar test names do not falsely match as worker-authored targets | Yes | No | `test_target_matches_authored_test_normalized_equality_and_parent_dir` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (1004 passed, 24 skipped, 0 failed in 866.57s).

## Fix 4 (Phase 4): Preflight False Positives

### 1. What Changed (Files and Lines)

- `src/jev/preflight.py`:
  - Lines 79–90: Removed `-d` and `-w` from `WATCH_STANDALONE_FLAGS`, preserving unambiguous flags (`--watch`, `--watchAll`, `--watch-all`, `--watch-files`, `--live`, `--hot`, `--daemon`, `--serve`).
  - Lines 92–104: Added `INTERACTIVE_SHELLS = {"python", "python3", "node", "irb", "bash", "sh", "zsh", "cmd", "powershell", "pwsh"}`. Removed `-i` from `INTERACTIVE_FLAGS`, keeping unambiguous flags (`--interactive`, `-it`, `--prompt`).
  - Lines 312–330: Scoped `-i` interactive flag rejection in `check_non_interactive_shape` to invocations of programs in `INTERACTIVE_SHELLS` only. Commands like `jest -i` (run in band) and `make -i` (ignore errors) are no longer flagged as interactive.
  - Lines 374–387: In `check_watch_daemon_indicators`, updated standalone `-w` handling to inspect whether `-w` is followed by an integer (worker count, e.g. `jest -w 2`), preventing worker specification flags from triggering watch mode errors while maintaining watch mode rejection for standalone `-w` without a worker count. Removed `-d` so flags like `pytest -d` (xdist) are no longer flagged.
- `tests/test_preflight.py`:
  - Lines 25–26: Imported `check_non_interactive_shape` and `check_watch_daemon_indicators`.
  - Lines 405–455: Added comprehensive test tables:
    - `test_preflight_false_positives_cleared`: verifies that formerly misflagged valid commands (`jest -i`, `make -i`, `pytest -d`, `jest -w 2`, `jest -w 4`, `pytest tests/`, `make test`) pass both `check_non_interactive_shape` and `check_watch_daemon_indicators` in `IN_PROFILE`.
    - `test_preflight_true_positives_still_flagged`: verifies that actual interactive flags (`python -i test.py`, `python3 -i`, `node -i`, `bash -i`, `jest --interactive`) and watch/daemon modes (`jest --watch`, `jest --watchAll`, `pytest --watch-all`, `pytest --daemon`, `vitest --serve`, `npm test -- --watch`, `pytest --watch=true`, `pytest -w`) remain strictly rejected as `OUTSIDE_PROFILE`.

### 2. How It Works

Single-letter flags `-i`, `-w`, and `-d` have conflicting meanings across tools:
- In test runners and build tools, `-i` often means "run in band" (`jest`) or "ignore errors" (`make`), whereas in REPL shells (`python`, `bash`, `node`) `-i` drops into an interactive shell. Preflight now inspects the program executable name and only flags `-i` when invoking an interactive shell or interpreter.
- `-w` in Jest represents worker count when followed by a number (e.g. `jest -w 2` is `--maxWorkers 2`). Preflight checks the subsequent token to avoid misclassifying worker specifications as watch loops.
- `-d` in pytest distributes tests under `pytest-xdist`. Removing `-d` from standalone daemon flags prevents valid distributed test runs from being blocked, while unambiguous `--daemon`, `--watch`, `--watchAll`, and `--live` flags continue to be caught.

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| `jest -i` (run in band) and `make -i` (ignore errors) not flagged | Yes | No | `test_preflight_false_positives_cleared` |
| `pytest -d` (xdist) and `jest -w 2` (workers) not flagged | Yes | No | `test_preflight_false_positives_cleared` |
| `-i` scoped to interactive shells (`python`, `node`, `bash`, etc.) | Yes | No | `test_preflight_true_positives_still_flagged` |
| Unambiguous long watch/daemon flags continue to be rejected | Yes | No | `test_preflight_true_positives_still_flagged`, `test_preflight_watch_mode_outside_profile` |
| All existing watch and interactive preflight tests remain green | Yes | No | `test_preflight_watch_mode_outside_profile`, `test_preflight_interactive_command_outside_profile` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (1024 passed, 24 skipped, 0 failed in 943.18s).
