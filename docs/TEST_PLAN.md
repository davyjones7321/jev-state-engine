# Jev State Engine: Test Plan

Test cases only — no implementation. Each section corresponds to a step in
`BUILD_ORDER.md` and must be written (and observed failing, since no
implementation exists yet) before that step's implementation begins.

---

## Workspace / Tier 0 — `tests/test_workspace.py`
(Write before `src/jev/workspace.py`. Corresponds to Build Order Step 2.)

1. `test_rollback_subgoal_reverts_dirty_file` — stage a mutation, call
   `rollback_subgoal()`, assert the file matches the pre-mutation state via
   native git commands (not by trusting the Workspace's own read-back).
2. `test_check_build_fails_on_syntax_error` — stage a file with a deliberate
   syntax error, assert `check_build()` returns failure without invoking
   `run_tests()`.
3. `test_check_build_passes_on_valid_file` — sanity check, valid file passes.
4. `test_run_tests_returns_passed` — stage a passing test file, assert
   `TestOutcome.PASSED`.
5. `test_run_tests_returns_failed` — stage a failing test, assert
   `TestOutcome.FAILED`.
6. `test_run_tests_on_empty_directory_returns_no_tests_collected` — point
   `run_tests()` at a directory with zero test files, assert
   `TestOutcome.NO_TESTS_COLLECTED` specifically — **must not** return
   `PASSED`. This is the regression test for the empty-folder issue.
7. `test_check_scope_flags_out_of_scope_file` — stage a diff touching a file
   not listed in `subgoal.scope`, assert `check_scope()` flags it.
8. `test_check_scope_passes_in_scope_diff` — diff only touches declared
   files, assert pass.
9. `test_run_mechanical_checks_short_circuits_on_build_failure` — mock
   `check_build` to fail, assert `run_tests()` and `check_scope()` are never
   called (use call-count assertions, not just the final result).
10. `test_run_mechanical_checks_short_circuits_on_test_failure` — build
    passes, tests fail, assert `check_scope()` is never called.
11. `test_run_mechanical_checks_rejects_no_tests_when_expected` —
    `subgoal.expects_tests=True`, `run_tests()` returns
    `NO_TESTS_COLLECTED`, assert `MechanicalCheckResult.passed=False`,
    `failed_check="no_tests_collected"`.
12. `test_run_mechanical_checks_allows_no_tests_when_not_expected` —
    `subgoal.expects_tests=False`, `run_tests()` returns
    `NO_TESTS_COLLECTED`, assert `MechanicalCheckResult.passed=True` **and**
    `detail` contains an explicit untested-pass flag (assert on its
    presence, not just that the field is non-empty).

---

## Gate routing — `tests/test_engine_gate.py`
(Write before `node_gate()`. Corresponds to Build Order Step 3. Use a fake
`Workspace` and fake `Gatekeeper` throughout — assert zero real network
calls in every test in this file.)

13. `test_mechanical_failure_increments_only_mechanical_counter` — fake
    Workspace returns a failing `MechanicalCheckResult`; assert
    `mechanical_strike_count += 1`, `semantic_strike_count` unchanged, fake
    Gatekeeper's `validate_subgoal` was **never called**.
14. `test_mechanical_failure_triggers_rollback` — assert
    `rollback_subgoal()` was called exactly once.
15. `test_tier0_pass_invokes_tier1` — fake Workspace returns a passing
    `MechanicalCheckResult`; assert `gatekeeper.validate_subgoal()` **was**
    called.
16. `test_semantic_rejection_increments_only_semantic_counter` — Tier 0
    passes, fake Gatekeeper returns `Invalid`; assert
    `semantic_strike_count += 1`, `mechanical_strike_count` unchanged.
17. `test_successful_commit_resets_both_counters` — start with nonzero
    counters from a prior subgoal, Tier 0 and Tier 1 both pass; assert both
    counters reset to 0 and `commit_subgoal()` was called.
18. `test_mechanical_escalation_fires_at_three_independent_of_semantic` —
    drive `mechanical_strike_count` to 3 via repeated Tier 0 failures while
    `semantic_strike_count` stays at 0 throughout; assert
    `escalate_deadlock(triggering_tier="mechanical")` fires at exactly 3, not
    before.
19. `test_semantic_escalation_fires_at_three_independent_of_mechanical` —
    mirror of 18 for the semantic counter.
20. `test_interleaved_strikes_do_not_cross_contaminate` — 2 mechanical
    failures, then 2 semantic rejections, then 1 more mechanical failure
    (3rd mechanical strike); assert escalation fires on the mechanical
    counter reaching 3, and that the semantic counter sitting at 2 never
    contributed to it.

---

## Gatekeeper mechanics — `tests/test_gatekeeper.py`
(Write before the HTTP client logic in `gatekeeper.py`. Corresponds to Build
Order Step 4. Note: these test the *plumbing*, not Jev's judgment quality —
see `BUILD_ORDER.md` Step 4 for why verdict correctness isn't unit-testable.)

21. `test_validate_subgoal_retries_on_5xx` — mock the HTTP layer to return
    a 500 then a 200, assert the client retried and returned the eventual
    success.
22. `test_validate_subgoal_does_not_retry_on_4xx` — mock a 400 (e.g. an
    invalid/expired key), assert no retry loop (a bad request or bad auth
    won't fix itself by resending).
23. `test_validate_subgoal_sends_noul_question` — inspect the outgoing
    request payload, assert `questions.valid.type == "boolean"` (Noul), not
    a `Choice` or free-text prompt.
24. `test_validate_subgoal_maps_response_correctly` — mock a well-formed
    Noul response (`{"valid": {"value": true, "probability": 0.91}}`),
    assert it maps to `ValidationVerdict(valid=True, probability=0.91)`
    with no parsing/regex involved.
25. `test_validate_subgoal_rejects_malformed_response` — mock an HTTP-level
    response that doesn't match the expected schema (missing fields, wrong
    types), assert it raises rather than silently returning a default
    verdict. This is a defensive HTTP-response check, not text parsing —
    the API guarantees typed output, so this test covers transport-layer
    failures, not "did the model format its answer correctly."
26. `test_validate_subgoal_forwards_untested_flag` — construct a call where
    `MechanicalCheckResult.detail` carries the untested-pass flag (from test
    12), assert it's present in the `state` object sent to Jev.
26. `test_env_vars_loaded_from_dotenv_only` — assert `JEV_API_URL` /
    `JEV_API_KEY` are read from `.env` and that no hardcoded fallback
    exists in the source.

---

## Notes for the implementing agent
- Tests 1–12 must all be written and failing (red) before any line of
  `workspace.py` is written. Same for 13–20 against `engine.py`'s
  `node_gate`, and 21–26 against `gatekeeper.py`.
- Tests 13–20 must never import or instantiate the real `Gatekeeper` class —
  use a fake/mock that implements the same interface. This is what lets the
  gate logic be proven correct without spending real API calls.
- After Step 4 is built, re-run 13–20 unchanged (swap the fake Gatekeeper
  for the real one) — they should still pass without modification. If they
  don't, the real Gatekeeper's interface has drifted from what the gate
  logic expects, which is itself a bug to fix before proceeding.
