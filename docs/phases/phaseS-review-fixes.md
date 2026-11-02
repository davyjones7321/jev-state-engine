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

## Fix 5: Never dead-end a repo (compile gate)

### 1. Files and Lines Changed

- `src/jev/workspace.py`:
  - Lines 692–696: In `JsTsCompileHandler.prepare_and_get_command`, when no tsconfig, typecheck, or build script exists, check whether any touched files are TypeScript (`.ts`, `.tsx`). If not, return `CompileOutcome.EXEMPT` rather than `NO_COMPILE_COMMAND`.
  - Lines 969–973: In `RubyCompileHandler.prepare_and_get_command`, return `CompileOutcome.EXEMPT` instead of `NO_COMPILE_COMMAND`.
  - Lines 988–992: In `PhpCompileHandler.prepare_and_get_command`, return `CompileOutcome.EXEMPT` instead of `NO_COMPILE_COMMAND`.
  - Lines 1074–1101: Added `resolve_compile_command` function providing direct manifest-based compile command resolution returning `CompileOutcome.EXEMPT` for optional compilation languages.
  - Lines 4501–4515: In `check_compile`, fallback for optional languages (`ruby`, `php`, `javascript`, `python`) without build commands returns `CompileOutcome.EXEMPT`.
  - Lines 4992–5015: In `run_mechanical_checks`, when compile outcome is `CompileOutcome.ENV_NOT_READY`, attach structured `MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.MISSING_TOOL)`.
- `src/jev/engine.py`:
  - Lines 3971–3983: In `node_gate`, when compile halts as `env_not_ready` with a missing tool, attach `MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=MechReason.MISSING_TOOL)` to `mech_ev`.
  - Lines 4767–4875: In `node_verify`, handle compile check outcomes:
    - `FAILED`: fails verification and escalates deadlock.
    - `ENV_NOT_READY`: records `UNMEASURED(reason=MISSING_TOOL)` (or `ENVIRONMENT_UNAVAILABLE`) in ledger, fails verification and escalates deadlock.
    - `NO_COMPILE_COMMAND`: degrades to UNMEASURED (`MechReason.NO_COMMAND`, zero strikes) and continues to tests and Jev rather than escalating immediately.
- `tests/test_compile_gate.py`:
  - Lines 1088–1298: Added regression tests:
    - `test_plain_js_repo_compile_exempt`: Plain JS project with no tsconfig/typecheck/build returns `EXEMPT` and completes gate without escalation.
    - `test_ruby_repo_compile_exempt`: Ruby project returns `EXEMPT` and completes gate without escalation.
    - `test_php_repo_compile_exempt`: PHP project returns `EXEMPT` and completes gate without escalation.
    - `test_missing_compiler_produces_unmeasured_missing_tool_in_ledger`: Missing compiler creates `UNMEASURED(reason=MISSING_TOOL)` record in attempt ledger.
    - `test_real_compile_failure_still_strikes`: Real compile failure (Python syntax error) still strikes `mechanical_failure` (strike count 1) without unexpected escalation.

### 2. How It Works

- Languages where compilation is optional (plain JS without tsconfig/build, Ruby, PHP) no longer return `NO_COMPILE_COMMAND`. Their handlers now return `CompileOutcome.EXEMPT`, allowing tickets on these repositories to proceed directly to testing and semantic validation without dead-ending.
- In `node_verify`, if a repo produces `NO_COMPILE_COMMAND`, it does not immediately escalate; it records an UNMEASURED attempt with reason `NO_COMMAND` (zero strikes) and continues to the test execution step and Jev evaluation.
- When compilation halts with `ENV_NOT_READY` due to a missing tool or executable, `run_mechanical_checks`, `node_gate`, and `node_verify` capture structured `MechanicalEvidence` with `mech_status=UNMEASURED` and `reason=MISSING_TOOL`, ensuring a clear diagnosis record is committed to the attempt ledger rather than a silent fall-through.

### 3. Rule Enforcement Table

| Rule | Mechanically Enforced | Prompt-Only | Test Name |
| :--- | :--- | :--- | :--- |
| Plain JS repos without tsconfig/build return `EXEMPT` and complete compile gate | Yes | No | `tests/test_compile_gate.py::test_plain_js_repo_compile_exempt` |
| Ruby repos return `EXEMPT` and complete compile gate | Yes | No | `tests/test_compile_gate.py::test_ruby_repo_compile_exempt` |
| PHP repos return `EXEMPT` and complete compile gate | Yes | No | `tests/test_compile_gate.py::test_php_repo_compile_exempt` |
| Missing compiler during compile step produces `UNMEASURED(reason=MISSING_TOOL)` ledger record | Yes | No | `tests/test_compile_gate.py::test_missing_compiler_produces_unmeasured_missing_tool_in_ledger` |
| Real compile failure still strikes | Yes | No | `tests/test_compile_gate.py::test_real_compile_failure_still_strikes` |
| Verify stage degrades `NO_COMPILE_COMMAND` to UNMEASURED without early deadlock escalation | Yes | No | `tests/test_compile_gate.py::test_verify_stage_compile_failure` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (1029 passed, 24 skipped, 0 failed in 1274.17s).

## Fix 6 (Phase 3b): Show and Bind the Recipe (proposals.py, repo_config.py, process_runner.py)

### 1. What Changed (Files and Lines)

- `requirements.txt`:
  - Added `pyyaml==6.0.3`.
- `src/jev/repo_config.py`:
  - Line 8: Imported `re`.
  - Lines 303–346: Defined `EXACT_MAKEFILE_TARGETS = ("test", "tests", "check", "run-tests", "test-all")` and implemented `extract_makefile_target_and_recipe(content: str) -> Tuple[Optional[str], List[str]]` which matches only exact target names (so `latest:` is never matched) and extracts indented recipe lines without executing anything.
  - Lines 362, 383: Added `recipe_hash: Optional[str] = None` to `TrustRecord` and `RepoConfig`.
  - Lines 411–413: Updated `compute_command_hash` to include `recipe_hash` in its deterministic payload when present.
  - Lines 450, 481: Updated `record_trust` to accept and record `recipe_hash`.
  - Lines 540–563: In `validate_trust`, if a recipe hash was bound at approval, extract the current recipe from the repository's Makefile. If the recipe changed or is missing, remove the trust record file under `JEV_HOME` and return `(False, "Makefile recipe changed: approval invalidated")`.
- `src/jev/proposals.py`:
  - Lines 24, 32: Imported `yaml` and `extract_makefile_target_and_recipe` / `EXACT_MAKEFILE_TARGETS`.
  - Lines 131, 143–144: Updated `compute_proposal_identity_hash` to accept optional `recipe_text` and include it in the identity payload.
  - Lines 168–169: Added `recipe_lines: List[str]` and `recipe_text: Optional[str]` to `Proposal`.
  - Line 186: Updated `Proposal.verify_identity` to pass `recipe_text=self.recipe_text` to `compute_proposal_identity_hash`.
  - Line 243: Updated `save_proposal` to recompute identity hash with `recipe_text=proposal.recipe_text`.
  - Lines 289–292: Updated `inspect_executable` so that on Windows, if `cmd[0]` is `make` and not found on `PATH`, it resolves `mingw32-make`.
  - Lines 297–319: Defined `INSTALL_STEP_PREFIXES` and `is_install_command_line` to ignore install and setup commands (`pip install`, `npm install`, `npm ci`, `cargo install`, `poetry install`, etc.).
  - Lines 340–350: Updated Makefile detection in `build_proposal_from_repo` to use `extract_makefile_target_and_recipe`.
  - Lines 355–404: Updated CI workflow detection in `build_proposal_from_repo` to parse workflows using `yaml.safe_load`, support scalar and `run: |` multi-line blocks, ignore install commands, and leave `${{ ... }}` expressions unexpanded.
  - Lines 494–517: Updated `approve_proposal` to compute `recipe_hash = sha256(recipe_text)` and record it in `RepoConfig` and `TrustRecord`.
  - Lines 546–550: In `format_proposal_screen`, show when `mingw32-make` is resolved for `make` on Windows.
  - Lines 557–562: In `format_proposal_screen`, print extracted Makefile recipe lines under `Makefile Recipe Lines:`.
  - Lines 577–587: In `format_proposal_screen`, print a notice when CI workflow expressions `${{ ... }}` are left unexpanded.
- `src/jev/process_runner.py`:
  - Lines 170–174: In `run_command`, on Windows when `argv_list[0]` is `make` and not on `PATH`, resolve and invoke `mingw32-make`.
- `tests/test_proposals.py`:
  - Lines 607–745: Added unit tests:
    - `test_makefile_latest_before_test_proposes_make_test`: Makefile with `latest:` before `test:` proposes `make test` (never `latest:`).
    - `test_makefile_recipe_shown_on_approval_screen`: Displays extracted recipe lines on proposal review screen.
    - `test_editing_recipe_after_approval_invalidates_trust`: Editing Makefile recipe invalidates trust and removes trust file under `JEV_HOME`.
    - `test_ci_install_line_before_pytest_proposes_pytest`: CI workflow with `pip install` before `pytest` ignores install and proposes `pytest`.
    - `test_ci_multiline_run_block_extracts_test_command`: CI workflow with multi-line `run: |` block extracts test command and skips install lines.
    - `test_ci_workflow_expression_left_unexpanded_and_shown_on_screen`: Leaves `${{ ... }}` expressions unexpanded and flags notice on screen.
    - `test_windows_make_lookup_resolves_mingw32_make`: On Windows, resolves `mingw32-make` when `make` is not on `PATH`.

### 2. How It Works

1. **Makefile Detection & Recipe Extraction**: `extract_makefile_target_and_recipe` only matches exact target names in `("test", "tests", "check", "run-tests", "test-all")`. Targets that happen to contain the word "test" as a substring (such as `latest:`) are ignored. It extracts indented recipe lines without executing them, stores them in `proposal.recipe_lines` and `proposal.recipe_text`, and presents them to the repository owner on the approval screen so they see exactly what commands will be executed.
2. **Cryptographic Binding & Trust Invalidation**: `compute_proposal_identity_hash` binds `recipe_text` into the proposal's SHA-256 identity hash. Upon owner approval, `recipe_hash` is recorded in `.jev/config.json` and in the external `TrustRecord`. If the Makefile is subsequently tampered with or edited, `validate_trust` detects that the current Makefile recipe hash does not match `rec.recipe_hash`, deletes the external trust record file so trust goes completely stale, and rejects execution with `"Makefile recipe changed: approval invalidated"`.
3. **CI Workflow Detection with `yaml.safe_load`**: CI workflows in `.github/workflows/*.yml` are parsed securely using `yaml.safe_load`. Both scalar `run:` and multi-line `run: |` blocks are supported. Setup and dependency installation steps (`pip install`, `npm install`, `cargo install`, etc.) are skipped, and test commands (`pytest`, `check`, `test`) are identified. Any workflow expressions like `${{ matrix.python-version }}` are left unexpanded in `exact_argv`, and a disclaimer notice is printed on the review screen.
4. **Windows Make Resolution**: On Windows systems where GNU `make` is installed as `mingw32-make`, both `inspect_executable` and `process_runner.run_command` fall back to `mingw32-make` when `make` is not directly on `PATH`, and the approval screen notes the resolved binary.

### 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| Makefile detection matches exact target names (`latest:` before `test:` proposes `make test`) | Yes | No | `test_makefile_latest_before_test_proposes_make_test` |
| Makefile recipe lines extracted and displayed on approval screen | Yes | No | `test_makefile_recipe_shown_on_approval_screen` |
| Editing Makefile recipe after approval invalidates trust and removes trust record | Yes | No | `test_editing_recipe_after_approval_invalidates_trust` |
| CI install steps ignored when proposing test command | Yes | No | `test_ci_install_line_before_pytest_proposes_pytest` |
| CI multi-line `run: \|` blocks parsed with test command extracted | Yes | No | `test_ci_multiline_run_block_extracts_test_command` |
| CI `${{ ... }}` expressions left unexpanded and flagged on screen | Yes | No | `test_ci_workflow_expression_left_unexpanded_and_shown_on_screen` |
| Windows `make` lookup falls back to `mingw32-make` | Yes | No | `test_windows_make_lookup_resolves_mingw32_make` |
| All existing proposal and repo config tests remain green | Yes | No | `tests/test_proposals.py`, `tests/test_repo_config.py` |

### 4. Unrequested Changes

- None.

### 5. Open Problems

- None. Full offline test suite passed (1036 passed, 24 skipped, 2 deselected, 0 failed in 1135.80s).
