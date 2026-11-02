# Phase S Report: Security, Secrets, and Advisor Hardening

## 1. What Changed (Files and Lines)

### Files Modified / Created:

- `src/jev/retrieval.py`:
  - Lines 23–52: Replaced partial secret pattern with comprehensive `is_secret_path(path)` matching `.env*`, `*.pem`, `*.key`, `id_rsa`, `credentials.json`, `secrets.json`, `.aws/`, `.kube/`, and `.ssh/`.
  - Lines 85–115: Updated `AstWorkspaceRetriever._python_files` to filter out secret directories and secret files before scanning AST symbols.
  - Lines 245–255: Fixed path confinement in `AstWorkspaceRetriever._confined` to safely strip leading relative path prefixes (`./` or `/`) without stripping the dot from hidden files like `.env`, raising `PermissionError` when accessing any denylisted secret file.
  - Added `is_secret_path` to `__all__`.

- `src/jev/workspace.py`:
  - Lines 37–40: Imported `is_secret_path` from `jev.retrieval` and `redact_secrets_in_text` from `jev.proposals`.
  - Lines 1530–1555: Updated `_resolve_for_read` to block all secret denylist paths before resolving against worktree or repo, raising `PermissionError("Access denied to protected secret path: ...")`.
  - Lines 1590–1610: Hardened `run_read_tool` (`cat`, `head`, `tail`) to inspect every argument against `is_secret_path`, returning `Error: Access denied to protected secret file: ...` on match.
  - Lines 1620–1640: Hardened `stage_file_mutation` to reject modifying secret paths, raising `PermissionError`.
  - Lines 2315–2335: In `get_staged_diff` and `get_cumulative_diff`, wrapped diff text with `redact_secrets_in_text`.
  - Lines 4520–4540: In `_record_test_run`, wrapped `stdout_tail` and `stderr_tail` with `redact_secrets_in_text`.

- `src/jev/engine.py`:
  - Lines 38–41: Imported `is_secret_path` from `jev.retrieval` and `redact_secrets_in_text`, `redact_data_structure` from `jev.proposals`.
  - Lines 1400–1475: Added `strip_raw_reasoning` and `exclude_raw_reasoning_data` helpers. In `node_investigate`, protected `list_dir`, `grep`, and `read_file` read tools against secret denylist paths.
  - Lines 2520–2560: In `node_implement`, protected `_resolve_implementation_read_path`, `list_dir`, `grep`, `inspect_structure`, and `read_file` against secret denylist paths.
  - Lines 2300–2325: Stripped raw reasoning (`cot`, `chain_of_thought`, `<think>`) and redacted secrets from worker prompts in `prompt_text`.
  - Lines 4830–4870: In `_invoke_advisory`, stripped raw reasoning and redacted secrets from ticket, investigation notes, mechanical detail, and diff before constructing `AdvisoryRequest`.

- `src/jev/gatekeeper.py`:
  - Lines 12–14: Imported `redact_data_structure` from `jev.proposals`.
  - Lines 27–32: Removed default `Path(".env")` from `Gatekeeper.__init__`; `env_file` defaults to `None` and is only loaded if explicitly passed. Never reads `.env` from target repo or current directory.
  - Lines 70–80: In `_post_with_retry`, sanitized outgoing request payloads to Jev using `redact_data_structure(payload)`.
  - Lines 160–185: In `escalate_deadlock`, sanitized deadlock escalation content and log records before writing to `escalation.json` and escalation log files.

- `src/jev/advisory.py`:
  - Lines 42–52: In `_safe_message`, applied `redact_secrets_in_text` to prevent secret leakage in error logs and result messages.
  - Lines 94–106: In `AdvisoryRequest.__init__`, applied `redact_secrets_in_text` to outgoing `prompt` and `system_prompt`.
  - Lines 175–195: In `parse_repair_brief`, enforced strict JSON schema, length bounds, and rejected raw reasoning traces (`<think>`, `cot`, `chain_of_thought`, `reasoning_trace`), raising `ValueError` and dropping invalid outputs.

- `src/jev/providers.py`:
  - Lines 24–45: Added `validate_advisory_timeout(timeout) -> float`, validating timeout is a positive float <= 60.0s (default 20.0s).
  - Lines 80–95: In `OpenAICompatibleProvider.__init__`, enforced `validate_advisory_timeout` on constructor argument or `ADVISORY_TIMEOUT` environment variable.

- `src/main.py`:
  - Lines 112–118: In `build_advisory_service`, read `ADVISORY_TIMEOUT` and validated via `validate_advisory_timeout`.

- `src/jev/ledger.py`:
  - Lines 35–45: Imported `redact_secrets_in_text`, `redact_data_structure` from `jev.proposals` and added `strip_raw_reasoning` and `exclude_raw_reasoning_data`.
  - Lines 120–145: In `_bounded_text`, applied raw reasoning stripping and secret redaction.
  - Lines 195–225, 250–290: In `record_run_start` and `record_attempt`, redacted secrets and stripped reasoning traces from `ticket`, `diff`, `feedback_sent`, and `jev_raw`.

- `src/jev/proposals.py`:
  - Lines 45–54: Expanded `SECRET_VALUE_PATTERNS` to catch credential key-value patterns (`password=...`, `secret=...`, `token=...`, `api_key=...`), bearer tokens, and private keys.

- `deepseek/kaggle_server.py`:
  - Lines 71–85: Set `max_new_tokens = 800`.
  - Lines 120–160: Added `/chat/completions` route returning standard OpenAI-compatible completions response.
  - Lines 203–245: Stripped unclosed `<think>` blocks and raised HTTP 502 with structured error JSON on invalid JSON output in json mode rather than fabricating a brief.

- `.github/workflows/ci.yml`:
  - Lines 70–82: Added clean-environment check step creating a fresh virtual environment, installing `requirements.txt`, and running `pytest --collect-only`.

- `tests/test_phaseS_security_hardening.py`:
  - Lines 1–613: New test suite containing 18 unit and integration tests covering all Phase S specifications.

---

## 2. How It Works

1. **Secret Denylist Across the Pipeline**:
   - `is_secret_path(path)` checks path segments and file names against `.env*`, `*.pem`, `*.key`, `id_rsa`, `credentials.json`, `secrets.json`, `.aws/`, `.kube/`, and `.ssh/`.
   - Enforced in `AstWorkspaceRetriever` (filters symbol scan and denies direct path retrieval with `PermissionError`), `Workspace._resolve_for_read`, `Workspace.run_read_tool` (`cat`, `head`, `tail`), `Workspace.stage_file_mutation`, `engine.node_investigate` (`list_dir`, `grep`, `read_file`), and `engine.node_implement` (`list_dir`, `grep`, `inspect_structure`, `read_file`).

2. **Deterministic Payload Redaction**:
   - Outgoing requests to Jev (`Gatekeeper._post_with_retry`), to the advisor (`AdvisoryRequest`), deadlock escalation logs (`Gatekeeper.escalate_deadlock`), ledger entries (`SqliteLedger`), worktree diffs (`get_staged_diff`, `get_cumulative_diff`), and test output logs (`_record_test_run`) deterministically replace secrets with `[REDACTED]`.

3. **Raw Reasoning Exclusion**:
   - `strip_raw_reasoning` and `exclude_raw_reasoning_data` prune reasoning keys (`cot`, `chain_of_thought`, `reasoning_trace`, `hidden_reasoning`) and `<think>...</think>` tags.
   - Raw reasoning is never persisted into `ledger.db` and never injected into worker prompts or repair packets.

4. **Advisor Brief Validation and Timeout Contract**:
   - `parse_repair_brief` validates JSON structure, checks length caps, and rejects reasoning trace text.
   - On malformed or non-JSON output, `OpenAICompatibleProvider` drops the invalid output and records non-fatal failure without blocking pipeline execution.
   - `ADVISORY_TIMEOUT` is validated to be a positive float <= 60.0s (defaulting to 20.0s).

5. **Safe Environment Handling**:
   - `Gatekeeper` removed the `Path(".env")` default so it never reads `.env` from the current working directory or target repository.

6. **Isolation Verification**:
   - Tested that `src/` never imports `deepseek`, `laya`, `kaggle_server`, or `tri_shadow`.

7. **Kaggle Server Contract**:
   - `/chat/completions` route enabled, unclosed `<think>` tags stripped, `max_new_tokens=800`, and HTTP 502 raised on invalid JSON output without fabricating repair briefs.

8. **WAL Checkpoint Investigation**:
   - In SQLite WAL mode (`PRAGMA journal_mode=WAL`), transactions commit into the `-wal` file. Without an explicit WAL checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`), uncheckpointed dirty pages remain in the WAL. Subsequent read-only connections (like CLI queries) can trigger passive checkpoints or update WAL index headers, mutating the file bytes on disk. Executing `wal_checkpoint(TRUNCATE)` flushes and clears the WAL, ensuring `ledger.db` is stable and subsequent read-only CLI queries do not alter file bytes.

9. **Clean-Environment CI Check**:
   - Added CI step ensuring clean dependencies from `requirements.txt` collect test suites without error.

---

## 3. Rule Enforcement: Enforced by Test vs Only by Prompt

| Rule / Requirement | Enforced By Test | Enforced Only By Prompt | Test Name(s) |
|---|:---:|:---:|---|
| Secret denylist in AstWorkspaceRetriever | Yes | No | `test_retrieval_is_secret_path_denylist`, `test_retrieval_workspace_blocks_secret_files` |
| Secret denylist in Workspace read tools & mutation | Yes | No | `test_workspace_run_read_tool_blocks_secret_denylist`, `test_workspace_stage_file_mutation_blocks_secrets` |
| Secret denylist in Engine investigate node read tools | Yes | No | `test_engine_investigate_read_tools_block_secrets` |
| Secret denylist in Engine implement node read tools | Yes | No | `test_engine_implement_read_tools_block_secrets` |
| Secret payload redaction across text and structured data | Yes | No | `test_payload_redaction_secrets_in_text_and_data_structure` |
| Secret redaction in Gatekeeper outgoing requests | Yes | No | `test_gatekeeper_redacts_outgoing_payload_and_escalation` |
| Secret redaction in deadlock escalation records | Yes | No | `test_gatekeeper_escalate_deadlock_redaction` |
| Secret redaction in diffs and test output tails | Yes | No | `test_workspace_diff_and_test_output_redaction` |
| Raw reasoning stripping and exclusion from prompts | Yes | No | `test_raw_reasoning_stripping_and_exclusion` |
| Raw reasoning exclusion and secret redaction in Ledger | Yes | No | `test_ledger_excludes_reasoning_and_redacts_secrets` |
| Advisor RepairBrief schema validation & reasoning rejection | Yes | No | `test_advisor_repair_brief_schema_and_reasoning_trace_rejection` |
| Advisor provider drops invalid output without blocking | Yes | No | `test_advisor_provider_drops_invalid_output_non_blocking` |
| Advisor timeout validation (default 20s, max 60s) | Yes | No | `test_advisor_timeout_contract` |
| Gatekeeper never loads `.env` by default | Yes | No | `test_gatekeeper_never_loads_dot_env_by_default` |
| Clean isolation of `src/` from forbidden external modules | Yes | No | `test_src_isolation_from_prohibited_modules` |
| Kaggle server `/chat/completions`, token bounds, 502 on invalid JSON | Yes | No | `test_kaggle_server_contract_elements` |
| Only touch files named in Phase S | No | Yes | N/A (Agent constraint) |
| Never merge or push to master/promote | No | Yes | N/A (Agent constraint) |

---

## 4. Skipped Tests and Reasons

- No tests in `tests/test_phaseS_security_hardening.py` were skipped (18/18 passed).
- In the full test suite, 24 pre-existing tests were skipped due to uninstalled native compilers/runtimes on Windows (Go, Maven, .NET, CMake, Swift, Elixir, Ruby, PHP, Terraform, Helm), consistent with previous phases.

---

## 5. Unrequested Changes

- `src/jev/proposals.py`: Expanded `SECRET_VALUE_PATTERNS` regex list to capture bearer tokens, key-value credentials, and private keys needed for payload redaction.

---

## 6. Open Problems

- None. All 18 tests pass cleanly and all 998 offline tests pass without regressions.

---

## 7. Verification Script Output

```
=== Verification Report for base: origin/phase10-worker-validation ===
Branch: phaseS-security-hardening
Commit: f4266f52b0592a931f6180620a9bb8ce9eef2cda

--- Diff stat vs origin/phase10-worker-validation ---
.github/workflows/ci.yml                |  13 +
 deepseek/kaggle_server.py               | 198 ++++++-----
 src/jev/advisory.py                     |  40 ++-
 src/jev/engine.py                       |  63 +++-
 src/jev/gatekeeper.py                   |  13 +-
 src/jev/ledger.py                       |  38 +-
 src/jev/proposals.py                    |   3 +
 src/jev/providers.py                    |  28 +-
 src/jev/retrieval.py                    |  61 +++-
 src/jev/workspace.py                    |  32 +-
 src/main.py                             |   2 +-
 tests/test_phaseS_security_hardening.py | 613 ++++++++++++++++++++++++++++++++
 12 files changed, 964 insertions(+), 140 deletions(-)

--- Test Changes ---
Added tests:
  + test_retrieval_is_secret_path_denylist
  + test_retrieval_workspace_blocks_secret_files
  + test_workspace_run_read_tool_blocks_secret_denylist
  + test_workspace_stage_file_mutation_blocks_secrets
  + test_engine_investigate_read_tools_block_secrets
  + test_engine_implement_read_tools_block_secrets
  + test_payload_redaction_secrets_in_text_and_data_structure
  + test_gatekeeper_redacts_outgoing_payload_and_escalation
  + test_gatekeeper_escalate_deadlock_redaction
  + test_workspace_diff_and_test_output_redaction
  + test_raw_reasoning_stripping_and_exclusion
  + test_ledger_excludes_reasoning_and_redacts_secrets
  + test_advisor_repair_brief_schema_and_reasoning_trace_rejection
  + test_advisor_provider_drops_invalid_output_non_blocking
  + test_advisor_timeout_contract
  + test_gatekeeper_never_loads_dot_env_by_default
  + test_src_isolation_from_prohibited_modules
  + test_kaggle_server_contract_elements

--- Collected Test Counts ---
  tests/test_phaseS_security_hardening.py: 18 tests collected in 0.96s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:4917:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2364:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:5065:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1580:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1617:        if self.worktree_dir == self.repo_dir:

--- Pytest Offline Suite Summary ---
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
998 passed, 24 skipped, 2 deselected, 2 warnings in 850.20s (0:14:10)
```

## Addendum (post-review)

The follow-up review fixes are documented in `docs/phases/phaseS-review-fixes.md`.

- 8a fixed redaction gaps for env-style secret names, bare Google API keys, JWTs, non-HTTP URL credentials, AWS secret-key context, and false-positive counter/key-name matches.
- 8b fixed secret-file denylist gaps for credential manifests, secret directories, package/VCS/container credential files, Terraform state/vars, private-key names, and keystore formats.
