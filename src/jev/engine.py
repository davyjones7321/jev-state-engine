import json
import re
import sqlite3
import threading
import time
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


def _extract_content_text(content: Any) -> str:
    """Normalize message content to a string, handling lists of text blocks if present."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def node_investigate(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
) -> State:
    """State 1: INVESTIGATION (Read-Only access to codebase)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    # If no LLM is provided, maintain backwards compatibility with static fallback
    if llm is None:
        notes = ""
        if workspace is not None and hasattr(workspace, "run_read_tool"):
            git_status = workspace.run_read_tool("git", ["status", "--porcelain"])
            notes = f"Workspace status:\n{git_status}"
        else:
            notes = "No workspace read tool bound."
        state["last_feedback"] = notes
        state["investigation_notes"] = notes
        state["trajectory"].append({"node": "investigate", "notes": notes})
        return state

    finished = False
    investigation_summary = ""
    executed_tools: List[Dict[str, Any]] = []

    # Bind strictly READ-ONLY tools (State 1 constraint)
    @tool
    def list_dir(path: str = ".") -> str:
        """List files and subdirectories at the given path relative to repository root.

        Args:
            path: Directory path to list, defaults to "." (repository root).
        """
        if hasattr(workspace, "list_dir"):
            try:
                return str(workspace.list_dir(path))
            except Exception as e:
                return f"Error listing directory {path}: {e}"

        worktree = getattr(workspace, "worktree_dir", getattr(workspace, "repo_dir", Path.cwd()))
        base = Path(worktree) if worktree else Path.cwd()
        target = (base / (path or ".")).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return f"Error: Access denied for path outside workspace: {path}"

        if not target.exists():
            return f"Directory not found: {path}"
        if not target.is_dir():
            return f"Not a directory: {path}"

        ignore_names = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}
        entries = []
        for child in sorted(target.iterdir()):
            if child.name in ignore_names:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
        return "\n".join(entries) if entries else "(empty directory)"

    @tool
    def grep(query: str, path: Optional[str] = None) -> str:
        """Search for string patterns or regex matches across files in the workspace.

        Args:
            query: String or regex pattern to search for.
            path: Optional subdirectory or file path to limit the search.
        """
        if not query:
            return "Error: query parameter is required for grep."

        worktree = getattr(workspace, "worktree_dir", getattr(workspace, "repo_dir", Path.cwd()))
        base = Path(worktree).resolve() if worktree else Path.cwd().resolve()
        safe_rel_path = None
        search_target = base

        if path:
            target = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            try:
                safe_rel_path = target.relative_to(base).as_posix()
            except ValueError:
                return f"Error: Access denied for path outside workspace: {path}"
            search_target = target

        if hasattr(workspace, "grep"):
            try:
                return str(workspace.grep(query, path))
            except Exception as e:
                return f"Error running grep: {e}"

        if workspace is not None and hasattr(workspace, "run_read_tool"):
            args = ["grep", "-n", "-I", "--untracked", query]
            if safe_rel_path:
                args.extend(["--", safe_rel_path])
            try:
                res = workspace.run_read_tool("git", args)
                if "fatal: not a git repository" not in res and "Command not found" not in res:
                    return res.strip() if res.strip() else "No matches found."
            except Exception:
                pass

        # Python regex search fallback
        import re
        if not search_target.exists():
            return f"Path not found: {path}"

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except Exception:
            pattern = re.compile(re.escape(query))

        ignore_dirs = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}
        target_files = [search_target] if search_target.is_file() else [
            p for p in search_target.rglob("*")
            if p.is_file() and not any(part in ignore_dirs for part in p.parts)
        ]
        matches = []
        for f in target_files:
            try:
                rel = f.relative_to(base)
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                for idx, line in enumerate(lines, 1):
                    if pattern.search(line):
                        matches.append(f"{rel}:{idx}:{line}")
                        if len(matches) >= 50:
                            break
            except Exception:
                continue
            if len(matches) >= 50:
                break
        return "\n".join(matches) if matches else "No matches found."

    @tool
    def read_file(path: str) -> str:
        """Read the full content of a file in the workspace. Backed by workspace.run_read_tool().

        Args:
            path: Relative path to the file from repository root.
        """
        if not path:
            return "Error: path is required for read_file."

        worktree = getattr(workspace, "worktree_dir", getattr(workspace, "repo_dir", None))
        if worktree:
            base = Path(worktree).resolve()
            target = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                return f"Error: Access denied for path outside workspace: {path}"

        if workspace is not None and hasattr(workspace, "run_read_tool"):
            try:
                output = workspace.run_read_tool("cat", [path])
                if output is not None and "Command not found: cat" not in output:
                    return output
            except Exception:
                pass

        if worktree:
            if target.exists():
                if target.is_dir():
                    return f"Error: {path} is a directory, not a file. Use list_dir instead."
                try:
                    return target.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    return f"Error reading file {path}: {e}"
            return f"File not found: {path}"
        return f"Workspace unavailable; could not read {path}"

    @tool
    def finish_investigation(summary: str = "") -> str:
        """Signal that investigation is complete and provide a comprehensive summary of findings to inform planning.

        Args:
            summary: Comprehensive summary of code patterns, architecture, relevant files, and implementation findings.
        """
        nonlocal finished, investigation_summary
        finished = True
        investigation_summary = summary
        return "Investigation finished."

    # Bind strictly READ-ONLY tools (no write tools)
    tools = [list_dir, grep, read_file, finish_investigation]

    ticket = state.get("ticket", "")
    prompt_lines = [
        f"Task Ticket: {ticket}",
        "",
        "Instructions:",
        "1. You have read-only access to investigate the codebase using the available tools: `list_dir`, `grep`, and `read_file`.",
        "2. Explore the file structure, find relevant files, and understand existing patterns and architecture.",
        "3. When you have gathered enough context to plan the implementation, call `finish_investigation` with a comprehensive summary of your findings.",
    ]
    prompt_text = "\n".join(prompt_lines)

    messages: List[Any] = [HumanMessage(content=prompt_text)]
    bound_llm = llm.bind_tools(tools)

    max_turns = 10
    turn = 0
    while turn < max_turns and not finished:
        turn += 1
        response = None
        for api_attempt in range(5):
            try:
                response = bound_llm.invoke(messages)
                break
            except Exception as e:
                if api_attempt == 4:
                    raise
                sleep_time = 25 if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else (5 * (api_attempt + 1))
                time.sleep(sleep_time)

        messages.append(response)

        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls and isinstance(response, dict):
            tool_calls = response.get("tool_calls")

        if not tool_calls:
            if not investigation_summary:
                investigation_summary = _extract_content_text(getattr(response, "content", "")).strip()
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

            if name == "finish_investigation":
                finished = True
                extracted = (
                    args.get("summary")
                    or args.get("notes")
                    or args.get("investigation_notes")
                    or args.get("findings")
                    or args.get("result")
                    or args.get("analysis")
                    or args.get("summary_text")
                    or ""
                )
                if extracted:
                    investigation_summary = _extract_content_text(extracted)
                else:
                    investigation_summary = _extract_content_text(getattr(response, "content", "")).strip()
                result = "Investigation finished."
            elif name == "read_file":
                actual_path = args.get("path") or args.get("file_path") or args.get("filename") or ""
                result = read_file.invoke({"path": actual_path}) if hasattr(read_file, "invoke") else read_file(actual_path)
            elif name == "list_dir":
                actual_path = args.get("path") or args.get("directory") or args.get("dir_path") or "."
                result = list_dir.invoke({"path": actual_path}) if hasattr(list_dir, "invoke") else list_dir(actual_path)
            elif name == "grep":
                actual_query = args.get("query") or args.get("pattern") or args.get("search_term") or ""
                actual_path = args.get("path") or args.get("directory") or args.get("file_path")
                call_args = {"query": actual_query}
                if actual_path:
                    call_args["path"] = actual_path
                result = grep.invoke(call_args) if hasattr(grep, "invoke") else grep(actual_query, actual_path)
            else:
                result = f"Error: Tool '{name}' is not permitted in investigation state. Only read-only tools (list_dir, grep, read_file, finish_investigation) are available."

            messages.append(
                ToolMessage(
                    content=str(result),
                    name=name,
                    tool_call_id=str(call_id or f"call_{turn}"),
                )
            )

        if finished:
            break

    if not investigation_summary:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                text = _extract_content_text(getattr(msg, "content", "")).strip()
                if text:
                    investigation_summary = text
                    break
        if not investigation_summary:
            investigation_summary = f"Investigation concluded after reaching maximum turns ({max_turns})."

    state["investigation_notes"] = investigation_summary
    state["last_feedback"] = investigation_summary

    traj_entry: Dict[str, Any] = {
        "node": "investigate",
        "notes": investigation_summary,
    }
    if executed_tools:
        traj_entry["tool_calls"] = executed_tools
    state["trajectory"].append(traj_entry)
    return state


class PlanSubgoalModel(BaseModel):
    """Strict schema for validating LLM-generated Subgoal outputs."""

    model_config = ConfigDict(strict=True)

    description: str = Field(..., min_length=1)
    scope: List[str] = Field(..., min_length=1)
    expects_tests: bool = True

    @field_validator("scope")
    @classmethod
    def validate_scope_items(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("Subgoal scope must not be empty.")
        cleaned_list: List[str] = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("Scope items must be non-empty strings.")
            cleaned_item = item.strip().replace("\\", "/")
            path_obj = Path(cleaned_item)
            is_abs = (
                path_obj.is_absolute()
                or bool(path_obj.drive)
                or cleaned_item.startswith("/")
                or cleaned_item.startswith("\\")
            )
            if is_abs or ".." in path_obj.parts:
                raise ValueError(f"Scope item '{item}' must be a relative path within the repository.")
            if cleaned_item not in cleaned_list:
                cleaned_list.append(cleaned_item)
        return cleaned_list

    @field_validator("description")
    @classmethod
    def validate_description_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Subgoal description must be non-empty.")
        return v.strip()


def _clean_json_text(text: str) -> str:
    cleaned = text.strip()

    # 1. Look specifically for a ```json ... ``` code fence (case-insensitive)
    match_json = re.search(r"```json\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if match_json:
        return match_json.group(1).strip()

    # 2. Look for any generic code fence ``` ... ```
    match_generic = re.search(r"```\s*([\s\S]*?)\s*```", cleaned)
    if match_generic:
        return match_generic.group(1).strip()

    # 3. Check for raw array bracket [ ... ] if root is not an object dict { ... }
    start_bracket = cleaned.find("[")
    end_bracket = cleaned.rfind("]")
    if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
        start_brace = cleaned.find("{")
        if start_brace == -1 or start_brace > start_bracket:
            return cleaned[start_bracket : end_bracket + 1].strip()

    return cleaned


def _parse_and_validate_plan(raw_text: str) -> List[Subgoal]:
    """Parse raw LLM output as JSON and validate strictly against Subgoal schema."""
    cleaned = _clean_json_text(raw_text)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"Plan output must be a JSON array of Subgoal objects, got {type(data).__name__}")
    if len(data) == 0:
        raise ValueError("Plan output must contain at least one subgoal, got empty array")

    subgoals: List[Subgoal] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Subgoal at index {idx} must be a JSON object, got {type(item).__name__}")
        validated = PlanSubgoalModel.model_validate(item)
        subgoals.append(
            Subgoal(
                description=validated.description,
                scope=validated.scope,
                expects_tests=validated.expects_tests,
            )
        )
    return subgoals


def _validate_plan_grounding(
    subgoals: List[Subgoal],
    workspace: Optional[Any],
    ticket: str,
    investigation_notes: str,
) -> None:
    """Validate that subgoal scopes are grounded in the repository or investigation notes."""
    ticket_lower = ticket.lower()
    notes_lower = (investigation_notes or "").lower()

    # Determine if ticket specifically targets existing code
    targets_existing = bool(
        re.search(r"\b(existing|undocumented|current)\b", ticket_lower)
    )

    # Determine if ticket requests creating new files or implementing new features
    creates_new = bool(
        re.search(r"\b(create|new\s+file|scaffold|implement|build|generate|add)\b", ticket_lower)
    )

    # Resolve workspace root directory if available
    base_dir: Optional[Path] = None
    if workspace is not None:
        raw_base = getattr(workspace, "worktree_dir", None) or getattr(workspace, "repo_dir", None)
        if raw_base is not None:
            base_dir = Path(raw_base).resolve()

    # Extract all file extensions mentioned in investigation_notes (e.g. .ts, .py, .go, .rs, .js)
    notes_extensions = set(re.findall(r"\.([a-zA-Z0-9_-]+)\b", notes_lower))

    for subgoal in subgoals:
        subgoal_desc_lower = subgoal.description.lower()
        subgoal_targets_existing = targets_existing or bool(
            re.search(r"\b(existing|undocumented|current)\b", subgoal_desc_lower)
        )

        for scope_item in subgoal.scope:
            cleaned_path = scope_item.strip().replace("\\", "/")
            path_obj = Path(cleaned_path)
            file_name = path_obj.name.lower()
            file_stem = path_obj.stem.lower()
            ext = path_obj.suffix.lower().lstrip(".")

            # 1. Check if the file exists on disk in the workspace
            exists_on_disk = False
            if base_dir is not None:
                try:
                    full_target = (base_dir / cleaned_path).resolve()
                    exists_on_disk = full_target.exists() and (full_target.is_file() or full_target.is_dir())
                except Exception:
                    exists_on_disk = False

            if exists_on_disk:
                continue

            # 2. Check if file path, filename, or meaningful stem is explicitly present in notes or ticket
            stem_in_notes = len(file_stem) >= 3 and bool(re.search(r"\b" + re.escape(file_stem) + r"\b", notes_lower))
            stem_in_ticket = len(file_stem) >= 3 and bool(re.search(r"\b" + re.escape(file_stem) + r"\b", ticket_lower))
            in_notes = (cleaned_path.lower() in notes_lower) or (file_name in notes_lower) or stem_in_notes
            in_ticket = (cleaned_path.lower() in ticket_lower) or (file_name in ticket_lower) or stem_in_ticket

            if in_notes or in_ticket:
                continue

            # 3. If file does not exist on disk, and is not in notes, and not in ticket:
            # Case A: Ticket or subgoal explicitly targets existing code/functions
            if subgoal_targets_existing:
                raise ValueError(
                    f"Scope file '{scope_item}' does not exist in repository and was not found during investigation for ticket modifying existing code."
                )

            # Case B: Investigation notes found specific files, but the proposed file has an alien extension
            # that was never found during investigation (e.g. .py proposed for a .ts repo)
            if notes_extensions and ext and (ext not in notes_extensions):
                # Check if the extension exists on disk in workspace
                ext_exists_in_workspace = False
                if base_dir is not None:
                    try:
                        ext_exists_in_workspace = any(base_dir.glob(f"*.{ext}")) or any(base_dir.glob(f"*/*.{ext}"))
                    except Exception:
                        pass
                if not ext_exists_in_workspace:
                    raise ValueError(
                        f"Scope file '{scope_item}' has file extension '.{ext}' which does not exist in the repository and was not found during investigation."
                    )

            # Case C: If investigation notes exist and ticket does NOT request creating new files,
            # touching uninvestigated non-existent files is ungrounded
            if notes_lower.strip() and not creates_new:
                raise ValueError(
                    f"Scope file '{scope_item}' does not exist in repository and was not identified in investigation notes."
                )


def node_plan(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
) -> State:
    """State 2: PLANNING (Generates structured Subgoals with declared scope).

    We use standard prompt instructions + manual JSON parsing with Pydantic validation
    (PlanSubgoalModel) rather than provider-bound .with_structured_output().
    Rationale:
    1. Complete provider agnosticism: Works identically across any LangChain BaseChatModel
       (Gemini, OpenAI, Claude, local models) and scripted/mock test models without requiring
       vendor-specific tool-calling or JSON-mode schema compilation.
    2. Strict Rule 0 enforcement: Direct visibility into JSONDecodeError and pydantic.ValidationError
       allows exact error messages to be reflected back in the retry prompt context.
    3. Deterministic failure boundaries: Guarantees zero silent coercions, hard stop on retry failure.
    """
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    # llm=None backwards compatibility branch (matching 7a/7b pattern)
    if llm is None:
        plan_queue = state.get("plan_queue")
        if not plan_queue:
            ticket = state.get("ticket", "Default task")
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

        state["current_subgoal"] = None
        state["last_feedback"] = None
        state["gate_status"] = None
        state["trajectory"].append({
            "node": "plan",
            "subgoals": [
                sg.model_dump() if hasattr(sg, "model_dump") else dict(sg)
                for sg in state["plan_queue"]
            ],
        })
        return state

    ticket = state.get("ticket", "")
    investigation_notes = state.get("investigation_notes", "")

    prompt_lines = [
        "You are the Planning module of the Jev State Engine.",
        "Your role is to create a deterministic, structured execution plan broken down into atomic Subgoals.",
        "",
        f"Ticket Description:\n{ticket}",
    ]
    if investigation_notes:
        prompt_lines.append(f"\nInvestigation Notes:\n{investigation_notes}")
    else:
        prompt_lines.append("\nInvestigation Notes: None")

    prompt_lines.extend([
        "",
        "Instructions:",
        "1. Break down the task into an ordered sequence of atomic Subgoals.",
        "2. For each Subgoal, specify:",
        "   - 'description': Clear, concise explanation of the atomic change.",
        "   - 'scope': Non-empty list of exact file paths to touch or create. No placeholder or empty scopes allowed.",
        "   - 'expects_tests': Boolean (true/false) indicating whether tests are expected to pass/run for this step.",
        "3. GROUNDING REQUIREMENTS (CRITICAL):",
        "   - If modifying, extending, or documenting existing code, every file path in 'scope' MUST be grounded strictly in the files discovered in 'Investigation Notes' or explicitly named in the 'Ticket Description'.",
        "   - Do NOT invent, hallucinate, or guess file paths.",
        "   - Do NOT assume any default project language or file extensions (e.g. do NOT assume Python 'src/main.py' if the repository is TypeScript, Go, Rust, or JavaScript). Use the actual language and paths discovered in Investigation Notes.",
        "   - If the ticket explicitly requests creating brand new files not previously existing, those new paths must be consistent with the directory structure established in Investigation Notes.",
        "4. Output format:",
        "   Return ONLY a valid JSON array of Subgoal objects. Do NOT include markdown commentary or explanations outside the JSON array.",
        "   Schema illustration:",
        '   [{"description": "Atomic change description", "scope": ["relative/path/to/target/file"], "expects_tests": true}]',
    ])
    prompt_text = "\n".join(prompt_lines)
    messages: List[Any] = [HumanMessage(content=prompt_text)]

    subgoals: Optional[List[Subgoal]] = None
    last_error: Optional[str] = None
    retries_used = 0

    for attempt in range(2):
        # API call with retry backoff for rate limiting
        response = None
        for api_attempt in range(5):
            try:
                response = llm.invoke(messages)
                break
            except Exception as e:
                if api_attempt == 4:
                    state["plan_queue"] = []
                    state["current_subgoal"] = None
                    state["gate_status"] = "planning_failed"
                    state["status"] = "escalated"
                    state["last_feedback"] = f"Planning API failure after 5 retries: {e}"
                    state["trajectory"].append({
                        "node": "plan",
                        "error_type": "api_failure",
                        "error": f"API failure after 5 retries: {e}",
                        "subgoals": [],
                    })
                    if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                        gatekeeper.escalate_deadlock(
                            trajectory=state["trajectory"],
                            triggering_tier="planning",
                        )
                    return state
                sleep_time = 25 if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else (5 * (api_attempt + 1))
                time.sleep(sleep_time)

        raw_content = _extract_content_text(getattr(response, "content", response))

        try:
            parsed_subgoals = _parse_and_validate_plan(raw_content)
            _validate_plan_grounding(parsed_subgoals, workspace, ticket, investigation_notes)
            subgoals = parsed_subgoals
            break
        except Exception as exc:
            subgoals = None
            last_error = str(exc)
            if attempt == 0:
                retries_used = 1
                messages.append(response if isinstance(response, AIMessage) else AIMessage(content=raw_content))
                retry_prompt = (
                    f"your previous output failed validation because: {last_error}, please retry. "
                    "Return ONLY a valid JSON array of Subgoal objects matching the required schema: "
                    "description (non-empty string), scope (non-empty list of file paths), expects_tests (boolean)."
                )
                messages.append(HumanMessage(content=retry_prompt))

    if subgoals is not None:
        state["plan_queue"] = subgoals
        state["current_subgoal"] = None
        state["last_feedback"] = None
        state["gate_status"] = None
        traj_entry: Dict[str, Any] = {
            "node": "plan",
            "subgoals": [sg.model_dump() for sg in subgoals],
        }
        if retries_used > 0:
            traj_entry["retries"] = retries_used
        state["trajectory"].append(traj_entry)
        return state

    # Hard failure: planning failed after retry
    state["plan_queue"] = []
    state["current_subgoal"] = None
    state["gate_status"] = "planning_failed"
    state["status"] = "escalated"
    state["last_feedback"] = f"Planning failed after retry: {last_error}"
    state["trajectory"].append({
        "node": "plan",
        "error_type": "validation_failure",
        "error": f"Planning validation failed: {last_error}",
        "subgoals": [],
    })
    if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
        gatekeeper.escalate_deadlock(
            trajectory=state["trajectory"],
            triggering_tier="planning",
        )
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
            response = None
            for api_attempt in range(5):
                try:
                    response = bound_llm.invoke(messages)
                    break
                except Exception as e:
                    if api_attempt == 4:
                        raise
                    sleep_time = 25 if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else (5 * (api_attempt + 1))
                    time.sleep(sleep_time)

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

                messages.append(
                    ToolMessage(
                        content=str(result),
                        name=name,
                        tool_call_id=str(call_id or f"call_{turn}"),
                    )
                )

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


def route_plan(state: State) -> str:
    """Evaluates whether planning succeeded or failed to determine next node."""
    if state.get("gate_status") == "planning_failed":
        return "escalate"
    return "implement"


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
        return node_plan(state, workspace=self.workspace, llm=self.llm, gatekeeper=self.gatekeeper)

    def _node_implement(self, state: State) -> State:
        return node_implement(state, workspace=self.workspace, llm=self.llm)

    def node_gate(self, state: State) -> State:
        return node_gate(state, workspace=self.workspace, gatekeeper=self.gatekeeper)

    def _node_verify(self, state: State) -> State:
        return node_verify(state, workspace=self.workspace, gatekeeper=self.gatekeeper)

    def _node_escalate(self, state: State) -> State:
        return node_escalate(state, gatekeeper=self.gatekeeper)

    def _route_after_plan(self, state: State) -> str:
        return route_plan(state)

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

        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {
                "implement": "implement",
                "escalate": "escalate",
            },
        )
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
