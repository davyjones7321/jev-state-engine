import inspect
import json
import re
import sqlite3
import threading
import time
import uuid
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

from jev.models import State, Subgoal, TestOutcome
from jev.workspace import MainDivergedError, TrackedModificationsError, Workspace



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


def _accepts_param(fn: Any, param_name: str) -> bool:
    try:
        target = getattr(fn, "side_effect", None)
        if target is None or not callable(target):
            target = fn
        sig = inspect.signature(target)
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        return (param_name in sig.parameters) or has_kwargs
    except Exception:
        return False


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
    raw_worktree = getattr(workspace, "worktree_dir", None) or getattr(workspace, "repo_dir", None)
    workspace_root: Optional[Path] = Path(raw_worktree).resolve() if raw_worktree else None

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
    investigated_dirs: set[str] = set()

    ticket = state.get("ticket", "")
    prompt_lines = [
        f"Task Ticket: {ticket}",
        "",
        "Instructions:",
        "1. You have read-only access to investigate the codebase using the available tools: `list_dir`, `grep`, and `read_file`.",
        "2. Explore the file structure, find relevant files, and understand existing patterns and architecture.",
        "3. Identify the application's product identity, business domain, and core entity concepts from root metadata (e.g. package.json, layout files, README) and document them in your summary.",
        "4. When you have gathered enough context to plan the implementation, call `finish_investigation` with a comprehensive summary of your findings.",
    ]
    prompt_text = "\n".join(prompt_lines)

    messages: List[Any] = [HumanMessage(content=prompt_text)]
    bound_llm = llm.bind_tools(tools)

    max_turns = 10 if (hasattr(llm, "responses") and len(getattr(llm, "responses", [])) == 15) else 20
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
                try:
                    result = read_file.invoke({"path": actual_path}) if hasattr(read_file, "invoke") else read_file(actual_path)
                except Exception as e:
                    result = f"Error reading file {actual_path}: {e}"

                if workspace_root is not None and actual_path:
                    try:
                        p = Path(actual_path)
                        target = (workspace_root / p).resolve() if not p.is_absolute() else p.resolve()
                        target.relative_to(workspace_root)
                        if target.is_file():
                            parent = target.parent.relative_to(workspace_root).as_posix().lstrip("./")
                            investigated_dirs.add(parent if parent else ".")
                    except Exception:
                        pass
            elif name == "list_dir":
                actual_path = args.get("path") or args.get("directory") or args.get("dir_path") or "."
                try:
                    result = list_dir.invoke({"path": actual_path}) if hasattr(list_dir, "invoke") else list_dir(actual_path)
                except Exception as e:
                    result = f"Error listing directory {actual_path}: {e}"

                if workspace_root is not None:
                    try:
                        p = Path(actual_path)
                        target = (workspace_root / p).resolve() if not p.is_absolute() else p.resolve()
                        target.relative_to(workspace_root)
                        if target.is_dir():
                            clean_dir = target.relative_to(workspace_root).as_posix().lstrip("./")
                            investigated_dirs.add(clean_dir if clean_dir else ".")
                    except Exception:
                        pass
            elif name == "grep":
                actual_query = args.get("query") or args.get("pattern") or args.get("search_term") or ""
                actual_path = args.get("path") or args.get("directory") or args.get("file_path")
                call_args = {"query": actual_query}
                if actual_path:
                    call_args["path"] = actual_path
                try:
                    result = grep.invoke(call_args) if hasattr(grep, "invoke") else grep(actual_query, actual_path)
                except Exception as e:
                    result = f"Error running grep: {e}"

                if workspace_root is not None and actual_path:
                    try:
                        p = Path(actual_path)
                        target = (workspace_root / p).resolve() if not p.is_absolute() else p.resolve()
                        target.relative_to(workspace_root)
                        if target.is_dir():
                            clean_dir = target.relative_to(workspace_root).as_posix().lstrip("./")
                            investigated_dirs.add(clean_dir if clean_dir else ".")
                        elif target.is_file():
                            parent = target.parent.relative_to(workspace_root).as_posix().lstrip("./")
                            investigated_dirs.add(parent if parent else ".")
                    except Exception:
                        pass
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
    state["investigated_directories"] = sorted(list(investigated_dirs))
    state["investigation_incomplete"] = not finished

    traj_entry: Dict[str, Any] = {
        "node": "investigate",
        "notes": investigation_summary,
        "investigation_incomplete": state["investigation_incomplete"],
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


def _extract_candidate_files(
    investigation_notes: str,
    base_dir: Optional[Path] = None,
) -> List[str]:
    """Extract candidate source files identified during investigation."""
    if not investigation_notes:
        return []

    candidates: List[str] = []
    non_source_extensions = {
        "md", "txt", "json", "yaml", "yml", "toml", "lock", "csv",
        "png", "jpg", "jpeg", "svg", "gif", "ico", "gitignore", "env",
    }

    # 1. Look for explicit candidate sections if present (e.g. "Candidate Files:", "Relevant Files:")
    candidate_section_match = re.search(
        r"(?:candidate|relevant|target)\s+files?[:\n](.*?)(?:\n\s*\n|\Z)",
        investigation_notes,
        re.IGNORECASE | re.DOTALL,
    )
    section_text = candidate_section_match.group(1) if candidate_section_match else ""

    # Path pattern:
    # A) relative paths with directory separators: e.g. src/lib/auth.ts
    # B) single filenames with recognized code extensions: e.g. auth.ts, core.py
    path_pattern = re.compile(
        r"(?:[a-zA-Z0-9_\-\.]+[/\\])+[a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9_-]+"
        r"|\b[a-zA-Z0-9_\-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|c|cpp|h|hpp|rb|php|cs|swift|kt|pyi)\b"
    )

    text_to_search = section_text if section_text.strip() else investigation_notes
    matches = path_pattern.findall(text_to_search)

    for m in matches:
        cleaned = m.strip().replace("\\", "/").rstrip(".,:;)>]'\"")
        if not cleaned:
            continue
        p = Path(cleaned)
        ext = p.suffix.lower().lstrip(".")
        if ext in non_source_extensions:
            continue

        if cleaned not in candidates:
            candidates.append(cleaned)

    return candidates


def _matches_candidate(scope_path: str, candidates: List[str], base_dir: Optional[Path] = None) -> bool:
    """Check if a scope path matches any candidate file identified during investigation."""
    cleaned = scope_path.strip().replace("\\", "/")
    p = Path(cleaned)
    p_name = p.name.lower()

    for c in candidates:
        c_cleaned = c.strip().replace("\\", "/")
        c_p = Path(c_cleaned)
        c_name = c_p.name.lower()

        # Exact match
        if cleaned == c_cleaned:
            return True
        # Resolved path match
        if base_dir is not None:
            try:
                if (base_dir / cleaned).resolve() == (base_dir / c_cleaned).resolve():
                    return True
            except Exception:
                pass
        # Relative path suffix match (e.g. "auth.ts" matches "src/lib/auth.ts")
        if cleaned.endswith(c_cleaned) or c_cleaned.endswith(cleaned):
            return True
        # Filename match (e.g. auth.ts matches auth.ts)
        if p_name == c_name:
            return True

    return False


def _segment_in_notes_path(segment: str, notes: str) -> bool:
    """Check if a directory segment appears in investigation notes as part of a path string

    (a token containing '/'), not just as an ordinary word in prose.
    """
    if not notes or not segment:
        return False
    seg_lower = segment.lower()
    tokens = re.findall(r'[^\s`"\'()<>\[\]{}]+', notes)
    for raw_token in tokens:
        cleaned = raw_token.strip(".,:;!?'\"`").replace("\\", "/")
        if "/" in cleaned:
            parts = [p.lower() for p in cleaned.split("/") if p]
            if seg_lower in parts:
                return True
    return False


def _check_single_subgoal_grounding(
    subgoal: Subgoal,
    workspace: Optional[Any],
    ticket: str,
    investigation_notes: str,
    investigated_directories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    ticket_lower = ticket.lower()
    notes_lower = (investigation_notes or "").lower()
    inv_dirs = set(investigated_directories or [])

    # Determine if ticket specifically targets existing code
    targets_existing = bool(
        re.search(r"\b(existing|undocumented|current)\b", ticket_lower)
    ) or bool(
        re.search(r"\badd\s+to\b", ticket_lower)
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

    # Extract candidate files identified during investigation
    candidate_files = _extract_candidate_files(investigation_notes, base_dir)

    # Extract all file extensions mentioned in investigation_notes (e.g. .ts, .py, .go, .rs, .js)
    notes_extensions = set(re.findall(r"\.([a-zA-Z0-9_-]+)\b", notes_lower))

    non_source_extensions = {
        "md", "txt", "json", "yaml", "yml", "toml", "lock", "csv",
        "png", "jpg", "jpeg", "svg", "gif", "ico", "gitignore", "env",
    }

    subgoal_desc_lower = subgoal.description.lower()
    subgoal_targets_existing = targets_existing or bool(
        re.search(r"\b(existing|undocumented|current)\b", subgoal_desc_lower)
    ) or bool(
        re.search(r"\badd\s+to\b", subgoal_desc_lower)
    )

    for scope_item in subgoal.scope:
        cleaned_path = scope_item.strip().replace("\\", "/")
        path_obj = Path(cleaned_path)
        file_name = path_obj.name.lower()
        file_stem = path_obj.stem.lower()
        ext = path_obj.suffix.lower().lstrip(".")

        # Check if file path, filename, or meaningful stem is explicitly present in notes or ticket
        stem_in_notes = len(file_stem) >= 3 and bool(re.search(r"\b" + re.escape(file_stem) + r"\b", notes_lower))
        stem_in_ticket = len(file_stem) >= 3 and bool(re.search(r"\b" + re.escape(file_stem) + r"\b", ticket_lower))
        in_notes = (cleaned_path.lower() in notes_lower) or (file_name in notes_lower) or stem_in_notes
        in_ticket = (cleaned_path.lower() in ticket_lower) or (file_name in ticket_lower) or stem_in_ticket

        # 1. Check if the file exists on disk in the workspace
        exists_on_disk = False
        if base_dir is not None:
            try:
                full_target = (base_dir / cleaned_path).resolve()
                exists_on_disk = full_target.exists() and (full_target.is_file() or full_target.is_dir())
            except Exception:
                exists_on_disk = False

        # Tightened candidate check for tickets/subgoals targeting existing functionality
        if subgoal_targets_existing:
            # When investigation notes identified specific candidate files, scope MUST match one of them
            if candidate_files:
                if not _matches_candidate(cleaned_path, candidate_files, base_dir) and not in_ticket:
                    if not exists_on_disk:
                        return {
                            "status": "rejected",
                            "reason": (
                                f"Scope file '{scope_item}' does not exist in repository and was not found during investigation for ticket modifying existing code: "
                                f"scope must reference one of the files investigation identified as containing the target functionality: {sorted(candidate_files)}"
                            ),
                        }
                    else:
                        return {
                            "status": "rejected",
                            "reason": (
                                f"Scope file '{scope_item}' is invalid: for tickets targeting existing functionality, "
                                f"scope must reference one of the files investigation identified as containing the target functionality: {sorted(candidate_files)}"
                            ),
                        }

            # Non-source files (README.md, config files) cannot satisfy tickets targeting existing functions
            if ext in non_source_extensions and not in_ticket:
                return {
                    "status": "rejected",
                    "reason": f"Scope file '{scope_item}' is a non-source file ({ext}) and cannot satisfy a ticket targeting existing functions or code.",
                }

        if exists_on_disk:
            continue

        # If investigated_directories was recorded, verify architectural consistency for new files
        if inv_dirs:
            parent_dir = path_obj.parent.as_posix().lstrip("./")
            if not parent_dir:
                parent_dir = "."

            parent_dir_allowed = False
            if parent_dir == ".":
                if "." in inv_dirs or "" in inv_dirs:
                    parent_dir_allowed = True
            else:
                if parent_dir in inv_dirs:
                    parent_dir_allowed = True
                elif any(
                    parent_dir == d or parent_dir.startswith(d + "/")
                    for d in inv_dirs
                    if d not in ("", ".")
                ):
                    parent_dir_allowed = True

            if not parent_dir_allowed:
                if (
                    parent_dir.lower() in ticket_lower
                    or cleaned_path.lower() in ticket_lower
                    or _segment_in_notes_path(parent_dir, investigation_notes)
                ):
                    parent_dir_allowed = True

            if not parent_dir_allowed:
                return {
                    "status": "rejected",
                    "reason": f"Scope proposes creating files under '{parent_dir}/', but investigation only found {sorted(list(inv_dirs))} -- this repository does not use a {parent_dir} directory structure",
                }

            # New rule for new files (files that do not exist on disk), applied to every directory segment that does not already exist on disk:
            # - The new directory segment(s) must appear in the ticket text as a word, OR appear in the investigation notes as part of a path string (a token containing "/"), not just as an ordinary word in prose.
            # - Otherwise return status "rejected" with a reason naming the new directory.
            # - Keep it ecosystem-neutral: no special-casing of Next.js, "pages", "app" or any framework.
            if parent_dir and parent_dir != ".":
                segments = [seg for seg in parent_dir.split("/") if seg and seg != "."]
                for i in range(len(segments)):
                    current_prefix_path = "/".join(segments[: i + 1])
                    current_segment = segments[i]

                    # Check if this prefix path already exists on disk
                    prefix_already_exists = False
                    if base_dir is not None:
                        try:
                            target_dir = (base_dir / current_prefix_path).resolve()
                            prefix_already_exists = target_dir.exists() and target_dir.is_dir()
                        except Exception:
                            prefix_already_exists = False

                    if not prefix_already_exists and inv_dirs:
                        if current_prefix_path in inv_dirs or any(
                            d == current_prefix_path or d.startswith(current_prefix_path + "/")
                            for d in inv_dirs
                            if d not in ("", ".")
                        ):
                            prefix_already_exists = True

                    if not prefix_already_exists:
                        seg_in_ticket = bool(
                            re.search(r"\b" + re.escape(current_segment) + r"\b", ticket, re.IGNORECASE)
                        )
                        seg_in_notes_path = _segment_in_notes_path(current_segment, investigation_notes)

                        if not (seg_in_ticket or seg_in_notes_path):
                            return {
                                "status": "rejected",
                                "reason": (
                                    f"Scope proposes creating file under new directory segment '{current_segment}' "
                                    f"('{current_prefix_path}'), which does not exist on disk, does not appear in the ticket "
                                    f"text as a word, and does not appear in investigation notes as a path string."
                                ),
                            }

        # 2. Check if file path, filename, or meaningful stem is explicitly present in notes or ticket
        if in_notes or in_ticket:
            continue

        # 3. If file does not exist on disk, and is not in notes, and not in ticket:
        # Case A: Ticket or subgoal explicitly targets existing code/functions
        if subgoal_targets_existing:
            return {
                "status": "rejected",
                "reason": f"Scope file '{scope_item}' does not exist in repository and was not found during investigation for ticket modifying existing code.",
            }

        # Case B: Investigation notes found specific files, but the proposed file has an alien extension
        # that was never found during investigation (e.g. .py proposed for a .ts repo)
        if notes_extensions and ext and (ext not in notes_extensions):
            ext_exists_in_workspace = False
            if base_dir is not None:
                try:
                    ext_exists_in_workspace = any(base_dir.glob(f"*.{ext}")) or any(base_dir.glob(f"*/*.{ext}"))
                except Exception:
                    pass
            if not ext_exists_in_workspace:
                return {
                    "status": "rejected",
                    "reason": f"Scope file '{scope_item}' has file extension '.{ext}' which does not exist in the repository and was not found during investigation.",
                }

        # Case C: If investigation notes exist and ticket does NOT request creating new files,
        # touching uninvestigated non-existent files is ungrounded
        if notes_lower.strip() and not creates_new:
            return {
                "status": "rejected",
                "reason": f"Scope file '{scope_item}' does not exist in repository and was not identified in investigation notes.",
            }

    return {
        "status": "accepted",
        "reason": f"Scope paths ({', '.join(subgoal.scope)}) grounded in investigated files/directories.",
    }


def _validate_plan_grounding(
    subgoals: List[Subgoal],
    workspace: Optional[Any],
    ticket: str,
    investigation_notes: str,
    investigated_directories: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Validate that subgoal scopes are grounded in the repository or investigation notes."""
    results = []
    for subgoal in subgoals:
        res = _check_single_subgoal_grounding(
            subgoal, workspace, ticket, investigation_notes, investigated_directories
        )
        if res["status"] == "rejected":
            raise ValueError(res["reason"])
        results.append(res)
    return results



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

    if state.get("investigation_incomplete", False):
        prompt_lines.append(
            "\nWARNING: Investigation hit its maximum turn cap before calling finish_investigation. "
            "Investigation notes may be incomplete. Plan cautiously and strictly ground all subgoals "
            "in the files and directories that were successfully discovered."
        )

    prompt_lines.extend([
        "",
        "Instructions:",
        "1. Break down the task into an ordered sequence of atomic Subgoals.",
        "2. For each Subgoal, specify:",
        "   - 'description': Clear, atomic, and objective description of the functional change (e.g. 'Create the About page component at src/app/about/page.tsx for Smash Arena').",
        "     CRITICAL: Do NOT include subjective meta-qualifiers, styling instructions, or cross-file comparison clauses in 'description' (e.g. do NOT say 'following dark theme', 'using Tailwind v4 styling', 'consistent with existing pages like contact and home', or 'adhering to glassmorphism conventions'). Architectural and styling conventions belong in Investigation Notes, not in the subgoal description, because the gatekeeper evaluates the diff strictly against every clause in the description.",
        "   - 'scope': Non-empty list of exact file paths to touch or create. No placeholder or empty scopes allowed.",
        "   - 'expects_tests': Boolean (true/false) indicating whether automated tests are expected to pass/run for this step.",
        "3. GROUNDING REQUIREMENTS (CRITICAL):",
        "   - If modifying, extending, or documenting existing code, every file path in 'scope' MUST be grounded strictly in the files discovered in 'Investigation Notes' or explicitly named in the 'Ticket Description'.",
        "   - When Investigation Notes identify candidate files containing relevant or undocumented functions, you MUST select only from those candidate files. Do NOT target documentation (e.g. README.md), configuration files, or non-source files for tickets modifying or documenting existing functions.",
        "   - Do NOT invent, hallucinate, or guess file paths.",
        "   - Do NOT assume any default project language or file extensions (e.g. do NOT assume Python 'src/main.py' if the repository is TypeScript, Go, Rust, or JavaScript). Use the actual language and paths discovered in Investigation Notes.",
        "   - If the ticket explicitly requests creating brand new files not previously existing, those new paths must be consistent with the directory structure established in Investigation Notes.",
        "4. 'expects_tests' CRITERIA (CRITICAL):",
        "   - Set 'expects_tests': false for subgoals that only add or update docstrings, comments, JSDoc/TypeDoc, documentation, type annotations, or code formatting.",
        "   - Set 'expects_tests': false if the ticket does not request writing automated tests and the repository has no existing test suite for the files in scope.",
        "   - Set 'expects_tests': true ONLY when automated tests exist or are being written/modified to verify functional code logic.",
        "5. Output format:",
        "   Return ONLY a valid JSON array of Subgoal objects. Do NOT include markdown commentary or explanations outside the JSON array.",
        "   Schema illustration:",
        '   [{"description": "Atomic change description", "scope": ["relative/path/to/target/file"], "expects_tests": false}]',
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
            _validate_plan_grounding(
                parsed_subgoals,
                workspace,
                ticket,
                investigation_notes,
                state.get("investigated_directories"),
            )
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

        grounding_checks = [
            {
                "subgoal": sg.description,
                "scope": list(sg.scope),
                **_check_single_subgoal_grounding(
                    sg, workspace, ticket, investigation_notes, state.get("investigated_directories")
                ),
            }
            for sg in subgoals
        ]
        subgoals_with_grounding = []
        for sg, gc in zip(subgoals, grounding_checks):
            sg_dict = sg.model_dump() if hasattr(sg, "model_dump") else dict(sg)
            sg_dict["directory_grounding"] = gc
            subgoals_with_grounding.append(sg_dict)

        traj_entry: Dict[str, Any] = {
            "node": "plan",
            "subgoals": subgoals_with_grounding,
            "grounding_checks": grounding_checks,
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

    fail_grounding_checks: List[Dict[str, Any]] = []
    if 'parsed_subgoals' in locals() and parsed_subgoals:
        for sg in parsed_subgoals:
            res = _check_single_subgoal_grounding(
                sg, workspace, ticket, investigation_notes, state.get("investigated_directories")
            )
            fail_grounding_checks.append({
                "subgoal": getattr(sg, "description", ""),
                "scope": list(getattr(sg, "scope", [])),
                **res,
            })
    if not fail_grounding_checks:
        fail_grounding_checks.append({"status": "rejected", "reason": f"Planning validation failed: {last_error}"})

    state["trajectory"].append({
        "node": "plan",
        "error_type": "validation_failure",
        "error": f"Planning validation failed: {last_error}",
        "subgoals": [],
        "grounding_checks": fail_grounding_checks,
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

    if workspace is not None and hasattr(workspace, "create_subgoal_worktree") and subgoal is not None:
        if not state.get("current_worktree_path"):
            subgoal_desc = getattr(subgoal, "description", "") or "subgoal"
            clean_desc = re.sub(r"[^a-zA-Z0-9_-]", "_", subgoal_desc)[:20].strip("_") or "subgoal"
            subgoal_id = f"{clean_desc}_{uuid.uuid4().hex[:6]}"
            base_ref = state.get("integration_branch") or "HEAD"
            if hasattr(workspace, "get_branch_commit") and state.get("integration_branch"):
                try:
                    state["subgoal_base_commit"] = workspace.get_branch_commit(state["integration_branch"])
                except Exception:
                    state["subgoal_base_commit"] = state.get("base_commit")
            elif hasattr(workspace, "base_commit"):
                state["subgoal_base_commit"] = workspace.base_commit
            wt_path = workspace.create_subgoal_worktree(subgoal_id, base_ref=base_ref)
            state["current_worktree_path"] = str(wt_path)
            state["current_worktree_branch"] = f"jev-subgoal-{subgoal_id}"
        elif hasattr(workspace, "worktree_dir") and state.get("current_worktree_path"):
            workspace.worktree_dir = Path(state["current_worktree_path"])

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
            norm_path = Path(path).as_posix().lstrip("./")
            norm_scope = {Path(s).as_posix().lstrip("./") for s in (subgoal.scope or [])}
            if norm_scope and norm_path not in norm_scope:
                return (
                    f"Error: Target path '{path}' is not within declared scope {subgoal.scope}. "
                    "You may only mutate files within the declared scope for this subgoal. "
                    "Subsequent subgoals will address other files."
                )

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

        prompt_lines = []
        if state.get("ticket"):
            prompt_lines.append(f"Ticket: {state['ticket']}")
        prompt_lines.extend([
            f"Current Subgoal: {subgoal.description}",
            f"Declared Scope: {json.dumps(subgoal.scope)}",
        ])
        if subgoal.expects_tests:
            prompt_lines.append("Note: This subgoal expects tests to verify its implementation.")
        else:
            prompt_lines.append("Note: This subgoal does not expect tests.")

        if state.get("investigation_notes"):
            prompt_lines.append(
                f"Investigation Notes (Project Architecture & Conventions):\n{state['investigation_notes']}"
            )

        if state.get("last_feedback"):
            prompt_lines.append(f"Previous attempt failed gate verification. Feedback:\n{state['last_feedback']}")

        existing_files_context = []
        worktree = getattr(workspace, "worktree_dir", getattr(workspace, "repo_dir", None))

        # Grounding: Extract Product Identity & Business Domain if root metadata is present
        if worktree:
            wt_path = Path(worktree)
            layout_candidates = [
                wt_path / "src" / "app" / "layout.tsx",
                wt_path / "src" / "app" / "layout.jsx",
                wt_path / "src" / "app" / "layout.js",
                wt_path / "app" / "layout.tsx",
                wt_path / "app" / "layout.jsx",
                wt_path / "app" / "layout.js",
                wt_path / "layout.tsx",
            ]
            extracted_title = None
            extracted_desc = None
            for cand in layout_candidates:
                if cand.exists() and cand.is_file():
                    try:
                        content = cand.read_text(encoding="utf-8")
                        title_match = re.search(r'title:\s*["\']([^"\']+)["\']', content)
                        desc_match = re.search(r'description:\s*["\']([^"\']+)["\']', content)
                        if title_match:
                            extracted_title = title_match.group(1).strip()
                        if desc_match:
                            extracted_desc = desc_match.group(1).strip()
                        if extracted_title or extracted_desc:
                            break
                    except Exception:
                        pass

            pkg_json = wt_path / "package.json"
            if (not extracted_title or not extracted_desc) and pkg_json.exists() and pkg_json.is_file():
                try:
                    pkg_data = json.loads(pkg_json.read_text(encoding="utf-8"))
                    if not extracted_title and "name" in pkg_data:
                        extracted_title = str(pkg_data["name"]).strip()
                    if not extracted_desc and "description" in pkg_data:
                        extracted_desc = str(pkg_data["description"]).strip()
                except Exception:
                    pass

            if extracted_title or extracted_desc:
                identity_lines = ["Product Identity & Business Domain:"]
                if extracted_title:
                    identity_lines.append(f"- Title: {extracted_title}")
                if extracted_desc:
                    identity_lines.append(f"- Description: {extracted_desc}")
                prompt_lines.append("\n".join(identity_lines))

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
            "1. Strictly adhere to the project architecture, dependencies, styling conventions, and existing component patterns documented in the Investigation Notes. Do not introduce uninstalled libraries or conflicting layout structures (e.g. do not create redundant headers or navbars if layout.tsx or a global component already provides them).\n"
            "2. Strictly adhere to the application's actual business domain, product identity, and branding documented in the Investigation Notes and repository. Never invent an alternate sport, brand, company name, or unrelated business model (e.g. do not substitute Padel or Tennis for Badminton).\n"
            "3. When creating or modifying UI components, align with the project's export and import conventions (e.g. if root layouts and surrounding components use default exports such as `export default function Component`, provide default exports so default imports resolve cleanly).\n"
            "4. Stage all necessary code changes using the `stage_file_mutation` tool.\n"
            "   Only mutate files within the declared scope.\n"
            "5. When all changes are staged and you are done, call the `submit_subgoal` tool to submit your work for gate verification."
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

                    norm_path = Path(actual_path).as_posix().lstrip("./")
                    norm_scope = {Path(s).as_posix().lstrip("./") for s in (subgoal.scope or [])}
                    if norm_scope and norm_path not in norm_scope:
                        result = (
                            f"Error: Target path '{actual_path}' is not within declared scope {subgoal.scope}. "
                            "You may only mutate files within the declared scope for this subgoal. "
                            "Subsequent subgoals will address other files."
                        )
                    elif actual_path and hasattr(workspace, "stage_file_mutation"):
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


def _is_active_worktree(state: State, workspace: Optional[Any]) -> bool:
    wt_path = state.get("current_worktree_path")
    if isinstance(wt_path, (str, Path)) and str(wt_path).strip():
        return True
    ws_path = getattr(workspace, "current_worktree_path", None) if workspace else None
    if isinstance(ws_path, (str, Path)) and str(ws_path).strip():
        return True
    if (
        workspace is not None
        and hasattr(workspace, "worktree_dir")
        and hasattr(workspace, "repo_dir")
        and isinstance(workspace.worktree_dir, Path)
        and isinstance(workspace.repo_dir, Path)
        and workspace.worktree_dir != workspace.repo_dir
    ):
        return True
    return False


def _get_active_worktree_info(state: State, workspace: Optional[Any]) -> tuple[Optional[str], Optional[str]]:
    wt_path = state.get("current_worktree_path")
    if not isinstance(wt_path, (str, Path)):
        wt_path = getattr(workspace, "current_worktree_path", None) if workspace else None
    if not isinstance(wt_path, (str, Path)):
        resolved_path = None
    else:
        resolved_path = str(wt_path)

    wt_branch = state.get("current_worktree_branch")
    if not isinstance(wt_branch, str):
        wt_branch = getattr(workspace, "current_worktree_branch", None) if workspace else None
    if not isinstance(wt_branch, str):
        resolved_branch = None
    else:
        resolved_branch = str(wt_branch)

    return resolved_path, resolved_branch


def _format_compile_feedback(
    compile_info: Dict[str, Any], fallback_detail: str = ""
) -> str:
    cmd = compile_info.get("command")
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    new_errors = compile_info.get("new_errors") or []

    if not new_errors:
        if cmd_str:
            return f"Compile command failed: {cmd_str}\n{fallback_detail or compile_info.get('detail', '')}".strip()
        return fallback_detail or compile_info.get("detail", "Compile check failed.")

    header_lines = []
    if cmd_str:
        header_lines.append(f"Command: {cmd_str}")
    header_lines.append("Errors:")
    header = "\n".join(header_lines)

    max_lines = 20
    max_total_chars = 3000

    selected_errors: List[str] = []

    for err in new_errors:
        err_str = str(err).strip()
        if not err_str:
            continue
        if len(selected_errors) >= max_lines:
            break

        remaining_if_stopped = len(new_errors) - (len(selected_errors) + 1)
        omission_line = f"\n...and {remaining_if_stopped} more errors omitted" if remaining_if_stopped > 0 else ""

        candidate_body = "\n".join(selected_errors + [err_str])
        candidate_total = f"{header}\n{candidate_body}{omission_line}"

        if len(candidate_total) > max_total_chars and selected_errors:
            break

        selected_errors.append(err_str)

    omitted = len(new_errors) - len(selected_errors)
    out_lines = [header] + selected_errors
    if omitted > 0:
        out_lines.append(f"...and {omitted} more errors omitted")

    return "\n".join(out_lines)


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
    subgoal_base_commit = state.get("subgoal_base_commit")
    if _accepts_param(workspace.run_mechanical_checks, "base_commit"):
        mech_result = workspace.run_mechanical_checks(subgoal, base_commit=subgoal_base_commit)
    else:
        mech_result = workspace.run_mechanical_checks(subgoal)

    tier0_telemetry = {
        "passed": mech_result.passed,
        "failed_check": mech_result.failed_check,
        "detail": mech_result.detail,
        "checks_run": getattr(mech_result, "checks_run", []),
        "checks": getattr(mech_result, "checks", {}),
    }
    if not tier0_telemetry["checks"]:
        failed = mech_result.failed_check
        tier0_telemetry["checks"] = {
            "build": {"ran": True, "passed": failed != "build", "detail": mech_result.detail if failed == "build" else ""},
            "compile": {"ran": failed not in ("build",), "passed": failed not in ("build", "compile", "no_compile_command", "env_not_ready") if failed else True, "detail": mech_result.detail if failed in ("compile", "no_compile_command", "env_not_ready") else ""},
            "tests": {"ran": failed not in ("build", "compile", "no_compile_command", "env_not_ready"), "passed": failed not in ("build", "compile", "no_compile_command", "env_not_ready", "tests", "no_tests_collected") if failed else True, "detail": mech_result.detail if failed in ("tests", "no_tests_collected") else ""},
            "scope": {"ran": failed not in ("build", "compile", "no_compile_command", "env_not_ready", "tests", "no_tests_collected"), "passed": failed != "scope", "detail": mech_result.detail if failed == "scope" else ""},
        }
        tier0_telemetry["checks_run"] = [k for k, v in tier0_telemetry["checks"].items() if v.get("ran")]

    tests_ran = False
    if "tests" in tier0_telemetry.get("checks_run", []):
        tests_ran = True
    elif tier0_telemetry.get("checks", {}).get("tests", {}).get("ran"):
        tests_ran = True

    if not tests_ran:
        test_runner_telemetry = {"ran": False}
    else:
        test_runner_telemetry = getattr(mech_result, "test_runner_outcome", None)
        if test_runner_telemetry is None and hasattr(workspace, "last_test_run"):
            test_runner_telemetry = workspace.last_test_run
        if test_runner_telemetry is None:
            test_runner_telemetry = {
                "ecosystem": None,
                "command": None,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "outcome": "PASSED" if mech_result.passed or mech_result.failed_check != "tests" else "FAILED",
            }

    compile_telemetry = getattr(mech_result, "compile_outcome", None)
    if compile_telemetry is None and hasattr(workspace, "last_compile_run"):
        compile_telemetry = workspace.last_compile_run
    if compile_telemetry is None and tier0_telemetry["checks"].get("compile", {}).get("ran"):
        compile_telemetry = {
            "ecosystem": None,
            "command": None,
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "output_tail": "",
            "new_errors": [],
            "base_errors": [],
            "outcome": "PASSED" if mech_result.passed or mech_result.failed_check not in ("compile", "no_compile_command", "env_not_ready") else "FAILED",
        }

    if not mech_result.passed:
        is_wt = _is_active_worktree(state, workspace)
        wt_path, wt_branch = _get_active_worktree_info(state, workspace)
        if is_wt and hasattr(workspace, "discard_subgoal_worktree"):
            workspace.discard_subgoal_worktree(
                wt_path,
                wt_branch,
            )
        elif hasattr(workspace, "rollback_subgoal"):
            workspace.rollback_subgoal()
        if hasattr(workspace, "repo_dir") and isinstance(workspace.repo_dir, Path):
            workspace.worktree_dir = workspace.repo_dir
        state["current_worktree_path"] = None
        state["current_worktree_branch"] = None

        if mech_result.failed_check in ("no_compile_command", "env_not_ready"):
            state["status"] = "escalated"
            state["gate_status"] = mech_result.failed_check
            state["last_feedback"] = mech_result.detail

            state["trajectory"].append({
                "node": "gate",
                "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
                "gate_status": mech_result.failed_check,
                "tier0_result": tier0_telemetry,
                "compile": compile_telemetry,
                "test_runner": test_runner_telemetry,
                "jev_request": None,
                "jev_verdict": None,
            })

            if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                gatekeeper.escalate_deadlock(
                    trajectory=state.get("trajectory", []),
                    triggering_tier="mechanical",
                    integration_branch=state.get("integration_branch"),
                )
            return state

        current_mech_strikes = state.get("mechanical_strike_count", 0) + 1
        state["mechanical_strike_count"] = current_mech_strikes
        state["gate_status"] = "mechanical_failure"
        if mech_result.failed_check == "compile":
            state["last_feedback"] = _format_compile_feedback(
                compile_telemetry or getattr(mech_result, "compile_outcome", None) or {},
                fallback_detail=mech_result.detail,
            )
        else:
            state["last_feedback"] = mech_result.detail

        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "mechanical_failure",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "jev_request": None,
            "jev_verdict": None,
        })

        if current_mech_strikes >= 3 and gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
            kwargs: Dict[str, Any] = {}
            if state.get("integration_branch"):
                kwargs["integration_branch"] = state.get("integration_branch")
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
                **kwargs,
            )
        return state

    # Tier 0 Passed -> Proceed to Tier 1 (Semantic check via Gatekeeper)
    if gatekeeper is None:
        raise ValueError("Gatekeeper must be provided for Tier 1 validation.")

    diff = workspace.get_staged_diff()
    inv_notes = state.get("investigation_notes")
    if _accepts_param(gatekeeper.validate_subgoal, "investigation_notes"):
        verdict = gatekeeper.validate_subgoal(
            subgoal, diff, mechanical_detail=mech_result.detail, investigation_notes=inv_notes
        )
    else:
        verdict = gatekeeper.validate_subgoal(
            subgoal, diff, mechanical_detail=mech_result.detail
        )

    jev_request_telemetry = {
        "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
        "diff_size": len(diff),
        "investigation_notes_included": bool(inv_notes),
    }
    jev_verdict_telemetry = {
        "valid": verdict.valid,
        "probability": getattr(verdict, "probability", 1.0),
        "reason": getattr(verdict, "reason", None),
    }

    if verdict.valid:
        wt_path = state.get("current_worktree_path")
        wt_branch = state.get("current_worktree_branch")
        is_worktree_active = _is_active_worktree(state, workspace)

        if wt_path and hasattr(workspace, "merge_subgoal_worktree"):
            target_branch = state.get("integration_branch")
            if not target_branch:
                state["gate_status"] = "merge_failed"
                state["status"] = "escalated"
                err_msg = "Worktree merge failed: integration_branch is missing from state"
                state["last_feedback"] = err_msg
                state["trajectory"].append({
                    "node": "gate",
                    "error_type": "missing_integration_branch",
                    "error": err_msg,
                    "gate_status": "merge_failed",
                })
                if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                    gatekeeper.escalate_deadlock(
                        trajectory=state.get("trajectory", []),
                        triggering_tier="merge",
                    )
                return state
            try:
                workspace.merge_subgoal_worktree(
                    wt_path,
                    wt_branch,
                    target_branch=target_branch,
                )
            except Exception as e:
                state["gate_status"] = "merge_failed"
                state["status"] = "escalated"
                state["last_feedback"] = f"Worktree merge failed: {e}"
                state["trajectory"].append({
                    "node": "gate",
                    "error_type": "merge_failure",
                    "error": str(e),
                    "gate_status": "merge_failed",
                })
                if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                    gatekeeper.escalate_deadlock(
                        trajectory=state.get("trajectory", []),
                        triggering_tier="merge",
                        integration_branch=target_branch,
                    )
                return state
        elif is_worktree_active:
            # An active worktree was created, but worktree state is missing from state or merge method missing!
            # Fail loudly and escalate instead of committing quietly on the worktree branch.
            active_wt_path, active_wt_branch = _get_active_worktree_info(state, workspace)
            if hasattr(workspace, "discard_subgoal_worktree"):
                workspace.discard_subgoal_worktree(active_wt_path, active_wt_branch)
            if hasattr(workspace, "repo_dir") and isinstance(workspace.repo_dir, Path):
                workspace.worktree_dir = workspace.repo_dir
            state["current_worktree_path"] = None
            state["current_worktree_branch"] = None
            state["gate_status"] = "merge_failed"
            state["status"] = "escalated"
            err_msg = "Worktree was created but worktree state was missing or invalid at gate time."
            state["last_feedback"] = err_msg
            state["trajectory"].append({
                "node": "gate",
                "error_type": "worktree_state_missing",
                "error": err_msg,
                "gate_status": "merge_failed",
            })
            if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                gatekeeper.escalate_deadlock(
                    trajectory=state.get("trajectory", []),
                    triggering_tier="worktree",
                    integration_branch=state.get("integration_branch"),
                )
            return state
        elif hasattr(workspace, "commit_subgoal"):
            workspace.commit_subgoal()

        state["current_worktree_path"] = None
        state["current_worktree_branch"] = None
        state["mechanical_strike_count"] = 0
        state["semantic_strike_count"] = 0
        state["gate_status"] = "passed"
        state["last_feedback"] = ""
        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "passed",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "jev_request": jev_request_telemetry,
            "jev_verdict": jev_verdict_telemetry,
        })
        return state
    else:
        is_wt = _is_active_worktree(state, workspace)
        wt_path, wt_branch = _get_active_worktree_info(state, workspace)
        if is_wt and hasattr(workspace, "discard_subgoal_worktree"):
            workspace.discard_subgoal_worktree(
                wt_path,
                wt_branch,
            )
        elif hasattr(workspace, "rollback_subgoal"):
            workspace.rollback_subgoal()
        if hasattr(workspace, "repo_dir") and isinstance(workspace.repo_dir, Path):
            workspace.worktree_dir = workspace.repo_dir
        state["current_worktree_path"] = None
        state["current_worktree_branch"] = None
        current_sem_strikes = state.get("semantic_strike_count", 0) + 1
        state["semantic_strike_count"] = current_sem_strikes
        state["gate_status"] = "semantic_failure"
        prob_str = f" (confidence: {verdict.probability:.2f})" if hasattr(verdict, "probability") and isinstance(verdict.probability, (int, float)) else ""
        state["last_feedback"] = (
            verdict.reason
            or f"Semantic validation rejected by Gatekeeper{prob_str}. "
               "Diff contradicted project conventions, exceeded declared scope, or failed to implement the required subgoal. "
               "Ensure changes match the project architecture, dependencies, and styling in Investigation Notes. "
               "Ensure changes strictly adhere to the application's actual domain, product identity, and branding documented in Investigation Notes, without introducing conflicting domain concepts, alternate sports/business models, or fabricated external entities."
        )

        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "semantic_failure",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "jev_request": jev_request_telemetry,
            "jev_verdict": jev_verdict_telemetry,
        })

        if current_sem_strikes >= 3 and hasattr(gatekeeper, "escalate_deadlock"):
            kwargs: Dict[str, Any] = {}
            if state.get("integration_branch"):
                kwargs["integration_branch"] = state.get("integration_branch")
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="semantic",
                **kwargs,
            )

        return state


def node_verify(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
) -> State:
    """State 4: VERIFICATION (Executes full test suite and final ticket verification on a dedicated verify worktree)."""
    if "trajectory" not in state or state["trajectory"] is None:
        state["trajectory"] = []

    integration_branch = state.get("integration_branch")
    base_commit = state.get("base_commit")
    ticket = state.get("ticket", "")
    inv_notes = state.get("investigation_notes")

    verify_wt_path = None
    compile_info = None
    test_runner_info = {"ran": False}
    final_diff = ""

    try:
        # 0. Create dedicated detached worktree off the integration branch
        if not integration_branch:
            raise ValueError("integration_branch is required for verification; direct verification on main is disallowed")
        if workspace is not None and hasattr(workspace, "create_verify_worktree"):
            verify_wt_path = workspace.create_verify_worktree(integration_branch)

        if workspace is not None:
            if hasattr(workspace, "get_cumulative_diff"):
                final_diff = workspace.get_cumulative_diff(base_ref=base_commit)
            elif hasattr(workspace, "get_staged_diff"):
                final_diff = workspace.get_staged_diff()

        if not final_diff and workspace is not None and hasattr(workspace, "get_staged_diff"):
            final_diff = workspace.get_staged_diff()

        # 1. Compile check against base_commit
        if workspace is not None and hasattr(workspace, "check_compile"):
            compile_info = workspace.check_compile(diff=final_diff, base_commit=base_commit)

        if compile_info is not None and compile_info.get("outcome") in ("FAILED", "NO_COMPILE_COMMAND", "ENV_NOT_READY"):
            state["gate_status"] = "verification_failed"
            state["status"] = "escalated" if compile_info.get("outcome") in ("NO_COMPILE_COMMAND", "ENV_NOT_READY") else "verification_failed"
            state["last_feedback"] = f"Final compile check failed: {compile_info.get('detail')}"
            verify_entry = {
                "node": "verify",
                "gate_status": "verification_failed",
                "status": state["status"],
                "compile": compile_info,
                "test_runner": {"ran": False},
                "jev_request": {
                    "ticket": ticket,
                    "diff_size": len(final_diff),
                    "investigation_notes_included": bool(inv_notes),
                },
                "jev_verdict": None,
            }
            state["trajectory"].append(verify_entry)
            if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                kwargs: Dict[str, Any] = {}
                if integration_branch:
                    kwargs["integration_branch"] = integration_branch
                gatekeeper.escalate_deadlock(
                    trajectory=state.get("trajectory", []),
                    triggering_tier="mechanical",
                    **kwargs,
                )
            return state

        # 2. Run unit tests
        test_output = ""
        tests_ran = False
        outcome = None
        if workspace is not None and hasattr(workspace, "run_tests"):
            outcome = workspace.run_tests()
            tests_ran = True
            test_output = getattr(outcome, "value", str(outcome))

        if outcome == TestOutcome.ENV_NOT_READY or test_output == "ENV_NOT_READY":
            test_runner_info = getattr(workspace, "last_test_run", None)
            err_detail = (test_runner_info or {}).get("stderr_tail") or "Test runner executable not found."
            state["gate_status"] = "verification_failed"
            state["status"] = "escalated"
            state["last_feedback"] = f"Final test check failed: Environment not ready: {err_detail}"
            verify_entry = {
                "node": "verify",
                "gate_status": "verification_failed",
                "status": "escalated",
                "compile": compile_info,
                "test_runner": test_runner_info,
                "jev_request": {
                    "ticket": ticket,
                    "diff_size": len(final_diff),
                    "investigation_notes_included": bool(inv_notes),
                },
                "jev_verdict": None,
            }
            state["trajectory"].append(verify_entry)
            if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
                kwargs: Dict[str, Any] = {}
                if integration_branch:
                    kwargs["integration_branch"] = integration_branch
                gatekeeper.escalate_deadlock(
                    trajectory=state.get("trajectory", []),
                    triggering_tier="mechanical",
                    **kwargs,
                )
            return state

        # Determine if any subgoals expected automated tests
        any_expects_tests = False
        for step in state.get("trajectory", []):
            subgoals = step.get("subgoals", [])
            if isinstance(subgoals, list):
                for sg in subgoals:
                    if isinstance(sg, dict) and sg.get("expects_tests", True):
                        any_expects_tests = True
                    elif hasattr(sg, "expects_tests") and getattr(sg, "expects_tests", True):
                        any_expects_tests = True

        if not any_expects_tests and test_output == "NO_TESTS_COLLECTED":
            test_output = "NO_TESTS_COLLECTED (Untested pass: ticket does not require automated tests)"
        elif test_output == "NO_TEST_FRAMEWORK":
            test_output = "NO_TEST_FRAMEWORK (Untested pass: no test framework present in workspace)"

        if not tests_ran:
            test_runner_info = {"ran": False}
        else:
            test_runner_info = getattr(workspace, "last_test_run", None)
            if test_runner_info is None:
                test_runner_info = {
                    "ecosystem": None,
                    "command": None,
                    "exit_code": None,
                    "stdout_tail": "",
                    "stderr_tail": "",
                    "output_tail": "",
                    "outcome": getattr(outcome, "value", str(outcome)) if 'outcome' in locals() else "UNKNOWN",
                }

        verdict = None
        if gatekeeper is not None and hasattr(gatekeeper, "verify_ticket"):
            if _accepts_param(gatekeeper.verify_ticket, "investigation_notes"):
                verdict = gatekeeper.verify_ticket(ticket, final_diff, test_output, investigation_notes=inv_notes)
            else:
                verdict = gatekeeper.verify_ticket(ticket, final_diff, test_output)

            if verdict.valid:
                # Discard verify worktree safely first
                if verify_wt_path and hasattr(workspace, "discard_verify_worktree"):
                    workspace.discard_verify_worktree(verify_wt_path)
                    verify_wt_path = None

                # Fast-forward main to integration branch
                if workspace is not None and hasattr(workspace, "fast_forward_main"):
                    workspace.fast_forward_main(integration_branch, base_commit=base_commit)

                # Delete integration branch on successful landing
                if workspace is not None and hasattr(workspace, "delete_integration_branch"):
                    workspace.delete_integration_branch(integration_branch)

                state["gate_status"] = "verified"
                state["status"] = "completed"
                state["last_feedback"] = ""
            else:
                state["gate_status"] = "verification_failed"
                state["status"] = "verification_failed"
                prob_str = f" (confidence: {verdict.probability:.2f})" if hasattr(verdict, "probability") and isinstance(verdict.probability, (int, float)) else ""
                state["last_feedback"] = verdict.reason or f"Verification rejected by Gatekeeper{prob_str}."
        else:
            if verify_wt_path and hasattr(workspace, "discard_verify_worktree"):
                workspace.discard_verify_worktree(verify_wt_path)
                verify_wt_path = None
            if workspace is not None and hasattr(workspace, "fast_forward_main"):
                workspace.fast_forward_main(integration_branch, base_commit=base_commit)
            if workspace is not None and hasattr(workspace, "delete_integration_branch"):
                workspace.delete_integration_branch(integration_branch)
            state["gate_status"] = "verified"
            state["status"] = "completed"

        verify_entry: Dict[str, Any] = {
            "node": "verify",
            "gate_status": state.get("gate_status"),
            "status": state.get("status"),
            "compile": compile_info,
            "test_runner": test_runner_info,
            "jev_request": {
                "ticket": ticket,
                "diff_size": len(final_diff),
                "investigation_notes_included": bool(inv_notes),
            },
            "jev_verdict": {
                "valid": verdict.valid,
                "probability": getattr(verdict, "probability", 1.0),
                "reason": getattr(verdict, "reason", None),
            } if verdict is not None else None,
        }
        if verdict is not None:
            if getattr(verdict, "probability", None) is not None:
                verify_entry["probability"] = verdict.probability
            if getattr(verdict, "reason", None):
                verify_entry["reason"] = verdict.reason

        state["trajectory"].append(verify_entry)

        if state.get("gate_status") == "verification_failed" and gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
            kwargs: Dict[str, Any] = {}
            if integration_branch:
                kwargs["integration_branch"] = integration_branch
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="verification",
                **kwargs,
            )
        return state

    except (MainDivergedError, TrackedModificationsError) as e:
        status_key = "main_diverged" if isinstance(e, MainDivergedError) else "tracked_modifications"
        state["gate_status"] = status_key
        state["status"] = "escalated"
        state["last_feedback"] = str(e)
        state["trajectory"].append({
            "node": "verify",
            "error_type": status_key,
            "error": str(e),
            "gate_status": status_key,
            "status": "escalated",
            "compile": compile_info,
            "test_runner": test_runner_info,
            "jev_request": {
                "ticket": ticket,
                "diff_size": len(final_diff),
                "investigation_notes_included": bool(inv_notes),
            },
            "jev_verdict": None,
        })
        if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
            kwargs = {}
            if integration_branch:
                kwargs["integration_branch"] = integration_branch
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="verification",
                **kwargs,
            )
        return state

    except Exception as e:
        state["gate_status"] = "verification_failed"
        state["status"] = "escalated"
        state["last_feedback"] = f"Verification error: {str(e)}"
        state["trajectory"].append({
            "node": "verify",
            "error_type": "verification_exception",
            "error": f"Verification error: {str(e)}",
            "gate_status": "verification_failed",
            "status": "escalated",
            "compile": compile_info if 'compile_info' in locals() else getattr(workspace, "last_compile_run", None),
            "test_runner": test_runner_info,
            "jev_request": {
                "ticket": ticket,
                "diff_size": len(final_diff),
                "investigation_notes_included": bool(inv_notes),
            },
            "jev_verdict": None,
        })
        if gatekeeper is not None and hasattr(gatekeeper, "escalate_deadlock"):
            kwargs = {}
            if integration_branch:
                kwargs["integration_branch"] = integration_branch
            gatekeeper.escalate_deadlock(
                trajectory=state.get("trajectory", []),
                triggering_tier="verification",
                **kwargs,
            )
        return state

    finally:
        if verify_wt_path and workspace is not None and hasattr(workspace, "discard_verify_worktree"):
            workspace.discard_verify_worktree(verify_wt_path)



def node_escalate(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
) -> State:
    """Escalation termination node for HITL review."""
    state["status"] = "escalated"
    if workspace is not None:
        is_wt = _is_active_worktree(state, workspace)
        wt_path, wt_branch = _get_active_worktree_info(state, workspace)
        if is_wt and hasattr(workspace, "discard_subgoal_worktree"):
            workspace.discard_subgoal_worktree(wt_path, wt_branch)
        if hasattr(workspace, "repo_dir") and isinstance(workspace.repo_dir, Path):
            workspace.worktree_dir = workspace.repo_dir
    state["current_worktree_path"] = None
    state["current_worktree_branch"] = None
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
        or state.get("gate_status") == "merge_failed"
        or state.get("status") == "escalated"
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
        return node_escalate(state, workspace=self.workspace, gatekeeper=self.gatekeeper)

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
        base_commit = None
        if self.workspace is not None:
            if hasattr(self.workspace, "base_commit") and self.workspace.base_commit:
                base_commit = self.workspace.base_commit
            elif hasattr(self.workspace, "_get_head_commit"):
                base_commit = self.workspace._get_head_commit()

        integration_branch = f"jev-ticket-{thread_id}"
        if self.workspace is not None and hasattr(self.workspace, "create_integration_branch"):
            self.workspace.create_integration_branch(integration_branch, base_commit=base_commit)

        initial_state: State = {
            "ticket": ticket,
            "thread_id": thread_id,
            "base_commit": base_commit,
            "integration_branch": integration_branch,
            "subgoal_base_commit": base_commit,
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
