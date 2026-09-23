# Jev State Engine: Architectural Blueprint

## RULE 0: THE PRIME DIRECTIVE
**Never assume, never hardcode.** If a structural decision, domain parameter, or transition boundary is undefined, the system must halt and require explicit user configuration. The engine will not attempt to guess intent, and it will not silently swallow errors to appease the user.

---

## 1. Core Philosophy: Inversion of Control (IoC)
This system abandons the traditional "Thick Agent" paradigm where a non-deterministic LLM controls the execution loop. Instead, we implement **Inversion of Control** via a Deterministic State Engine. 

*   **The Engine (FSM):** A hardcoded Directed Acyclic Graph (DAG) that dictates execution flow, memory management, and tool permissions.
*   **The Worker (Base LLM):** A probabilistic generative model invoked strictly to perform atomic tasks within a specific state constraint.
*   **The Gatekeeper (Jev):** A deterministic "System One" classifier that evaluates physical reality (diffs, test outputs) to permit or deny state transitions.

## 2. Infrastructure & Tech Stack
*   **Language:** Python
*   **FSM Framework:** LangGraph (configured strictly for deterministic state transitions and memory checkpointing).
*   **Integration Model:** Autonomous Execution Engine. The FSM acts as the primary orchestrator/CLI, directly managing the environment (Git Worktrees) and making raw API calls to backend frontier models (e.g., Anthropic/OpenAI), completely owning the control loop.

## 3. The State Machine Topology
The FSM executes a rigid, 4-phase pipeline. The system cannot skip phases.

### State 1: INVESTIGATION
*   **Constraint:** The LLM is granted **Read-Only** access to the filesystem (`ls`, `cat`, `grep`). Write tools are physically unlinked.
*   **Jev Usage:** 0 API calls.
*   **Exit Condition:** The LLM declares it has gathered enough context to plan.

### State 2: PLANNING
*   **Constraint:** The LLM is forced to output a strictly typed Pydantic/JSON object representing the execution plan (an array of atomic subgoals).
*   **Memory:** The FSM stores this array in the immutable graph state (`state["plan_queue"]`).
*   **Exit Condition:** Successful schema validation of the JSON output.

### State 3: IMPLEMENTATION (The Micro-State Loop)
*   **Constraint:** The FSM pops exactly one subgoal from the `plan_queue` and feeds it to the LLM. 
*   **Worktree Execution & Jev Subgoal Gating:**
    *   The LLM is granted an isolated Git Worktree. It executes read/write mutations freely with zero latency.
    *   When the LLM submits the subgoal, the FSM extracts the entire Worktree `diff` and queries the **Jev API**.
    *   *Jev Prompt:* "Does this cumulative diff satisfy the current active subgoal?"
    *   If Jev == `Valid`: Merge to main. If `Invalid`: Reject diff, execute `git reset --hard` on the Worktree, and feed Jev's denial back to the LLM.
*   **Exit Condition:** The subgoal is completed. FSM loops until `plan_queue` is empty.

### State 4: VERIFICATION
*   **Constraint:** The FSM executes the test suite or linter.
*   **Jev Usage:** The FSM sends the original user query and the final environment diff/test results to Jev.
*   **Exit Condition:** Jev mathematically verifies that the physical state of the codebase satisfies the initial ticket.
*   **Failure Edge:** If verification fails, route to Stage 2 Circuit Breaker (Human Escalation) with the test failure diff.

## 4. The Two-Stage Circuit Breaker
LLMs are prone to context blindness and looping. The FSM handles failures deterministically.

### Stage 1: Real-Time Mechanics (Bounded Loop)
If the LLM writes code that triggers a physical error (e.g., Python `SyntaxError`, test `exit_code != 0`), the FSM feeds `stderr` back into the context window.
*   **Trigger:** 3 consecutive mechanical failures (e.g., syntax errors) on the same subgoal.
*   **Action:** Triggers a Human-in-the-Loop (HITL) halt to prevent infinite token drain on unresolvable environmental errors.

### Stage 2: Semantic Loop Halt (Human Escalation)
If the LLM produces code that executes perfectly (no `stderr`), but **Jev repeatedly rejects the output** for failing to meet the business logic of the state, a loop counter increments.
*   **Trigger:** 3 consecutive Jev rejections on the same state (Terminal Logic Failure).
*   **Action:** The FSM physically halts the live run.
*   **Diagnostic:** The FSM freezes the Git Worktree and extracts the LangGraph memory checkpoint (the trapped trajectory) and the Jev API rejection logs.
*   **Resolution:** The FSM executes a Human-in-the-Loop (HITL) handoff. It notifies the human developer to manually review the trajectory, resolve the deadlock, and re-engage the state machine.

---
*System initialization complete. Architecture locked.*
