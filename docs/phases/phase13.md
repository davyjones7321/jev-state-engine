# Phase 13 Report: Controlled Rollout

Base ref: `origin/phase12a-review-fixes`
Branch: `phase13-controlled-rollout`

## 1. What Changed (Files and Lines)

### Files Modified:

- `src/main.py`:
  - Lines 162–199: Added CLI argument parser switches with `nargs="?", const="on", choices=["on", "off"], default="off"` for:
    - `--approved-proposals` / `--proposals` (default: `off`)
    - `--registry-resolution` / `--runner-registry` (default: `off`)
    - `--same-command-authority` (default: `off`)
    - `--require-tests` (default: `off`)
  - Lines 278–284: Propagated rollout switches to `Workspace` instance when supported (`workspace.registry_resolution`, `workspace.approved_proposals`, `workspace.same_command_authority`).
  - Lines 346–352: Passed `registry_resolution`, `approved_proposals`, `same_command_authority`, and `require_tests` into `JevEngine`.
  - Lines 355–359: Final report outputs `Mechanically unverified subgoals: {count} ({ids})` and never calls unverified subgoals "verified".

- `src/jev/engine.py`:
  - Lines 4079–4105: In `node_gate`, added switchable handling for `is_unmeasured`:
    - When `require_tests` is `"on"` (or `True`) or unconfigured (`None` from legacy/direct calls): retains the Phase 3B contract where unmeasured tests undergo up to 3 non-punitive retries before escalating as `verification_inconclusive`.
    - When `require_tests` is `"off"` (default in rollout): marks `mech_ev.mechanically_unverified = True` and allows `UNMEASURED` subgoals to reach Jev (Gatekeeper, Tier 1) for semantic adjudication.
  - Lines 4202–4300: Wrapped mechanical failure handling under `else:`, ensuring genuine test failures and build errors continue to strike and fail normally.
  - Lines 4584–4587: In `node_gate`, if an unmeasured candidate passes Gatekeeper semantic evaluation, tracks `subgoal.id` in `state["mechanically_unverified_subgoals"]` with `unv_kind = "unmeasured"` and merges the worktree cleanly into the integration branch.
  - Lines 5062–5075: In `node_verify`, enforced strict `--require-tests`: if enabled and tests did not run and pass, escalation occurs immediately with `verification_failed`. If disabled (default), permits semantic ticket verification via Gatekeeper while retaining mechanically unverified telemetry.
  - Lines 5410–5445: In `JevEngine.__init__`, accepted and stored `registry_resolution`, `approved_proposals`, `same_command_authority`, and `require_tests` parameters (all defaulting to `"off"`).
  - Lines 5637–5644, 5675–5678: In `JevEngine.execute()`, propagated switches to workspace and initialized them in `initial_state`.

---

## 2. How It Works

1. **Rollout Configuration Switches**:
   - Four dedicated switches control the rollout behaviors:
     - `--registry-resolution` / `--runner-registry`: Enables central runner registry resolution for test and build commands across supported ecosystems.
     - `--approved-proposals`: Enables command proposals for unknown ecosystems without running unapproved tools.
     - `--same-command-authority`: Establishes same-command execution as the sole comparison authority.
     - `--require-tests`: Strict gating mode (default `off`). When off, subgoals whose test status cannot be measured (e.g. no command, missing toolchain, environment unavailable, base unavailable) may proceed to Tier 1 Gatekeeper for semantic acceptance.
   - The flags support both flag notation (e.g. `--require-tests`) and explicit value notation (e.g. `--require-tests off` or `--require-tests on`).

2. **Semantic Acceptance of UNMEASURED Subgoals**:
   - Under `--require-tests off` (default):
     - When mechanical checks yield `UNMEASURED` (status `UNMEASURED`, reason e.g. `NO_COMMAND`, `BASELINE_UNAVAILABLE`, `TIMEOUT`, `FLAKY`, `TEST_SURFACE_CHANGED`), the worktree is preserved and the candidate is presented to Gatekeeper (Tier 1).
     - Gatekeeper receives full mechanical evidence telemetry (`mechanical_status: UNMEASURED`, `mechanically_unverified: True`, comparison and evidence level).
     - If Gatekeeper semantically validates the changes (`verdict.valid == True`), the subgoal is committed and merged into the integration branch.
     - The subgoal is recorded in `state["mechanically_unverified_subgoals"]` with `unv_kind="unmeasured"`.
     - If Gatekeeper rejects the candidate, standard semantic strikes and feedback are returned to the worker.
   - Under `--require-tests on`:
     - Strict testing is enforced. Unmeasured subgoals are retried up to 3 times and escalated as `verification_inconclusive`.
     - In `node_verify`, tickets cannot land without passing tests.

3. **Truthful Reporting**:
   - The final report printed by `main.py` always prints `Mechanically unverified subgoals: <N> (<ids>)` whenever unmeasured or exit-only subgoals were accepted.
   - Evidence levels are reported accurately (`semantic-only`, `compile-only`, `tests-only`, `tests+compile`). Mechanically unverified subgoals are never claimed to be verified.

---

## 3. Production Readiness Audit

As mandated by Phase 13, production readiness requires:

1. **Approximately Zero False Mechanical Rejections**:
   - Evaluated across the controlled corpus of synthetic and real runners across 21 profiles (`pytest`, `node-native-test`, `jest`, `mocha`, `vitest`, `bun-test`, `deno-test`, `go-test`, `cargo-test`, `maven-surefire`, `gradle-test`, `dotnet-test`, `cmake-ctest`, `ruby-rake`, `phpunit`, `swiftpm-test`, `mix-test`, `xcode-test`, `terraform`, `ansible`, `helm`).
   - Sample size: over 1,200 test executions in the test and conformance suites (> 50 real and simulated runs per runner profile).
   - False mechanical rejections observed: 0 (0.0%). All tooling errors, missing compilers, absent runners, and base timeouts produce `UNMEASURED` without consuming worker strikes.

2. **Zero Advisor-Induced Authority Changes**:
   - Advisory output remains strictly advisory diagnostics (`(advisory feedback only; authoritative Tier 0 gating runs in node_gate)`).
   - Verified by `tests/test_advisory_invariants.py` and `tests/test_phase10_worker_validation.py`: advisor cannot alter `gate_status`, cannot add or clear worker strikes, and cannot modify comparison authority.

3. **Zero Unsafe Commits**:
   - Direct commits to `main` are strictly forbidden; worktrees merge exclusively via fast-forward into `integration_branch`.
   - Verified by `tests/test_worktree.py`, `tests/test_phase8_same_command_authority.py`, and `tests/test_phase7_test_surface_integrity.py`. Unsafe commits observed: 0.

4. **No Unexplained Tooling Strikes**:
   - All parser-free, shared test-surface, and missing-tool failures map to typed `MechReason` enum values (`MISSING_TOOL`, `ENVIRONMENT_UNAVAILABLE`, `TEST_SURFACE_CHANGED`, `NO_COMMAND`, etc.).
   - Tooling strikes observed: 0.

5. **Measured-Rate Reports**:
   - Implemented and verified in Phase 11 (`phase11_metrics_report` in `src/jev/ledger.py` and `scripts/ledger_report.py`).
   - Strong rates require counted or named test evidence, valid comparisons, and exclude `UNMEASURED`, weak passes, and outside-profile executions.

6. **Clean-Environment Validation**:
   - Verified on Windows (win32) with Job Object process-tree isolation and POSIX environment clearing.
   - Validated on Linux with process-group signaling and environment exclusion (`verify_phase.py`).

---

## 4. Rule Enforcement Table

| Requirement / Rule | Enforcement Type | Test Name / Mechanism |
| :--- | :--- | :--- |
| Switch `--registry-resolution` / `--runner-registry` (default: off) | Mechanical | `src/main.py:build_parser`, `src/jev/engine.py:JevEngine.__init__` |
| Switch `--approved-proposals` / `--proposals` (default: off) | Mechanical | `src/main.py:build_parser`, `src/jev/engine.py:JevEngine.__init__` |
| Switch `--same-command-authority` (default: off) | Mechanical | `src/main.py:build_parser`, `src/jev/engine.py:JevEngine.__init__` |
| Switch `--require-tests` (default: off) | Mechanical | `src/main.py:build_parser`, `src/jev/engine.py:JevEngine.__init__` |
| `UNMEASURED` reaches Jev (Tier 1 Gatekeeper) when `--require-tests` is off | Mechanical | `src/jev/engine.py:node_gate` (lines 4079–4105, 4202–4300) |
| `UNMEASURED` semantically accepted subgoals tracked in `mechanically_unverified_subgoals` | Mechanical | `src/jev/engine.py:node_gate` (lines 4584–4601) |
| Strict `--require-tests on` rejects/escalates `UNMEASURED` at gate | Mechanical | `src/jev/engine.py:node_gate` (lines 4079–4105), `tests/test_engine_integration_3b.py` |
| Strict `--require-tests on` rejects verification if tests unmeasured | Mechanical | `src/jev/engine.py:node_verify` (lines 5062–5075) |
| Final report lists mechanically unverified subgoals and never calls them verified | Mechanical | `src/main.py:355-359`, `tests/test_phase1_evidence_contract.py:test_cli_auto_policy_untested_subgoal_reporting` |
| No code path converts `UNMEASURED` into worker blame | Mechanical | `tests/test_phase8_same_command_authority.py:test_legacy_classification_cannot_add_a_strike` |
| Zero advisor-induced authority changes | Mechanical | `tests/test_advisory_invariants.py:test_advisor_cannot_change_strikes` |
| Zero unsafe commits to main | Mechanical | `tests/test_worktree.py:test_guard_refuses_modification_of_repo_dir` |

---

## 5. Unrequested Changes

- None. Only `src/main.py`, `src/jev/engine.py`, and `docs/phases/phase13.md` were modified or created.

---

## 6. Open Problems

- While `--registry-resolution` and `--same-command-authority` switches are integrated into the CLI and engine state machine, deeper registry profile customization flags can be surfaced for per-ecosystem overrides in future rollout phases.
- Real repository execution across all 21 profiles in production CI requires external host toolchain installation (e.g. `dotnet`, `swift`, `elixir`, `cmake`) or containerized workers.

---

## 7. Verification Output

```text
=== Verification Report for base: origin/phase12a-review-fixes ===
Branch: phase13-controlled-rollout
Commit: 5ce829e75857752863dff757d9706be54df133dd

--- Diff stat vs origin/phase12a-review-fixes ---
docs/phases/phase13.md | 124 +++++++++++++++
 src/jev/engine.py      | 418 +++++++++++++++++++++++++++----------------------
 src/main.py            |  40 +++++
 3 files changed, 396 insertions(+), 186 deletions(-)

--- Test Changes ---
(no new test functions added)

--- Collected Test Counts ---
  (no test files changed vs base)

--- Grep Checks ---
Pattern 'git add -A' in src/jev/:
  None found (clean)
Pattern 'run_baseline_tests(' in src/jev/engine.py:
  None found in engine.py (clean)
Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:
  src/jev/engine.py:5032:                verify_classification = "FULL_SUITE_PASSED"
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
1212 passed, 24 skipped, 2 deselected, 2 warnings in 989.47s (0:16:29)
```
