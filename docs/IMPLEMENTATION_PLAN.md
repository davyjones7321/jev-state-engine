# Jev State Engine: Deep Module Implementation Plan (Tier 0 Architecture)

This plan translates the `ARCHITECTURE.md` blueprint into concrete Python modules using deep module boundaries. It relies on a **Two-Tier Gating System** to minimize external API costs, strictly eliminate scope creep, and enforce test coverage mechanically.

## Environment & Tooling
*   **Dependency Management:** standard `venv` and `requirements.txt`.
*   **LLM Provider:** `langchain-anthropic` or `langchain-openai` for structured tool outputs.
*   **API Configuration:** All external integrations read strictly from a local `.env` file. No hardcoding. No mocks.

## Proposed Changes

We will scaffold the Python package `jev` inside a `src/` directory to separate it from the documentation.

### Core Domain Models

#### [NEW] src/jev/models.py
Define all strict Pydantic schemas that pass between boundaries.
*   `Subgoal(BaseModel)`: 
    *   `scope: list[str]`: Declares intended file mutations.
    *   `expects_tests: bool`: Defaults to `True`. The LLM must explicitly opt-out if a feature is untestable.
*   `TestOutcome(str, Enum)`: `PASSED`, `FAILED`, `NO_TESTS_COLLECTED`. Prevents Pytest exit code 5 from being swallowed as a false positive.
*   `MechanicalCheckResult(BaseModel)`: Returned by Tier 0. Contains `passed: bool`, `failed_check: str`, and `detail: str`.
*   `ValidationVerdict(BaseModel)`: Typed response from Jev (`Valid` / `Invalid` + reason).
*   `State(TypedDict)`: The LangGraph state dictionary. Tracks `plan_queue`, `current_subgoal`, `mechanical_strike_count`, and `semantic_strike_count`.

---

### The Workspace Module

#### [NEW] src/jev/workspace.py
Isolates all physical disk mutation and Git worktree orchestration, and acts as the **Tier 0 (Mechanical) Gate**.
*   `Workspace(class)`:
    *   `run_read_tool(cmd, args)`: Executes safe bash commands.
    *   `stage_file_mutation(path, content)`: Writes to the active Git worktree.
    *   `get_staged_diff()`: Generates `git diff`.
    *   `commit_subgoal()` & `rollback_subgoal()`: Native Git branching resolution.
    *   `check_build()`: Fast-fails on local syntax/compilation errors.
    *   `run_tests() -> TestOutcome`: Executes the unit test suite. Explicitly maps `pytest` exit code 5 to `NO_TESTS_COLLECTED`.
    *   `check_scope(diff, subgoal.scope)`: Parses the Git diff and throws an error if any file was touched that was not declared in the `Subgoal.scope` array.
    *   `run_mechanical_checks(subgoal)`: The Tier 0 orchestrator. Runs build -> scope -> tests in order. If `expects_tests=True`, a `NO_TESTS_COLLECTED` outcome is a hard mechanical failure.

---

### The Gatekeeper Module

#### [NEW] src/jev/gatekeeper.py
Isolates all external HTTP calls and acts as the **Tier 1 (Semantic) Gate**. 
*   `Gatekeeper(class)`:
    *   `__init__()`: Loads `JEV_API_URL` and `JEV_API_KEY` from `.env`.
    *   `validate_subgoal(subgoal, diff, mechanical_detail)`: **Tier 1 Only.** Posts to the REAL Jev FastAPI service using the `Noul` schema. If the code passed Tier 0 but lacked tests (`expects_tests=False`), the Gatekeeper flags this explicitly in the Jev prompt so the Semantic Judge knows it is evaluating unprotected code.
    *   `verify_ticket(ticket, final_diff, test_output)`: Final Phase 4 verification post to Jev.
    *   `escalate_deadlock(trajectory, triggering_tier)`: Formats the trapped trajectory and specifies whether the deadlock was `mechanical` or `semantic`. Triggers a hard halt for human review.

---

### The Engine Module (FSM)

#### [NEW] src/jev/engine.py
The rigid LangGraph DAG orchestrator.
*   `JevEngine(class)`:
    *   `__init__(workspace, gatekeeper, llm)`: Dependency injection.
    *   `execute(ticket)`: Compiles and runs the LangGraph DAG.
*   **Graph Nodes (Internal Functions):**
    *   `node_investigate()`: Binds Read tools.
    *   `node_plan()`: Enforces structured `Subgoal` output (including `scope` and `expects_tests`).
    *   `node_implement()`: Pops a subgoal, runs worker LLM, collects mutations in `workspace`.
    *   `node_gate()`: **The Two-Tier Router.**
        1.  Calls `workspace.run_mechanical_checks()`. If fail: `rollback`, increment `mechanical_strike_count`, return to LLM.
        2.  If Tier 0 passes, calls `gatekeeper.validate_subgoal()`. 
        3.  If Tier 1 valid: `commit`, reset BOTH counters to 0. 
        4.  If Tier 1 invalid: `rollback`, increment `semantic_strike_count`, return to LLM.
    *   `node_verify()`: Calls `workspace.run_tests()` and `gatekeeper.verify_ticket()`.
*   **Edge Routing:**
    *   Monitors `mechanical_strike_count == 3` and `semantic_strike_count == 3` independently. Routes to `escalate_deadlock` if either hits the threshold.

---

### CLI Entrypoint

#### [NEW] src/main.py
The executable CLI that wires the dependencies together.
*   Instantiates `Workspace`, `Gatekeeper`, and the `LLMWorker`.
*   Injects them into `JevEngine`.
*   Fires `engine.execute(sys.argv[1])`.
