# Jev State Engine: MVP Specification
## Architecture Direction: Direction C (Hybrid MCP + Worktree)

### 1. The Core Mechanical Adaptation
Based on the vulnerability analysis (specifically the "Latency Trap" and "Jev Semantic Accuracy on partial edits"), gating every individual `write_file` tool call is mathematically unsound. An LLM often requires multiple atomic tool calls to satisfy a single subgoal. Gating a partial edit guarantees false-positive rejections.

To resolve this, we adopt **Direction C augmented by Direction B**.

*   **MCP for Tool Control:** The FSM exposes tools via Model Context Protocol (MCP) to enforce state boundaries (e.g., locking write tools during the `INVESTIGATION` state).
*   **Git Worktree for Execution:** During the `IMPLEMENTATION` state, the LLM is granted a sandboxed Git Worktree. It may execute multiple, zero-latency local writes. 
*   **Jev for Subgoal Gating:** Jev is invoked *only* when the LLM declares the subgoal complete. Jev evaluates the cumulative `git diff` of the Worktree against the subgoal contract. If `Valid`, the Worktree merges to main. If `Invalid`, the FSM triggers the circuit breaker.

### 2. Implementation Pipeline

#### Phase 1: Investigation (MCP Read-Only)
*   **Engine State:** The FSM initializes the LLM connected to an MCP server exposing only `read_file`, `list_dir`, and `grep`.
*   **Goal:** Generate the context required for planning.

#### Phase 2: Planning (Structured Schema)
*   **Engine State:** The FSM prompts the LLM to yield a strict JSON array of Subgoals.
*   **Validation:** Local Pydantic schema validation. No Jev API calls.

#### Phase 3: Implementation (Worktree Isolation)
*   **Engine State:** The FSM forks the repo into a temporary Git Worktree (e.g., `branch: jev-subgoal-1`).
*   **MCP Shift:** The MCP server dynamically unlocks `write_file` and `run_test`.
*   **Execution:** The LLM mutates the Worktree at zero network latency.

#### Phase 4: Jev Boundary Gating
*   **Trigger:** The LLM calls the `submit_subgoal` tool.
*   **Action:** The FSM pauses the LLM. It extracts the full `git diff` of the Worktree.
*   **Jev API Call:** The FSM sends the `diff` and the `subgoal_contract` to Jev (Primitive: `Noul` or `Choice`).
*   **Success:** FSM merges the Worktree. LLM proceeds to Subgoal 2.
*   **Failure:** FSM increments loop counter. FSM executes `git reset --hard` on the Worktree and returns the Jev denial reason to the LLM.

### 3. Human Escalation Circuit Breaker
*   **Trigger:** Jev rejects the same subgoal boundary 3 times.
*   **Action:** 
    1. The FSM physically halts the LLM worker.
    2. The LangGraph SQLite memory checkpoint and Jev rejection logs are extracted.
    3. The FSM freezes the current Git Worktree to preserve the failed state.
    4. The FSM executes a Human-in-the-Loop (HITL) handoff, alerting the developer.
    5. The developer manually resolves the logical deadlock and resumes the FSM.

### 4. MVP Success Criteria
1.  **Zero Hallucinated Merges:** No code enters the main branch without Jev validation.
2.  **API Efficiency:** Jev API calls are minimized to discrete subgoal boundaries, eliminating step-by-step latency.
3.  **Graceful Escalation:** The system successfully intercepts a 3-strike logic loop, prevents architectural drift, and halts cleanly for human intervention.
