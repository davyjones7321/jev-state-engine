# Phase 13 Report: Controlled Rollout

Base ref: `origin/phase13-controlled-rollout`
Branch: `phase13-review-fixes`

## 1. What Changed (Files and Lines)

### Files Modified:

- `src/main.py`:
  - Lines 162–190: Removed no-op CLI flags `--approved-proposals` / `--proposals`, `--registry-resolution` / `--runner-registry`, and `--same-command-authority`.
  - Retained `--require-tests` (nargs="?", const="on", choices=["on", "off"], default="off").
  - Lines 247–254: Removed dead `hasattr(workspace, ...)` switch assignments.
  - Lines 311–316: Removed dead switch parameters passed into `JevEngine(...)`.
  - Lines 339–349: Decoupled proposal hint from any switch: whenever an untyped/auto run encounters `NO_COMMAND` / `MISSING_TOOL`, the proposal hint `Proposal: <id> (run 'python scripts/approve.py <id>' to review and approve)` is printed directly.
  - Lines 355–359: Final report outputs `Mechanically unverified subgoals: {count} ({ids})` and accurately prints evidence levels without calling unverified subgoals "verified".

- `src/jev/engine.py`:
  - Lines 4079–4201: In `node_gate`, refined `is_unmeasured` handling:
    - **Declared test/recipe modifications** (`mech_ev.verifier_modified == True`, covering `TEST_SURFACE_CHANGED` in declared scope or `COMMAND_IDENTITY_CHANGED` with declared recipe edit): disallow autonomous merge by Jev when `--require-tests` is `off`. Sets `state["status"] = "escalated"`, `state["gate_status"] = "human_approval_required"`, emits clear feedback, tears down worktree, and escalates without calling Gatekeeper for merge.
    - **Timeouts and undeclared shared-test surfaces** (`TIMEOUT`, or `TEST_SURFACE_CHANGED` with `verifier_modified == False`): disallow Jev-only autonomous merge; routes to the strict retry path (up to 3 retries, then `verification_inconclusive`) even when `--require-tests` is `off`.
    - **Standard unmeasured outcomes** (`NO_COMMAND`, `BASELINE_UNAVAILABLE`, `FLAKY`, `TOOLING_ERROR`, `NO_TEST_EXECUTION_SIGNAL`, `ENGINE_WORKER_PATH_COLLISION`): when `--require-tests` is `off`, marks `mech_ev.mechanically_unverified = True` and falls through to Gatekeeper (Tier 1) for semantic acceptance.
  - Lines 4584–4587: In `node_gate`, accepted unmeasured subgoals are recorded in `state["mechanically_unverified_subgoals"]` with `unv_kind="unmeasured"`.
  - Lines 5062–5075: In `node_verify`, strict `--require-tests on` escalates if tests did not run and pass; `--require-tests off` permits verification while preserving unverified telemetry.
  - Lines 5444: In `route_gate`, added `"human_approval_required"` to escalation gate statuses.
  - Lines 5484–5520, 5705–5710, 5749–5752: Removed `registry_resolution`, `approved_proposals`, and `same_command_authority` parameters from `JevEngine.__init__`, workspace attribute synchronization, and `initial_state`.

- `tests/test_phase13_require_tests.py`:
  - Added 20 tests covering all `--require-tests` paths, backward compatibility, and sensitive unmeasured escalations.

- `tests/test_phase13_cli_switches.py`:
  - Added 4 tests verifying rejection of the 3 removed flags, `--require-tests` CLI parsing, switchless proposal hint output, and absence of removed attributes on `JevEngine`.

---

## 2. How It Works

1. **Single Rollout Switch (`--require-tests`)**:
   - The CLI and engine expose only `--require-tests` (default: `off`).
   - The three former switches (`--registry-resolution`, `--approved-proposals`, `--same-command-authority`) were complete no-ops—never read by `workspace.py`, `proposals.py`, or `runner_registry.py`—and have been removed. Registry runner resolution, approved proposals, and same-command authority are core engine architecture, not toggleable rollout flags.

2. **Gating Under `--require-tests off` (Default)**:
   - **Declared test/recipe edits**: When `verifier_modified` is `True` (`TEST_SURFACE_CHANGED` within declared scope or `COMMAND_IDENTITY_CHANGED` with recipe edit), the engine forbids autonomous merge on Jev's word alone. It escalates to human review with `gate_status="human_approval_required"`, sets `status="escalated"`, tears down the worktree, and never calls Gatekeeper for merge.
   - **Timeouts & undeclared shared-test surfaces**: When reason is `TIMEOUT` or undeclared `TEST_SURFACE_CHANGED`, autonomous merge is also forbidden. Instead of merging on Jev's verdict, the candidate takes the strict retry path (up to 3 retries, then `verification_inconclusive`).
   - **Standard unmeasured reasons**: Subgoals with `NO_COMMAND`, `BASELINE_UNAVAILABLE`, `FLAKY`, `TOOLING_ERROR`, `NO_TEST_EXECUTION_SIGNAL`, or `ENGINE_WORKER_PATH_COLLISION` are presented to Gatekeeper (Tier 1). If Gatekeeper validates the diff (`verdict.valid == True`), the worktree merges into `integration_branch` and the subgoal is tracked in `state["mechanically_unverified_subgoals"]` with `unv_kind="unmeasured"`. If rejected, worker strikes and feedback apply.
   - **Tooling/environment missing**: `MISSING_TOOL` and `ENVIRONMENT_UNAVAILABLE` produce `env_not_ready` and escalate immediately without strikes or Gatekeeper calls.

3. **Gating Under `--require-tests on` (Strict)**:
   - All unmeasured candidates are retried up to 3 times before escalating as `verification_inconclusive`.
   - Gatekeeper is never called for semantic acceptance of unmeasured candidates.
   - `node_verify` requires full passing tests before a ticket can land.

---

## 3. Production Readiness Audit: Measured vs. Not Measured

| Requirement / Item | Status | Measured Evidence in Repo | What Is NOT Measured / Unverified Claims |
| :--- | :--- | :--- | :--- |
| **False Mechanical Rejections** | **Partially Measured** | 1,236 passed unit and conformance tests in the offline test suite. Unit tests verify that missing tools, absent runners, and base timeouts produce `UNMEASURED` / `env_not_ready` without adding worker strikes. | **NOT MEASURED**: "Over 50 runs per profile" and "0.0% false rejections in production" are unmeasured claims. No 50-run benchmark corpus exists in this repo. 17 of 21 runner profiles have no captured real output (only synthetic fixture mocks), and 11 profiles were skipped entirely due to absent host binaries (`swiftpm`, `xcode`, `gradle`, `dotnet`, `ctest`, `ruby`, `phpunit`, `mix`, `terraform`, `ansible`, `helm`). Real measurement is deferred to later eval work; test counts must not be presented as run counts. |
| **Advisor Authority Changes** | **Measured** | Verified by `tests/test_advisory_invariants.py:test_advisor_cannot_change_strikes` and `tests/test_phase10_worker_validation.py`. Advisor diagnostics are completely isolated from strike counters and comparison authority. | None. |
| **Zero Unsafe Commits** | **Measured** | Verified by `tests/test_worktree.py:test_guard_refuses_modification_of_repo_dir` and repository guard checks in `workspace.py:1662, 1699`. Commits land strictly via worktree fast-forward into `integration_branch`. | None. |
| **No Unexplained Tooling Strikes** | **Partially Measured** | Unit tests in `test_phase5_compile_gate.py` and `test_preflight.py` confirm typed `MechReason` mapping for known errors. | **NOT MEASURED**: Calling tooling strikes 0 in "production" is an unmeasured claim for un-instrumented arbitrary repositories. |
| **Measured-Rate Reports** | **Measured** | Metrics aggregation and SQL formulas verified by unit tests in `tests/test_phase11_metrics.py`. | None. |
| **Clean-Environment Validation** | **Partially Measured** | Windows Job Object process-tree isolation and POSIX environment clearing in `verify_phase.py` are executed and measured on `win32`. | **NOT MEASURED**: Linux clean-environment validation (`os.killpg`, process-group signaling) was never run or validated on a Linux host during this Windows session. |

---

## 4. Rule Enforcement Table

| Requirement / Rule | Enforcement Type | Test Name / Mechanism |
| :--- | :--- | :--- |
| Parser rejects `--registry-resolution`, `--approved-proposals`, `--same-command-authority` | Mechanical | `tests/test_phase13_cli_switches.py:test_three_no_op_flags_are_rejected_by_parser` |
| `--require-tests` flag, explicit on/off, default off | Mechanical | `tests/test_phase13_cli_switches.py:test_require_tests_cli_parsing` |
| Proposal hint prints on `NO_COMMAND` without switch | Mechanical | `tests/test_phase13_cli_switches.py:test_proposal_hint_prints_for_no_command_run_without_switch` |
| `JevEngine` contains no dead switch attributes | Mechanical | `tests/test_phase13_cli_switches.py:test_engine_does_not_contain_no_op_switch_attributes` |
| `UNMEASURED` reaches Jev and merges when `--require-tests off` | Mechanical | `tests/test_phase13_require_tests.py:test_require_tests_off_unmeasured_jev_approves_merges` |
| `UNMEASURED` rejected by Jev adds semantic strike | Mechanical | `tests/test_phase13_require_tests.py:test_require_tests_off_unmeasured_jev_rejects` |
| Declared test/recipe edit disallows Jev-only merge under `--require-tests off` (`human_approval_required`) | Mechanical | `tests/test_phase13_require_tests.py:test_declared_test_or_recipe_edit_requires_human_approval_when_require_tests_off` |
| `TIMEOUT` takes strict retry path even when `--require-tests off` | Mechanical | `tests/test_phase13_require_tests.py:test_timeout_takes_strict_retry_path_even_when_require_tests_off` |
| Undeclared shared-test surface takes strict retry path under `--require-tests off` | Mechanical | `tests/test_phase13_require_tests.py:test_undeclared_shared_test_surface_takes_strict_retry_path_even_when_require_tests_off` |
| Strict `--require-tests on` retries 3x and escalates inconclusive | Mechanical | `tests/test_phase13_require_tests.py:test_require_tests_on_unmeasured_retries_and_escalates` |
| Missing `--require-tests` key behaves as strict | Mechanical | `tests/test_phase13_require_tests.py:test_missing_require_tests_key_behaves_as_strict` |
| `MISSING_TOOL` / `ENVIRONMENT_UNAVAILABLE` escalate immediately (`env_not_ready`) | Mechanical | `tests/test_phase13_require_tests.py:test_missing_tool_and_environment_unavailable_escalate_immediately` |
| `node_verify` off permits unverified telemetry; on escalates `verification_failed` | Mechanical | `tests/test_phase13_require_tests.py:test_node_verify_require_tests_off_lets_ticket_verify_and_keeps_telemetry`, `test_node_verify_require_tests_on_escalates_when_tests_unmeasured_or_not_passed` |
| `JevEngine` default is `--require-tests off` | Mechanical | `tests/test_phase13_require_tests.py:test_jev_engine_defaults_to_require_tests_off` |
| Final report lists mechanically unverified subgoals | Mechanical | `src/main.py:355-359`, `tests/test_phase1_evidence_contract.py:test_cli_auto_policy_untested_subgoal_reporting` |
| No code path converts `UNMEASURED` into worker blame | Mechanical | `tests/test_phase8_same_command_authority.py:test_legacy_classification_cannot_add_a_strike` |
| Zero advisor-induced authority changes | Mechanical | `tests/test_advisory_invariants.py:test_advisor_cannot_change_strikes` |
| Zero unsafe commits to main | Mechanical | `tests/test_worktree.py:test_guard_refuses_modification_of_repo_dir` |

---

## 5. Unrequested Changes

- None. Only `src/main.py`, `src/jev/engine.py`, `tests/test_phase13_require_tests.py`, `tests/test_phase13_cli_switches.py`, and `docs/phases/phase13.md` were modified or created.

---

## 6. Open Problems

- **Profile Real Executions**: 17 of the 21 runner profiles have no captured real execution output; 11 profiles are skipped on Windows CI due to missing toolchains. Empirical evaluation against real repositories is deferred to future benchmarking phases.
- **Human Approval Interaction**: While `gate_status="human_approval_required"` halts execution cleanly and escalates without autonomous merging, an interactive CLI resume/approval flow for the human operator (similar to proposal approvals) has not yet been built into `main.py`.
- **Linux Environment Validation**: Process-tree killing and environment clearing logic on Linux has not been run or validated on a Linux host during this development session.

---

## 7. Verification Output

```text
=== Verification Report for base: origin/phase13-controlled-rollout ===
Branch: phase13-review-fixes
Commit: 5eee4ae230bde8ab880f893c824b10f8d5b05c47

--- Diff stat vs origin/phase13-controlled-rollout ---
src/jev/engine.py                   |  96 ++++--
 src/main.py                         |  42 +--
 tests/test_phase13_cli_switches.py  | 104 +++++++
 tests/test_phase13_require_tests.py | 593 ++++++++++++++++++++++++++++++++++++
 4 files changed, 779 insertions(+), 56 deletions(-)

--- Test Changes ---
Added tests:
  + test_three_no_op_flags_are_rejected_by_parser
  + test_require_tests_cli_parsing
  + test_proposal_hint_prints_for_no_command_run_without_switch
  + test_engine_does_not_contain_no_op_switch_attributes
  + test_require_tests_off_unmeasured_jev_approves_merges
  + test_require_tests_off_unmeasured_jev_rejects
  + test_require_tests_on_unmeasured_retries_and_escalates
  + test_missing_require_tests_key_behaves_as_strict
  + test_missing_tool_and_environment_unavailable_escalate_immediately
  + test_node_verify_require_tests_off_lets_ticket_verify_and_keeps_telemetry
  + test_node_verify_require_tests_on_escalates_when_tests_unmeasured_or_not_passed
  + test_jev_engine_defaults_to_require_tests_off
  + test_declared_test_or_recipe_edit_requires_human_approval_when_require_tests_off
  + test_timeout_takes_strict_retry_path_even_when_require_tests_off
  + test_undeclared_shared_test_surface_takes_strict_retry_path_even_when_require_tests_off

--- Collected Test Counts ---
  tests/test_phase13_cli_switches.py: 4 tests collected in 2.24s
  tests/test_phase13_require_tests.py: 20 tests collected in 1.82s

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:5109:                verify_classification = "FULL_SUITE_PASSED"
  src/jev/workspace.py:2449:                    "outcome": TestOutcome.FULL_SUITE_PASSED.value,
  src/jev/workspace.py:5559:                        test_classification = TestOutcome.FULL_SUITE_PASSED.value
Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):
  src/jev/workspace.py:1662:        if self.worktree_dir == self.repo_dir:
  src/jev/workspace.py:1699:        if self.worktree_dir == self.repo_dir:

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
1236 passed, 24 skipped, 2 deselected, 2 warnings in 1081.87s (0:18:01)
```
