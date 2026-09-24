# Jev State Engine: Execution Tasks (Revised — Tier 0 Gate)

- `[ ]` **Phase 1: Environment & Schemas**
  - `[ ]` Initialize `venv` and `requirements.txt`.
  - `[ ]` Install core libraries: `langgraph`, `langchain-anthropic`, `pydantic`, `httpx`, `python-dotenv`.
  - `[ ]` Create `src/jev/models.py`:
    - `Subgoal(BaseModel)`: must include a `scope: list[str]` field — the files/paths the subgoal is expected to touch. Tier 0 needs this to check for scope creep.
    - **[NEW]** `Subgoal.expects_tests: bool` — set during Planning. `True` for any subgoal claiming to implement real behavior; `False` only for subgoals that are honestly not testable yet (e.g. initial scaffolding, config-only changes). Defaults to `True` — a subgoal has to explicitly opt out of being tested, not opt in.
    - **[NEW]** `TestOutcome(str, Enum)`: `PASSED` / `FAILED` / `NO_TESTS_COLLECTED` — three-way, not a boolean. Collapsing "no tests ran" into "passed" is exactly the failure mode this schema exists to prevent.
    - `MechanicalCheckResult(BaseModel)`: `passed: bool`, `failed_check: str | None` (`"build"` / `"tests"` / `"no_tests_collected"` / `"scope"`), `detail: str` (stderr, out-of-scope file list, etc).
    - `ValidationVerdict(BaseModel)`: Jev's semantic verdict — `valid: bool`, `probability: float` (Jev's calibrated confidence, straight from the Noul response), `reason: str | None` (only populate if you add a follow-up explanation call; the base Noul response doesn't include one). Only ever produced *after* Tier 0 passes.
    - `State(TypedDict)`: add two separate counters — `mechanical_strike_count` and `semantic_strike_count` — replacing the single `loop_counter`.

- `[ ]` **Phase 2: The Workspace Module**
  - `[ ]` Create `src/jev/workspace.py`.
  - `[ ]` Implement `run_read_tool()`.
  - `[ ]` Implement Git Worktree isolation (`stage_file_mutation`, `get_staged_diff`).
  - `[ ]` Implement Worktree resolution (`commit_subgoal`, `rollback_subgoal`).
  - `[ ]` Implement `run_tests() -> TestOutcome` — executes the test suite, returns stdout/stderr/exit code. **Must distinguish "zero tests collected" from "all tests passed"** — do not treat the runner's exit code as a plain boolean (e.g. `pytest` returns a distinct exit code, `5`, specifically for "no tests were collected," separate from `0`/pass and `1`/fail — map that to `TestOutcome.NO_TESTS_COLLECTED`, not `PASSED`).
  - `[ ]` **[NEW] Implement `check_build()`** — attempts a compile/parse/import pass. **[REVISED] Scope: the files declared in `subgoal.scope` (or the diff's touched files), not a full walk of the worktree.** A whole-repo `check_build()` would fail a subgoal for a syntax error in an unrelated, pre-existing file it never touched — that's a false-positive rejection of the exact kind the original design was meant to avoid. `run_mechanical_checks()` must pass `subgoal.scope` into `check_build()` explicitly.
  - `[ ]` **[NEW] Implement `check_scope(diff, subgoal.scope)`** — compares the files touched in `get_staged_diff()` against `subgoal.scope`; flags any file touched that wasn't declared.
  - `[ ]` **[NEW] Implement `run_mechanical_checks(subgoal) -> MechanicalCheckResult`** — runs `check_build`, `run_tests`, `check_scope` in that order (cheapest/fastest first), short-circuits on first failure, returns a single typed result. This is Tier 0.
    - **[NEW] `run_tests()` returning `NO_TESTS_COLLECTED` is not automatically a pass.** Policy: if `subgoal.expects_tests` is `True`, treat `NO_TESTS_COLLECTED` as a mechanical failure (`failed_check: "no_tests_collected"`) — the subgoal claimed to deliver verifiable behavior and left nothing to verify it. If `subgoal.expects_tests` is `False`, let it through, but set `MechanicalCheckResult.detail` to note the untested pass explicitly so it's visible downstream.

- `[ ]` **Phase 3: The Gatekeeper Module**
  - `[ ]` Create `src/jev/gatekeeper.py`.
  - `[ ]` Load `JEV_API_URL` (`https://api.typesafe.ai/v1/systemone`) and `JEV_API_KEY` exclusively from `.env`.
  - `[ ]` **[REVISED]** Implement `validate_subgoal(subgoal, diff)` — **Tier 1 only**. Assumes Tier 0 already passed.
    - **[NEW] Canonical signature — do not deviate:** `validate_subgoal(self, subgoal: Subgoal, diff: str, mechanical_detail: str = "") -> ValidationVerdict`. This exact parameter name (`mechanical_detail`) must be used by the real `Gatekeeper`, by `node_gate`'s call site, and by every fake/mock Gatekeeper in the test suite. `node_gate` must call it directly with this signature — no `try/except TypeError` fallback cascade through alternate parameter names. If a call fails, the failure should surface, not be silently retried under a different guessed signature.
    - Jev is TypeSafe AI's System One model — it does not generate text. It takes a `state` object plus a set of typed `questions` and returns typed, calibrated answers directly. There is no free-text response to parse, so drop any earlier assumption about prompting a chat model and extracting JSON from its output.
    - Build the request's `state` from `subgoal.description`, `subgoal.scope`, and the raw diff from `get_staged_diff()` — this is just the evidence Jev evaluates against, not a prompt.
    - Ask a single `Noul` (boolean) question, e.g. `valid`, instructions: "Does the diff fully and correctly implement the subgoal described in state, without exceeding its declared scope?" Noul is the right primitive here (not `Choice`) because the decision is strictly binary — `Choice` is for selecting among 3+ named categories, which doesn't apply to a valid/invalid gate.
    - Map the response's `value` (`true`/`false`) directly to `ValidationVerdict.Valid`/`Invalid` — no JSON-parsing or malformed-response recovery logic needed here, since the API guarantees the typed shape. Still validate the HTTP-level response (status code, schema) defensively — that's a different concern from text-parsing risk.
    - Store the response's `probability` field on `ValidationVerdict` alongside the boolean — useful later for tuning, logging, or a confidence threshold, even if the MVP just gates on the boolean.
    - Includes HTTP retry logic (5xx errors). No mocks.
    - **[NEW]** If `MechanicalCheckResult.detail` flags an untested pass (`subgoal.expects_tests == False`), include that as an explicit field in `state` — the untested-pass flag is evidence Jev should see, same as the diff itself, not a hint or instruction.
  - `[ ]` Implement `verify_ticket()` (Phase 4/final verification, unchanged).
  - `[ ]` Implement `escalate_deadlock(trajectory, triggering_tier)` — now takes which tier caused the escalation, so the dumped `escalation.log` tells the human whether they're looking at a mechanical dead end (bad plan, environment issue) or a semantic dead end (Jev and the LLM disagree on intent repeatedly).

- `[ ]` **Phase 4: The Engine (LangGraph FSM)**
  - `[ ]` Create `src/jev/engine.py`.
  - `[ ]` Define the `StateGraph` topography.
  - `[ ]` Implement `node_investigate()`, `node_plan()`, `node_implement()`, `node_verify()`.
  - `[ ]` **[REVISED] Implement `node_gate()` with two-tier routing:**
    1. Call `workspace.run_mechanical_checks(subgoal)`.
       - **Fail** → `workspace.rollback_subgoal()`. Increment `mechanical_strike_count`. Feed `MechanicalCheckResult.detail` back to the LLM. Do **not** call Jev. Do **not** touch `semantic_strike_count`.
       - **Pass** → continue to step 2.
    2. Call `gatekeeper.validate_subgoal(subgoal, diff)` (Tier 1 / Jev).
       - **Valid** → `workspace.commit_subgoal()`. Reset both counters to 0 (fresh subgoal, fresh trust).
       - **Invalid** → `workspace.rollback_subgoal()`. Increment `semantic_strike_count`. Feed Jev's denial reason back to the LLM. Do **not** touch `mechanical_strike_count`.
  - `[ ]` Implement edge routing:
    - `mechanical_strike_count == 3` → `gatekeeper.escalate_deadlock(trajectory, triggering_tier="mechanical")`.
    - `semantic_strike_count == 3` → `gatekeeper.escalate_deadlock(trajectory, triggering_tier="semantic")`.
    - Counters are independent — 2 mechanical strikes followed by 2 semantic strikes on the same subgoal does **not** escalate; each tier gets its own full budget.
  - `[ ]` Wire LangGraph Checkpointer (SQLite) for memory persistence — persist both counters per subgoal, not just one.

- `[ ]` **Phase 5: CLI Entrypoint**
  - `[ ]` Create `src/main.py`.
  - `[ ]` Wire Dependency Injection (Workspace + Gatekeeper -> Engine).
  - `[ ]` Implement basic CLI argument parsing for the ticket input.

## Verification Plan

### Automated Tests
- `pytest tests/test_workspace.py`:
  - Verify `rollback_subgoal()` successfully reverts a dirty file using native Git commands.
  - **[NEW]** Verify `check_scope()` correctly flags a diff that touches a file outside the subgoal's declared scope.
  - **[NEW]** Verify `run_mechanical_checks()` short-circuits — a failing build check must not trigger a `run_tests()` call.
  - **[NEW]** Verify `run_tests()` on an empty/no-test-files directory returns `TestOutcome.NO_TESTS_COLLECTED`, not `PASSED`.
  - **[NEW]** Verify `run_mechanical_checks()` rejects a subgoal with `expects_tests=True` when `run_tests()` returns `NO_TESTS_COLLECTED`.
  - **[NEW]** Verify `run_mechanical_checks()` allows a subgoal with `expects_tests=False` through on `NO_TESTS_COLLECTED`, and that `MechanicalCheckResult.detail` flags it as untested.
- **[NEW]** `pytest tests/test_gatekeeper.py`:
  - Verify `validate_subgoal()` is never called when `run_mechanical_checks()` fails (mock `run_mechanical_checks` to fail, assert the Jev HTTP client received zero calls).
- **[NEW]** `pytest tests/test_engine.py`:
  - Verify a mechanical failure increments only `mechanical_strike_count`.
  - Verify a Jev rejection increments only `semantic_strike_count`.
  - Verify a successful commit resets both counters.
  - Verify escalation fires independently at 3 strikes on either counter.

## Follow-up Cleanup Tasks
- `[ ]` **Centralize Path Containment Security Check**:
  Consolidate the four separate hand-written path containment checks (`read_file`, `Workspace.run_read_tool`, `grep`, and `PlanSubgoalModel.validate_scope_items`) into a single canonical helper (e.g. `Workspace.validate_path_containment(path, base) -> Path`) with unified symlink resolution, `..` traversal blocking, and absolute path confinement, preventing future sprawl.
