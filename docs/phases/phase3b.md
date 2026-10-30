# Phase 3B Report: Command Proposals and Approval Interface

## 1. What Changed (Files and Lines)

### Allowed Files Modified / Created:
- `src/jev/proposals.py` (New file, lines 1–433):
  - Defines the `Proposal` Pydantic model (`id`, `repo_id`, `exact_argv`, `cwd`, `source_evidence`, `expanded_package_script`, `tool_path`, `tool_version`, `platform`, `target_rule`, `scope`, `environment_variables`, `network_assumptions`, `timeout`, `generated_paths`, `identity_hash`, `created_at`, `status`, `approved_at`, `approved_by`).
  - Implements `compute_proposal_identity_hash()`: Computes deterministic SHA-256 hash over canonical JSON representation of proposal fields (`repo_id`, `exact_argv`, `cwd`, `platform`, `env`, `timeout`, `target_rule`).
  - Implements `Proposal.verify_identity()`: Verifies proposal has not been tampered with before approval.
  - Implements secret redaction: `redact_secrets_in_text()`, `redact_secrets_in_env()`, `redact_data_structure()`, redacting known API key patterns (GitHub PAT, OpenAI/Anthropic keys, Bearer tokens, AWS keys, embedded basic auth credentials) and sensitive key names (`*key*`, `*token*`, `*secret*`, `*password*`, `*auth*`, `*credential*`).
  - Implements `format_proposal_screen(proposal: Proposal) -> str`: Formats proposal details for review, with exact argv printed one argument per line, all fields displayed, secrets redacted, and the mandatory notice statement: `"approval authorizes execution, not a mechanical pass"`.
  - Implements `build_proposal_from_repo(repo_dir) -> Optional[Proposal]`: Heuristically discovers test commands from Makefiles, GitHub Actions CI workflows (`.github/workflows/*.y*ml`), package manifests (`package.json`), or README code blocks. Inspects executable path and version without running the test command.
  - Implements `approve_proposal(proposal, repo_dir, approved_by="owner") -> Tuple[RepoConfig, TrustRecord]`: Verifies identity hash, writes `.jev/config.json`, and records external owner trust under `<JEV_HOME>/trust/<repo_id>.json`.
  - Implements storage helpers: `save_proposal()`, `load_proposal()`, `list_proposals()`.

- `scripts/approve.py` (New file, lines 1–95):
  - CLI script implementing the interactive approval interface:
    - `python scripts/approve.py <proposal_id>`: Displays formatted proposal screen and prompts user `Approve proposal? [y/N]`.
    - `--yes`: Skips prompt for non-interactive / automated workflows.
    - `--repo-dir`: Targets specific repository directory.
    - `--approved-by`: Identifies the approver (defaults to `"owner"`).
    - `--list`: Lists all pending and approved proposals.
    - Never executes the proposed command.

- `src/main.py` (Modified, lines 163–169, 319–325):
  - Adds CLI argument `--approved-proposals` / `--proposals` (`choices=["on", "off"], default="off"`).
  - When `--approved-proposals on` and a run encounters `UNMEASURED(NO_COMMAND)`, generates a proposal for the unknown ecosystem, saves it under `.jev/proposals/<id>.json`, and prints the proposal ID and command to review and approve.

- `tests/test_proposals.py` (New file, lines 1–466):
  - Integration test suite for Phase 3B covering all required constraints and behaviors.

---

## 2. How It Works

1. **Unknown Ecosystem Discovery**: When a repository lacks a trusted `.jev/config.json` and cannot be recognized by the built-in runner registry, `build_proposal_from_repo` scans repository artifacts (Makefiles, CI workflows, `package.json`, and README markdown blocks) to assemble a proposed test command and execution environment.
2. **Deterministic Identity Binding**: A SHA-256 identity hash binds `repo_id`, `exact_argv`, `cwd`, `platform`, `env`, `timeout`, and `target_rule`. Any alteration to these fields on disk causes `verify_identity()` to fail, preventing approval or execution of modified commands.
3. **Interactive & Redacted Approval**: `scripts/approve.py` formats all proposal details, enforces secret redaction on all fields, displays each argument on a separate line, and displays the mandatory disclaimer:
   > "approval authorizes execution, not a mechanical pass"
4. **External Trust Binding**: When approved, `approve_proposal` creates the repo config `.jev/config.json` (holding declarations only) and writes the external owner trust record into `<JEV_HOME>/trust/<repo_id>.json`. Subsequent test resolution directly recognizes the repo as trusted via Phase 3A's external trust mechanism.

---

## 3. Rules Enforced by Tests vs Only by Prompts

| Rule / Requirement | Enforcement Type | Test Name |
| :--- | :--- | :--- |
| Proposals never execute without owner approval | Mechanical (Test) | `tests/test_proposals.py::test_proposals_never_execute_without_approval` |
| Unapproved proposal results in `NO_COMMAND` / no host execution | Mechanical (Test) | `tests/test_proposals.py::test_proposals_never_execute_without_approval` |
| Approval screen shows all required fields and exact argv one arg per line | Mechanical (Test) | `tests/test_proposals.py::test_approval_screen_data_complete_and_redacted` |
| Secrets (API keys, tokens, auth headers, passwords) are redacted | Mechanical (Test) | `tests/test_proposals.py::test_approval_screen_data_complete_and_redacted` |
| Disclaimer "approval authorizes execution, not a mechanical pass" is printed | Mechanical (Test) | `tests/test_proposals.py::test_approval_screen_data_complete_and_redacted` |
| Approval writes `.jev/config.json` and external trust under `JEV_HOME` | Mechanical (Test) | `tests/test_proposals.py::test_approval_writes_trusted_config_and_enables_execution` |
| Approval enables execution as `trusted_repo_config` | Mechanical (Test) | `tests/test_proposals.py::test_approval_writes_trusted_config_and_enables_execution` |
| Approval survives subsequent runs on unchanged repository | Mechanical (Test) | `tests/test_proposals.py::test_approval_survives_unchanged_run` |
| Proposal tampering on disk invalidates identity and blocks approval | Mechanical (Test) | `tests/test_proposals.py::test_identity_change_invalidates_proposal_and_approval` |
| Post-approval command tampering in repo config invalidates execution | Mechanical (Test) | `tests/test_proposals.py::test_identity_change_invalidates_proposal_and_approval` |
| Proposals extracted from Makefile test target | Mechanical (Test) | `tests/test_proposals.py::test_proposal_generation_from_makefile` |
| Proposals extracted from GitHub Actions CI workflow | Mechanical (Test) | `tests/test_proposals.py::test_proposal_generation_from_ci_workflow` |
| Proposals extracted from README code blocks | Mechanical (Test) | `tests/test_proposals.py::test_proposal_generation_from_readme` |
| `scripts/approve.py` CLI supports non-interactive (`--yes`) approval | Mechanical (Test) | `tests/test_proposals.py::test_cli_approve_script_interactive_and_yes` |
| CLI `--approved-proposals` flag wiring in `main.py` | Mechanical (Test) | Verified via `build_parser()` and help inspection |
| LLM cannot invent arbitrary commands outside proposals/config | Prompt & Architecture | Architectural enforcement by `resolve_test_command` returning `NO_COMMAND` for any unverified ecosystem |

---

## 4. Unrequested Changes
- **None**. Only files specified in Phase 3B were added or modified (`src/jev/proposals.py`, `scripts/approve.py`, `src/main.py`, `tests/test_proposals.py`, `docs/phases/phase3b.md`).

---

## 5. Open Problems
- **None**: All 9 unit and integration tests in `tests/test_proposals.py` pass. The full offline test suite passes (879 passed, 18 skipped, 0 failures).

---

## 6. Verification Script Output

```text
=== Verification Report for base: origin/phase3a-fixes ===
Branch: phase3b-command-proposals
Commit: 9686b0387d966f038e605ecec2e381c4f8fe7fd0

--- Diff stat vs origin/phase3a-fixes ---
docs/phases/phase3b.md  |  77 ++++++++
 scripts/approve.py      |  94 ++++++++++
 src/jev/proposals.py    | 433 ++++++++++++++++++++++++++++++++++++++++++++
 src/main.py             |  14 ++
 tests/test_proposals.py | 466 ++++++++++++++++++++++++++++++++++++++++++++++++
 5 files changed, 1084 insertions(+)

--- Test Changes ---
Added tests:
  + test_proposals_never_execute_without_approval
  + test_approval_screen_data_complete_and_redacted
  + test_approval_writes_trusted_config_and_enables_execution
  + test_approval_survives_unchanged_run
  + test_identity_change_invalidates_proposal_and_approval
  + test_proposal_generation_from_makefile
  + test_proposal_generation_from_ci_workflow
  + test_proposal_generation_from_readme
  + test_cli_approve_script_interactive_and_yes

--- Collected Test Counts ---
  tests/test_proposals.py: 9 tests collected in 1.39s

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
879 passed, 18 skipped, 2 deselected, 2 warnings in 1060.51s (0:17:40)
```

