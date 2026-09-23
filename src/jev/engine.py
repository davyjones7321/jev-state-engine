import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.graph import END, START, StateGraph

from jev.models import State, Subgoal


class SqliteSaver(BaseCheckpointSaver):
    """SQLite-backed LangGraph checkpointer for persisting FSM state and strike counters."""

    def __init__(self, conn: sqlite3.Connection):
        super().__init__()
        self.conn = conn
        self.lock = threading.RLock()
        self._setup()

    @contextmanager
    def cursor(self, transaction: bool = True) -> Iterator[sqlite3.Cursor]:
        with self.lock:
            cur = self.conn.cursor()
            try:
                yield cur
                if transaction:
                    self.conn.commit()
            except Exception:
                if transaction:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    def _setup(self) -> None:
        with self.lock:
            cur = self.conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode = WAL")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        thread_id TEXT,
                        checkpoint_ns TEXT,
                        checkpoint_id TEXT,
                        parent_checkpoint_id TEXT,
                        type TEXT,
                        checkpoint TEXT,
                        metadata TEXT,
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS checkpoint_writes (
                        thread_id TEXT,
                        checkpoint_ns TEXT,
                        checkpoint_id TEXT,
                        task_id TEXT,
                        idx INTEGER,
                        channel TEXT,
                        type TEXT,
                        blob TEXT,
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                    )
                """)
                self.conn.commit()
            finally:
                cur.close()

    @classmethod
    def from_conn_string(cls, conn_string: str) -> "SqliteSaver":
        if conn_string != ":memory:" and not conn_string.startswith("file:"):
            Path(conn_string).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(conn_string, check_same_thread=False)
        return cls(conn)

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        checkpoint_ns = configurable.get("checkpoint_ns") or ""
        checkpoint_id = configurable.get("checkpoint_id")

        if not thread_id:
            return None

        with self.cursor(transaction=False) as cursor:
            if checkpoint_id:
                cursor.execute(
                    """
                    SELECT parent_checkpoint_id, type, checkpoint, metadata
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    """,
                    (str(thread_id), str(checkpoint_ns), str(checkpoint_id)),
                )
            else:
                cursor.execute(
                    """
                    SELECT parent_checkpoint_id, type, checkpoint, metadata, checkpoint_id
                    FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (str(thread_id), str(checkpoint_ns)),
                )

            row = cursor.fetchone()
            if not row:
                return None

            if checkpoint_id:
                parent_id, cp_type, cp_data, md_data = row
                ret_id = checkpoint_id
            else:
                parent_id, cp_type, cp_data, md_data, ret_id = row

            raw_bytes = cp_data if isinstance(cp_data, bytes) else (cp_data.encode("utf-8") if isinstance(cp_data, str) else cp_data)
            checkpoint = self.serde.loads_typed((cp_type, raw_bytes))
            metadata = json.loads(md_data) if md_data else {}

            cursor.execute(
                """
                SELECT task_id, channel, type, blob
                FROM checkpoint_writes
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                ORDER BY task_id, idx
                """,
                (str(thread_id), str(checkpoint_ns), str(ret_id)),
            )
            writes = []
            for task_id, channel, w_type, w_blob in cursor.fetchall():
                w_bytes = w_blob if isinstance(w_blob, bytes) else (w_blob.encode("utf-8") if isinstance(w_blob, str) else w_blob)
                val = self.serde.loads_typed((w_type, w_bytes))
                writes.append((task_id, channel, val))

        parent_config = (
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": parent_id,
                }
            }
            if parent_id
            else None
        )

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": ret_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=writes,
        )

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        configurable = config.get("configurable", {}) if config else {}
        thread_id = configurable.get("thread_id")
        checkpoint_ns = configurable.get("checkpoint_ns")
        if checkpoint_ns is None and config and "checkpoint_ns" in configurable:
            checkpoint_ns = ""
        query = "SELECT thread_id, checkpoint_ns, checkpoint_id FROM checkpoints WHERE 1=1"
        params: List[Any] = []
        if thread_id:
            query += " AND thread_id = ?"
            params.append(str(thread_id))
        if checkpoint_ns is not None:
            query += " AND checkpoint_ns = ?"
            params.append(str(checkpoint_ns))
        if before and before.get("configurable", {}).get("checkpoint_id"):
            query += " AND checkpoint_id < ?"
            params.append(str(before["configurable"]["checkpoint_id"]))
        query += " ORDER BY rowid DESC"
        if limit:
            query += f" LIMIT {limit}"

        with self.cursor(transaction=False) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

        for t_id, c_ns, c_id in rows:
            tup = self.get_tuple(
                {
                    "configurable": {
                        "thread_id": t_id,
                        "checkpoint_ns": c_ns,
                        "checkpoint_id": c_id,
                    }
                }
            )
            if tup:
                if filter:
                    if not all(tup.metadata.get(k) == v for k, v in filter.items()):
                        continue
                yield tup

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config.get("configurable", {})
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns") or ""
        parent_id = configurable.get("checkpoint_id")
        checkpoint_id = checkpoint["id"]

        cp_type, cp_blob = self.serde.dumps_typed(checkpoint)
        blob_bytes = bytes(cp_blob) if isinstance(cp_blob, (bytes, bytearray, memoryview)) else (cp_blob.encode("utf-8") if isinstance(cp_blob, str) else cp_blob)
        md_str = json.dumps(metadata)

        with self.cursor(transaction=True) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(thread_id),
                    str(checkpoint_ns),
                    str(checkpoint_id),
                    str(parent_id) if parent_id else None,
                    str(cp_type),
                    blob_bytes,
                    md_str,
                ),
            )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable", {})
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns") or ""
        checkpoint_id = configurable.get("checkpoint_id", "")

        query = (
            """
            INSERT OR REPLACE INTO checkpoint_writes
            (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            if all(w[0] in WRITES_IDX_MAP for w in writes)
            else """
            INSERT OR IGNORE INTO checkpoint_writes
            (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        params = []
        for idx, (channel, val) in enumerate(writes):
            w_type, w_blob = self.serde.dumps_typed(val)
            blob_bytes = bytes(w_blob) if isinstance(w_blob, (bytes, bytearray, memoryview)) else (w_blob.encode("utf-8") if isinstance(w_blob, str) else w_blob)
            idx_val = WRITES_IDX_MAP.get(channel, idx)
            params.append(
                (
                    str(thread_id),
                    str(checkpoint_ns),
                    str(checkpoint_id),
                    str(task_id),
                    int(idx_val),
                    str(channel),
                    str(w_type),
                    blob_bytes,
                )
            )

        with self.cursor(transaction=True) as cur:
            cur.executemany(query, params)


def node_investigate(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
) -> State:
    """State 1: INVESTIGATION (Read-Only access to codebase)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    notes = ""
    if workspace is not None and hasattr(workspace, "run_read_tool"):
        git_status = workspace.run_read_tool("git", ["status", "--porcelain"])
        notes = f"Workspace status:\n{git_status}"
    else:
        notes = "No workspace read tool bound."

    if llm is not None:
        pass

    state["last_feedback"] = notes
    state["trajectory"].append({"node": "investigate", "notes": notes})
    return state


def node_plan(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
) -> State:
    """State 2: PLANNING (Generates structured Subgoals with declared scope)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    plan_queue = state.get("plan_queue")
    if not plan_queue:
        ticket = state.get("ticket", "Default task")
        # Default plan: single subgoal based on ticket
        plan_queue = [
            Subgoal(
                description=ticket,
                scope=[],
                expects_tests=True,
            )
        ]
        state["plan_queue"] = plan_queue
    else:
        normalized_queue: List[Subgoal] = []
        for item in plan_queue:
            if isinstance(item, dict):
                normalized_queue.append(Subgoal(**item))
            elif isinstance(item, Subgoal):
                normalized_queue.append(item)
        state["plan_queue"] = normalized_queue

    state["trajectory"].append({
        "node": "plan",
        "subgoals": [
            sg.model_dump() if hasattr(sg, "model_dump") else dict(sg)
            for sg in state["plan_queue"]
        ],
    })
    return state


def node_implement(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
) -> State:
    """State 3: IMPLEMENTATION (Executes subgoal mutations in isolated worktree)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    plan_queue = state.get("plan_queue", [])
    current_subgoal = state.get("current_subgoal")

    # If current subgoal is None or previous subgoal passed, pop next from queue
    if current_subgoal is None or state.get("gate_status") == "passed":
        if plan_queue:
            current_subgoal = plan_queue.pop(0)
            state["current_subgoal"] = current_subgoal
            state["gate_status"] = None

    subgoal: Optional[Subgoal] = None
    if isinstance(current_subgoal, dict):
        subgoal = Subgoal(**current_subgoal)
    elif isinstance(current_subgoal, Subgoal):
        subgoal = current_subgoal

    executed_tools: List[Dict[str, Any]] = []

    if llm is not None and subgoal is not None and workspace is not None:
        submitted = False

        @tool
        def stage_file_mutation(path: str, content: str) -> str:
            """Stage a file mutation (create or overwrite file content) in the workspace worktree.

            Args:
                path: Path to the file to modify, relative to repository root.
                content: The complete new content of the file.
            """
            if hasattr(workspace, "stage_file_mutation"):
                workspace.stage_file_mutation(path, content)
                return f"Successfully staged mutation for {path}"
            return f"Workspace unavailable; could not stage mutation for {path}"

        @tool
        def submit_subgoal(notes: str = "") -> str:
            """Signal that the implementation for the current subgoal is complete and ready for gating.

            Args:
                notes: Optional summary or notes about the changes made.
            """
            nonlocal submitted
            submitted = True
            return "Subgoal submitted."

        tools = [stage_file_mutation, submit_subgoal]

        prompt_lines = [
            f"Current Subgoal: {subgoal.description}",
            f"Declared Scope: {json.dumps(subgoal.scope)}",
        ]
        if subgoal.expects_tests:
            prompt_lines.append("Note: This subgoal expects tests to verify its implementation.")
        else:
            prompt_lines.append("Note: This subgoal does not expect tests.")

        if state.get("last_feedback"):
            prompt_lines.append(f"Previous attempt failed gate verification. Feedback:\n{state['last_feedback']}")

        existing_files_context = []
        worktree = getattr(workspace, "worktree_dir", getattr(workspace, "repo_dir", None))
        if worktree and subgoal.scope:
            for s_file in subgoal.scope:
                target_file = Path(worktree) / s_file
                if target_file.exists() and target_file.is_file():
                    try:
                        file_content = target_file.read_text(encoding="utf-8")
                        existing_files_context.append(f"--- Existing file: {s_file} ---\n{file_content}\n--- End of {s_file} ---")
                    except Exception:
                        pass
        if existing_files_context:
            prompt_lines.append("Existing content of files in scope:\n" + "\n\n".join(existing_files_context))

        prompt_lines.append(
            "Instructions:\n"
            "1. Stage all necessary code changes using the `stage_file_mutation` tool.\n"
            "   Only mutate files within the declared scope.\n"
            "2. When all changes are staged and you are done, call the `submit_subgoal` tool to submit your work for gate verification."
        )
        prompt_text = "\n\n".join(prompt_lines)

        messages: List[Any] = [HumanMessage(content=prompt_text)]
        bound_llm = llm.bind_tools(tools)

        max_turns = 10
        turn = 0
        while turn < max_turns and not submitted:
            turn += 1
            response = bound_llm.invoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls and isinstance(response, dict):
                tool_calls = response.get("tool_calls")

            if not tool_calls:
                break

            for tool_call in tool_calls:
                name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
                args = tool_call.get("args") if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
                call_id = tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", f"call_{turn}")

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                executed_tools.append({"name": name, "args": args})

                if name == "submit_subgoal":
                    submitted = True
                    result = "Subgoal submitted."
                elif name in ("stage_file_mutation", "write_file"):
                    actual_path = args.get("path") or args.get("file_path") or args.get("filename") or ""
                    actual_content = args.get("content")
                    if actual_content is None:
                        actual_content = args.get("code", args.get("text", ""))
                    if actual_path and hasattr(workspace, "stage_file_mutation"):
                        try:
                            workspace.stage_file_mutation(actual_path, actual_content)
                            result = f"Successfully staged mutation for {actual_path}"
                        except Exception as e:
                            result = f"Error staging mutation for {actual_path}: {e}"
                    else:
                        result = f"Error: path is required for {name}."
                else:
                    result = f"Unknown tool: {name}. Available tools: stage_file_mutation, submit_subgoal."

                messages.append(ToolMessage(content=str(result), tool_call_id=str(call_id or f"call_{turn}")))

            if submitted:
                break

    subgoal_data = (
        subgoal.model_dump()
        if subgoal and hasattr(subgoal, "model_dump")
        else (dict(subgoal) if subgoal else (dict(current_subgoal) if current_subgoal else None))
    )
    traj_entry: Dict[str, Any] = {"node": "implement", "current_subgoal": subgoal_data}
    if executed_tools:
        traj_entry["tool_calls"] = executed_tools
    state["trajectory"].append(traj_entry)
    return state


def node_gate(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
) -> State:
    """The Two-Tier Gate Router.

    Tier 0 (Mechanical):
      - Run workspace.run_mechanical_checks(subgoal).
      - If fail: rollback_subgoal, increment mechanical_strike_count.
        Do NOT call gatekeeper. Do NOT touch semantic_strike_count.
        If mechanical_strike_count reaches 3, escalate_deadlock.
      - If pass: proceed to Tier 1.

    Tier 1 (Semantic):
      - Call gatekeeper.validate_subgoal(subgoal, diff, mechanical_detail=...).
      - If valid: commit_subgoal, reset BOTH strike counters to 0.
      - If invalid: rollback_subgoal, increment semantic_strike_count.
        Do NOT touch mechanical_strike_count.
        If semantic_strike_count reaches 3, escalate_deadlock.
    """
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    if workspace is None:
        raise ValueError("Workspace must be provided to node_gate.")

    subgoal_raw = state.get("current_subgoal")
    if isinstance(subgoal_raw, dict):
        subgoal = Subgoal(**subgoal_raw)
    elif isinstance(subgoal_raw, Subgoal):
        subgoal = subgoal_raw
    else:
        subgoal = Subgoal()

    # Tier 0: Mechanical checks
    mech_result = workspace.run_mechanical_checks(subgoal)

    if not mech_result.passed:
        workspace.rollback_subgoal()
        current_mech_strikes = state.get("mechanical_strike_count", 0) + 1
        state["mechanical_strike_count"] = current_mech_strikes
        state["gate_status"] = "mechanical_failure"
        state["last_feedback"] = mech_result.detail

        if current_mech_strikes >= 3 and gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
            )
        return state

    # Tier 0 Passed -> Proceed to Tier 1 (Semantic check via Gatekeeper)
    if gatekeeper is None:
        raise ValueError("Gatekeeper must be provided for Tier 1 validation.")

    diff = workspace.get_staged_diff()
    verdict = gatekeeper.validate_subgoal(
        subgoal, diff, mechanical_detail=mech_result.detail
    )

    if verdict.valid:
        workspace.commit_subgoal()
        state["mechanical_strike_count"] = 0
        state["semantic_strike_count"] = 0
        state["gate_status"] = "passed"
        state["last_feedback"] = ""
        return state
    else:
        workspace.rollback_subgoal()
        current_sem_strikes = state.get("semantic_strike_count", 0) + 1
        state["semantic_strike_count"] = current_sem_strikes
        state["gate_status"] = "semantic_failure"
        state["last_feedback"] = verdict.reason or "Semantic validation rejected."

        if current_sem_strikes >= 3 and hasattr(gatekeeper, "escalate_deadlock"):
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="semantic",
            )
        return state


def node_verify(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
) -> State:
    """State 4: VERIFICATION (Executes full test suite and final ticket verification)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    test_output = ""
    if workspace is not None and hasattr(workspace, "run_tests"):
        outcome = workspace.run_tests()
        test_output = getattr(outcome, "value", str(outcome))

    final_diff = workspace.get_staged_diff() if workspace and hasattr(workspace, "get_staged_diff") else ""
    ticket = state.get("ticket", "")

    if gatekeeper is not None and hasattr(gatekeeper, "verify_ticket"):
        verdict = gatekeeper.verify_ticket(ticket, final_diff, test_output)
        if verdict.valid:
            state["gate_status"] = "verified"
            state["status"] = "completed"
        else:
            state["gate_status"] = "verification_failed"
            state["status"] = "verification_failed"
    else:
        state["gate_status"] = "verified"
        state["status"] = "completed"

    state["trajectory"].append({
        "node": "verify",
        "gate_status": state.get("gate_status"),
        "status": state.get("status"),
    })

    if state.get("gate_status") == "verification_failed" and gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
        gatekeeper.escalate_deadlock(
            trajectory=state.get("trajectory", []),
            triggering_tier="verification",
        )
    return state


def node_escalate(
    state: State,
    gatekeeper: Optional[Any] = None,
) -> State:
    """Escalation termination node for HITL review."""
    state["status"] = "escalated"
    return state


def route_gate(state: State) -> str:
    """Evaluates strike counters and plan queue to determine the next graph node."""
    if (
        state.get("mechanical_strike_count", 0) >= 3
        or state.get("semantic_strike_count", 0) >= 3
    ):
        return "escalate"
    if state.get("gate_status") == "passed":
        if state.get("plan_queue"):
            return "implement"
        return "verify"
    return "implement"


class JevEngine:
    """The FSM Engine orchestrating the LangGraph DAG with Two-Tier Gating."""

    def __init__(
        self,
        workspace: Optional[Any] = None,
        gatekeeper: Optional[Any] = None,
        llm: Optional[Any] = None,
        db_path: Optional[Union[str, Path]] = "checkpoints.db",
        checkpointer: Optional[BaseCheckpointSaver] = None,
    ):
        self.workspace = workspace
        self.gatekeeper = gatekeeper
        self.llm = llm

        if checkpointer is not None:
            self.checkpointer = checkpointer
        elif db_path is not None:
            self.checkpointer = SqliteSaver.from_conn_string(str(db_path))
        else:
            self.checkpointer = None

        self.app = self.build_graph()

    def _node_investigate(self, state: State) -> State:
        return node_investigate(state, workspace=self.workspace, llm=self.llm)

    def _node_plan(self, state: State) -> State:
        return node_plan(state, workspace=self.workspace, llm=self.llm)

    def _node_implement(self, state: State) -> State:
        return node_implement(state, workspace=self.workspace, llm=self.llm)

    def node_gate(self, state: State) -> State:
        return node_gate(state, workspace=self.workspace, gatekeeper=self.gatekeeper)

    def _node_verify(self, state: State) -> State:
        return node_verify(state, workspace=self.workspace, gatekeeper=self.gatekeeper)

    def _node_escalate(self, state: State) -> State:
        return node_escalate(state, gatekeeper=self.gatekeeper)

    def _route_after_gate(self, state: State) -> str:
        return route_gate(state)

    def _route_after_verify(self, state: State) -> str:
        if state.get("gate_status") == "verified":
            return END
        return "escalate"

    def build_graph(self) -> Any:
        builder = StateGraph(State)

        builder.add_node("investigate", self._node_investigate)
        builder.add_node("plan", self._node_plan)
        builder.add_node("implement", self._node_implement)
        builder.add_node("gate", self.node_gate)
        builder.add_node("verify", self._node_verify)
        builder.add_node("escalate", self._node_escalate)

        builder.add_edge(START, "investigate")
        builder.add_edge("investigate", "plan")
        builder.add_edge("plan", "implement")
        builder.add_edge("implement", "gate")

        builder.add_conditional_edges(
            "gate",
            self._route_after_gate,
            {
                "implement": "implement",
                "verify": "verify",
                "escalate": "escalate",
            },
        )

        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {
                END: END,
                "escalate": "escalate",
            },
        )

        builder.add_edge("escalate", END)

        return builder.compile(checkpointer=self.checkpointer)

    def execute(self, ticket: str, thread_id: str = "default") -> State:
        initial_state: State = {
            "ticket": ticket,
            "plan_queue": [],
            "current_subgoal": None,
            "mechanical_strike_count": 0,
            "semantic_strike_count": 0,
            "trajectory": [],
            "gate_status": None,
            "last_feedback": None,
            "status": None,
        }

        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return self.app.invoke(initial_state, config=config)
