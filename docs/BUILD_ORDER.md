# Jev State Engine: Build Order

This sequencing overrides the Phase 1→5 numbering in `TASK.md`. `TASK.md` describes
*what* to build; this document describes *in what order and with what testing
discipline*. Do not start a step until the previous one's tests are green.

Rule of thumb driving the order: build and prove the deterministic core first,
with the expensive/nondeterministic Jev API plugged in last. Every step before
Step 4 must be fully testable using mocks, with zero real Jev API calls.

---

## Step 1 — Schemas (no tests needed, this is the foundation)
**File:** `src/jev/models.py`

Build: `Subgoal` (incl. `scope`, `expects_tests`), `TestOutcome` enum,
`MechanicalCheckResult`, `ValidationVerdict`, `State` (incl. both
`mechanical_strike_count` and `semantic_strike_count`).

No test file needed here — these are data containers. Pydantic's own
validation is the test. Move to Step 2 once this imports cleanly.

---

## Step 2 — Workspace / Tier 0 (test-first)
**Files:** `tests/test_workspace.py` written **before** `src/jev/workspace.py`

This is pure, deterministic logic — the ideal test-first candidate. Write
every test in `TEST_PLAN.md`'s "Workspace / Tier 0" section first, watch them
fail (no implementation exists yet), then implement `workspace.py` until they
pass.

Build in this sub-order, each with its own passing tests before moving to the
next:
1. `stage_file_mutation`, `get_staged_diff`, `rollback_subgoal` — basic git
   worktree mechanics.
2. `check_build` — compile/parse check.
3. `run_tests` — including the three-way `TestOutcome`, including the
   `NO_TESTS_COLLECTED` case.
4. `check_scope` — diff-vs-declared-scope comparison.
5. `run_mechanical_checks` — composes 2–4, short-circuits on first failure,
   applies the `expects_tests` policy.

**Do not proceed to Step 3 until every test in this section is green and
`run_mechanical_checks` has zero dependency on anything in `gatekeeper.py`.**

---

## Step 3 — Gate routing logic (test-first, fully mocked)
**Files:** `tests/test_engine_gate.py` written before `node_gate()` in
`src/jev/engine.py`

This is the most important step to get right before touching the real API,
because it's where the counter logic — the actual point of this redesign —
lives. Test it with a **fake** `Workspace` and a **fake** `Gatekeeper`: feed
`node_gate()` hand-constructed `MechanicalCheckResult` and `ValidationVerdict`
objects and assert on the resulting counters and routing decision. No network
calls, no real Jev, no flakiness — this step should run in milliseconds and
be exercised dozens of times while you shake out bugs.

Cover every case in `TEST_PLAN.md`'s "Gate routing" section, especially:
mechanical strike doesn't touch semantic counter, semantic strike doesn't
touch mechanical counter, a commit resets both, escalation fires
independently per counter, Tier 1 is never invoked when Tier 0 fails.

**Do not proceed to Step 4 until this passes with a mocked Gatekeeper.** At
this point you have a fully-proven deterministic skeleton with a stubbed-out
judge — this is a legitimate, demo-able milestone on its own.

---

## Step 4 — Gatekeeper / real Jev integration (build, lighter testing)
**Files:** `tests/test_gatekeeper.py`, then `src/jev/gatekeeper.py`

Different discipline here: you can and should test the *mechanics* of the
HTTP client (retry on 5xx, `.env` loading, schema-constrained
`temperature=0` request shape, malformed-response rejection) test-first, the
same as Step 2. But "does Jev's verdict match reality" is not something a
unit test can assert in advance — treat that as an empirical question you
answer by running Step 3's already-proven `node_gate()` against the real
Gatekeeper afterward, not something to pre-specify.

Swap the mocked Gatekeeper from Step 3 for this real one. Re-run Step 3's
test suite — it should still pass unchanged, since it only ever depended on
the Gatekeeper's interface, not its implementation.

---

## Step 5 — Remaining FSM nodes
**File:** `src/jev/engine.py` (rest of it)

`node_investigate()`, `node_plan()`, `node_implement()`, `node_verify()`,
LangGraph `StateGraph` wiring, SQLite checkpointer. These wrap around the
already-proven `node_gate()` — build and wire them now that the core is
solid.

---

## Step 6 — CLI entrypoint
**File:** `src/main.py`

Thin glue: dependency injection, argument parsing. Build last, test lightly
(this is the lowest-risk code in the system).

---

## Milestone checkpoints
- **After Step 2:** Tier 0 can independently reject a bad diff. Demoable
  without Jev existing at all.
- **After Step 3:** The full gating/escalation state machine is provably
  correct against a fake judge. This is the milestone worth pausing at to
  sanity-check the counter design once more before spending real Jev API
  budget on Step 4.
- **After Step 6:** End-to-end system, ready for a real ticket.
