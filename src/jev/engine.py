import ast
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
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
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jev.dependency_delta import compute_dependency_delta
from jev.advisory import AdvisoryErrorCode, AdvisoryRequest, AdvisoryResult, RepairBrief
from jev.evidence import evidence_level
from jev.ledger import NullRecorder, Recorder
from jev.retrieval import AstWorkspaceRetriever, RetrievalLimits
from jev.replanning import replan_remaining_queue
from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)

from jev.models import (
    CompileOutcome,
    JevFeedback,
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    TestPolicy,
    ValidationVerdict,
)
from jev.workspace import (
    ALL_SOURCE_EXTENSIONS,
    ECOSYSTEM_COMPILE_HANDLERS,
    MAX_READ_CHARS,
    MainDivergedError,
    TrackedModificationsError,
    Workspace,
)

MAX_IMPLEMENT_FILE_CHARS = 80_000
MAX_IMPLEMENT_READ_CHARS = 20_000
MAX_IMPLEMENT_READ_CALLS = 12
MAX_IMPLEMENT_RETRIEVAL_FILES = 100
MAX_IMPLEMENT_RETRIEVAL_ITEMS = 200
SELF_CHECK_TIMEOUT_SECONDS = 5

def is_secret_path(p: Union[str, Path]) -> bool:
    path_obj = Path(p)
    name = path_obj.name.lower()
    parts = [part.lower() for part in path_obj.parts]
    if any(part in (".aws", ".kube", ".ssh") for part in parts):
        return True
    if name.startswith(".env") or name in ("credentials.json", "secrets.json", "id_rsa", "id_dsa", "id_ed25519"):
        return True
    if name.endswith((".pem", ".key", ".pfx", ".p12", ".pkcs12")):
        return True
    return False

MAX_SELF_CHECK_OUTPUT_CHARS = 4_000


def _cap_read_text(text: str) -> str:
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n[truncated: showing first {MAX_READ_CHARS} of {len(text)} characters]"
    return text


def _cap_self_check_output(text: str) -> str:
    if len(text) > MAX_SELF_CHECK_OUTPUT_CHARS:
        return text[:MAX_SELF_CHECK_OUTPUT_CHARS] + (
            f"\n[truncated: showing first {MAX_SELF_CHECK_OUTPUT_CHARS} of {len(text)} characters]"
        )
    return text

logger = logging.getLogger(__name__)


def _role_metadata(role: str, component: Any, *, provider: Optional[str] = None,
                   base_url_origin: Optional[str] = None, capabilities: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return non-secret provenance for a role without changing its authority."""
    model = None
    if component is not None:
        candidate_model = getattr(component, "model", None) or getattr(component, "model_name", None)
        model = candidate_model if isinstance(candidate_model, str) else None
        if not model and hasattr(component, "provider"):
            provider_obj = component.provider
            candidate_model = getattr(provider_obj, "model", None)
            model = candidate_model if isinstance(candidate_model, str) else None
            provider = provider or provider_obj.__class__.__name__
        provider = provider if isinstance(provider, str) else component.__class__.__name__
    if not isinstance(provider, str):
        provider = None
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "base_url_origin": base_url_origin,
        "capabilities": sorted(set(capabilities or [])),
        "configured": component is not None,
    }


def build_role_provenance(worker: Any, judge: Any, advisory: Any) -> Dict[str, Dict[str, Any]]:
    """Describe selected roles for state, trajectory, and ledger auditability."""
    advisory_provider = getattr(getattr(advisory, "provider", None), "__class__", None)
    provider_obj = getattr(advisory, "provider", None)
    advisory_url = getattr(provider_obj, "base_url", None)
    if advisory_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(str(advisory_url))
        advisory_url = f"{parsed.scheme}://{parsed.hostname}" if parsed.scheme and parsed.hostname else None
        try:
            port = parsed.port
        except ValueError:
            port = None
        if advisory_url and port:
            advisory_url += f":{port}"
    judge_metadata = _role_metadata("judge", judge, provider="JevGatekeeper", capabilities=["validate", "gate"])
    if judge is not None:
        judge_model = getattr(judge, "model", None)
        judge_metadata["model"] = judge_model if isinstance(judge_model, str) else "jev-latest"
    return {
        "worker": _role_metadata("worker", worker, capabilities=["investigate", "plan", "implement"]),
        "judge": judge_metadata,
        "advisory": _role_metadata(
            "advisory", advisory,
            provider=advisory_provider.__name__ if advisory_provider else None,
            base_url_origin=advisory_url,
            capabilities=["repair_guidance"] if advisory is not None else [],
        ),
    }


STATE_CUSTOM_TYPES = [
    Subgoal,
    MechanicalCheckResult,
    ValidationVerdict,
    TestOutcome,
    CompileOutcome,
    TestPolicy,
]


class SafeRecorder(NullRecorder):
    """Fail-safe proxy wrapper around a Recorder.

    Recording must never change a run's outcome. Wrap every ledger call.
    On any error, log one warning per run ("ledger disabled for this run: <error>") and continue.
    """

    def __init__(self, target: Any):
        self.target = target
        self.disabled = False

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.disabled or self.target is None:
            return None
        try:
            fn = getattr(self.target, name, None)
            if fn:
                return fn(*args, **kwargs)
        except Exception as exc:
            self.disabled = True
            msg = f"ledger disabled for this run: {exc}"
            print(f"WARNING: {msg}", file=sys.stderr, flush=True)
            return None

    def record_run_start(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_run_start", *args, **kwargs)

    def record_run_end(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_run_end", *args, **kwargs)

    def record_subgoals(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_subgoals", *args, **kwargs)

    def record_subgoal_committed(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_subgoal_committed", *args, **kwargs)

    def record_subgoal_status(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_subgoal_status", *args, **kwargs)

    def record_attempt(self, *args: Any, **kwargs: Any) -> None:
        self._call("record_attempt", *args, **kwargs)

    def has_run(self, *args: Any, **kwargs: Any) -> bool:
        res = self._call("has_run", *args, **kwargs)
        return bool(res)

    def get_run_evidence_level(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return self._call("get_run_evidence_level", *args, **kwargs)

    def get_run_dependency_delta(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return self._call("get_run_dependency_delta", *args, **kwargs)

    def get_run_provenance(self, *args: Any, **kwargs: Any) -> Optional[str]:
        return self._call("get_run_provenance", *args, **kwargs)


def _get_engine_commit() -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return None


def _normalize_compile_errors(compile_info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not compile_info or not isinstance(compile_info, dict):
        return []
    new_errors = compile_info.get("new_errors") or []
    if not new_errors:
        return []
    normalized: List[Dict[str, Any]] = []
    try:
        diag_objs = Workspace.parse_diagnostics("\n".join(str(e) for e in new_errors))
        for d in diag_objs:
            normalized.append({"file": d.file, "code": d.code, "message": d.message})
    except Exception:
        pass
    if not normalized:
        for err in new_errors:
            normalized.append({"file": "", "code": "", "message": str(err)})
    return normalized


def _extract_diff_excerpt(diff: Any, max_lines: int = 40, max_chars: int = 3000) -> str:
    if not isinstance(diff, str) or not diff:
        return ""
    lines = diff.splitlines(keepends=True)
    excerpt_lines = lines[:max_lines]
    excerpt = "".join(excerpt_lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars]
    return excerpt


def _extract_declarations_and_head(content: str, max_lines: int = 40) -> str:
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(f"{i + 1}: {lines[i]}" for i in range(len(lines)))

    decl_pattern = re.compile(
        r'^\s*(?:(?:export|public|pub|private|protected|static|final|abstract|async|default)\s+)*'
        r'(?:export|module\.exports|class|def|func|fn|function|pub|public|interface|type|const|struct)\b'
    )

    decl_indices: List[int] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*", "#")):
            continue
        if decl_pattern.match(line):
            decl_indices.append(idx)
            if len(decl_indices) >= max_lines:
                break

    selected_indices = list(decl_indices)
    decl_set = set(decl_indices)

    # then fill with the head
    for idx in range(len(lines)):
        if len(selected_indices) >= max_lines:
            break
        if idx not in decl_set:
            selected_indices.append(idx)

    return "\n".join(f"{idx + 1}: {lines[idx]}" for idx in selected_indices)


def _build_source_hints(
    error_text: str,
    candidate_root: Optional[Path],
    *,
    max_files: int = 3,
    max_lines: int = 40,
    max_chars: int = 6000,
) -> List[Dict[str, Any]]:
    if not error_text or candidate_root is None:
        return []

    try:
        cand_root = Path(candidate_root).resolve()
        if not cand_root.is_dir():
            return []
    except Exception:
        return []

    probe_extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java", ".kt"]
    hints_by_file: Dict[str, Dict[str, Any]] = {}

    def _resolve_candidate(p_str: str, base_dir: Path) -> Optional[Path]:
        p = Path(str(p_str).replace("\\", "/"))
        if p.is_absolute():
            try:
                res = p.resolve()
                res.relative_to(cand_root)
                if res.is_file() and not is_secret_path(res):
                    return res
            except (ValueError, OSError):
                return None
        else:
            try:
                norm = str(p_str).replace("\\", "/").lstrip("./")
                res = (base_dir / norm).resolve()
                res.relative_to(cand_root)
                if res.is_file() and not is_secret_path(res):
                    return res
            except (ValueError, OSError):
                return None
        return None

    # 1. Location hints:
    # a. File "x", line N
    # b. file(line,col) or file(line)
    # c. file:line[:col]
    location_patterns = [
        re.compile(r'File\s+["\']([^"\']+)["\'],\s+line\s+(\d+)'),
        re.compile(r'(?:^|[\s"\'\(])([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\(([0-9]+)(?:,\s*(\d+))?\)'),
        re.compile(r'(?:^|[\s"\'\(])([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+):(\d+)(?::(\d+))?'),
    ]

    for pat in location_patterns:
        for m in pat.finditer(error_text):
            if len(hints_by_file) >= max_files:
                break
            raw_file = m.group(1).strip()
            line_str = m.group(2).strip()
            try:
                line_num = int(line_str)
            except ValueError:
                continue

            resolved = _resolve_candidate(raw_file, cand_root)
            if resolved is None:
                continue

            rel_posix = resolved.relative_to(cand_root).as_posix()
            if rel_posix in hints_by_file:
                continue

            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                start_line = max(1, line_num - 10)
                end_line = min(len(lines), line_num + 10)
                if (end_line - start_line + 1) > max_lines:
                    end_line = start_line + max_lines - 1
                snippet_lines = [
                    f"{ln}: {lines[ln - 1]}"
                    for ln in range(start_line, end_line + 1)
                ]
                snippet = "\n".join(snippet_lines)
            except Exception:
                snippet = ""

            hints_by_file[rel_posix] = {
                "file": rel_posix,
                "line": line_num,
                "type": "location",
                "snippet": snippet,
                "confidence": "hint, may be incomplete",
            }

    # 2. Referenced-file hints
    if len(hints_by_file) < max_files:
        search_dirs: List[Path] = []
        for h_file in list(hints_by_file.keys()):
            h_dir = (cand_root / Path(h_file).parent).resolve()
            if h_dir not in search_dirs:
                search_dirs.append(h_dir)
        if cand_root not in search_dirs:
            search_dirs.append(cand_root)

        raw_tokens = re.findall(r'[^\s"\'\(\)<>\[\]`:]+', error_text)
        for token in raw_tokens:
            if len(hints_by_file) >= max_files:
                break
            cleaned = token.strip(",;()\"'").rstrip(".")
            if not cleaned or cleaned.startswith("http://") or cleaned.startswith("https://"):
                continue
            if not ("/" in cleaned or "\\" in cleaned or any(cleaned.endswith(ext) for ext in probe_extensions)):
                continue

            resolved: Optional[Path] = None
            for s_dir in search_dirs:
                cand = _resolve_candidate(cleaned, s_dir)
                if cand is not None:
                    resolved = cand
                    break
                for ext in probe_extensions:
                    cand = _resolve_candidate(f"{cleaned}{ext}", s_dir)
                    if cand is not None:
                        resolved = cand
                        break
                    cand = _resolve_candidate(f"{cleaned}/index{ext}", s_dir)
                    if cand is not None:
                        resolved = cand
                        break
                if resolved is not None:
                    break

            if resolved is None:
                continue

            rel_posix = resolved.relative_to(cand_root).as_posix()
            if rel_posix in hints_by_file:
                continue

            if resolved.suffix.lower() == ".py":
                try:
                    retriever = AstWorkspaceRetriever(cand_root)
                    res = retriever.retrieve(path=rel_posix)
                    struct_parts = []
                    if res.definitions:
                        struct_parts.append("Definitions: " + ", ".join(d.name for d in res.definitions[:10]))
                    if res.symbols:
                        struct_parts.append("Symbols: " + ", ".join(f"{s.kind} {s.name}" for s in res.symbols[:10]))
                    if res.imports:
                        struct_parts.append("Imports: " + ", ".join(i.name for i in res.imports[:10]))
                    snippet = "\n".join(struct_parts) if struct_parts else ""
                    if not snippet:
                        content = resolved.read_text(encoding="utf-8", errors="replace")
                        snippet = _extract_declarations_and_head(content, max_lines=max_lines)
                except Exception:
                    content = resolved.read_text(encoding="utf-8", errors="replace")
                    snippet = _extract_declarations_and_head(content, max_lines=max_lines)
            else:
                content = resolved.read_text(encoding="utf-8", errors="replace")
                snippet = _extract_declarations_and_head(content, max_lines=max_lines)

            hints_by_file[rel_posix] = {
                "file": rel_posix,
                "type": "referenced_file",
                "snippet": snippet,
                "confidence": "hint, may be incomplete",
            }

    source_hints: List[Dict[str, Any]] = []
    total_chars = 0
    for h in list(hints_by_file.values())[:max_files]:
        h_copy = dict(h)
        snip = h_copy.get("snippet", "")
        rem = max_chars - total_chars
        if rem <= 0:
            break
        if len(snip) > rem:
            h_copy["snippet"] = snip[:rem]
        total_chars += len(h_copy["snippet"])
        source_hints.append(h_copy)

    return source_hints


def _attempt_evidence(
    state: State,
    *,
    failure_class: str,
    failed_check: Optional[str],
    diff: str,
    detail: str = "",
    compile_info: Optional[Dict[str, Any]] = None,
    test_info: Optional[Dict[str, Any]] = None,
    subgoal: Optional[Subgoal] = None,
    tests_ran: bool = False,
    workspace: Optional[Any] = None,
    worktree_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Build a bounded, deterministic repair packet for a failed attempt."""
    compile_info = compile_info if isinstance(compile_info, dict) else {}
    test_info = test_info if isinstance(test_info, dict) else {}
    touched: List[str] = []
    try:
        touched = sorted(Workspace._extract_files_from_diff(diff)) if diff else []
    except Exception:
        touched = []
    previous = []
    for attempt in (state.get("failed_attempts") or [])[-2:]:
        previous.append({
            "hash": attempt.get("diff_sha", ""),
            "excerpt": _extract_diff_excerpt(attempt.get("diff_excerpt", ""), max_chars=1200),
        })
    new_errors = compile_info.get("new_errors") or []
    baseline_errors = compile_info.get("base_errors") or []
    output = test_info.get("output_tail") or compile_info.get("output_tail") or detail
    known_facts = [
        f"failure class is {failure_class}",
        f"declared scope: {list(subgoal.scope) if subgoal else []}",
        f"touched files: {touched}",
        f"tests ran: {tests_ran}",
    ]
    unknowns = ["root cause is not established by deterministic checks"]
    if not new_errors and not baseline_errors:
        unknowns.append("no compiler baseline/new-error comparison is available")

    # Build source hints from error outputs
    candidate_root: Optional[Path] = None
    if worktree_dir:
        candidate_root = Path(worktree_dir)
    elif workspace and getattr(workspace, "worktree_dir", None):
        candidate_root = Path(workspace.worktree_dir)
    elif state.get("current_worktree_path"):
        candidate_root = Path(state["current_worktree_path"])
    elif workspace and getattr(workspace, "repo_dir", None):
        candidate_root = Path(workspace.repo_dir)

    err_tokens = [str(output or ""), str(detail or "")]
    if compile_info:
        for ne in compile_info.get("new_errors") or []:
            err_tokens.append(str(ne))
        if compile_info.get("output_tail"):
            err_tokens.append(str(compile_info["output_tail"]))
    if test_info and test_info.get("output_tail"):
        err_tokens.append(str(test_info["output_tail"]))
    combined_err = "\n".join(err_tokens)

    source_hints = _build_source_hints(combined_err, candidate_root)
    if source_hints:
        hint_file = source_hints[0]["file"]
        required = [f"read {hint_file} before retrying"]
    else:
        required = ["inspect the affected files and relevant tests before retrying"]

    constraints = ["do not repeat a prior diff hash", "preserve declared write scope", "pass Tier 0 before semantic validation"]
    selected_trajectory = []
    selected_read_evidence = []
    for entry in reversed(state.get("trajectory") or []):
        if entry.get("node") not in {"implement", "gate"}:
            continue
        selected_trajectory.append({
            "node": entry.get("node"),
            "gate_status": entry.get("gate_status"),
        })
        for call in entry.get("tool_calls", []) or []:
            if isinstance(call, dict) and call.get("name") in {"read_file", "grep", "list_dir"}:
                selected_read_evidence.append({
                    "name": call.get("name"),
                    "args": str(call.get("args", ""))[:1000],
                })
        if len(selected_trajectory) >= 3:
            break
    return {
        "failure_class": failure_class,
        "failed_check": failed_check,
        "files": touched,
        "baseline_errors": baseline_errors[:20],
        "new_errors": new_errors[:20],
        "output_excerpt": _cap_self_check_output(str(output or "")),
        "known_facts": known_facts,
        "unknowns": unknowns,
        "required_inspection": required,
        "source_hints": source_hints,
        "prior_attempts": previous,
        "retry_constraints": constraints,
        "selected_trajectory": selected_trajectory[:3],
        "selected_read_evidence": selected_read_evidence[-6:],
    }


def _feedback_packet(evidence: Dict[str, Any]) -> str:
    """Render structured metadata compactly; diagnostics remain in the packet."""
    compact = {key: value for key, value in evidence.items() if key != "output_excerpt"}
    return json.dumps(compact, sort_keys=True)


def _format_repair_brief(brief: RepairBrief) -> str:
    """Render bounded advisory output as retry guidance, never as a verdict."""
    actions = "\n".join(f"- {action}" for action in brief.actions)
    return f"Advisory repair brief: {brief.summary}\nCause: {brief.cause}\nActions:\n{actions}"


def _format_advisory_failure(result: Any) -> str:
    """Render advisory failures with a stable, human-readable error code."""
    code = getattr(getattr(result, "error_code", None), "value", None) or "unknown_error"
    message = str(getattr(result, "error_message", None) or "advisory did not return a repair brief").strip()
    return (
        f"Advisory unavailable ({code}): {message}. "
        f"Advisory model is not available, error code = {code}. "
        "Continuing without advisory diagnosis."
    )


def _invoke_advisory(advisory: Optional[Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Call an optional advisory and return bounded telemetry without raising."""
    if advisory is None:
        return {"status": "not_configured", "result": None}
    if getattr(advisory, "enabled", True) is False:
        return {"status": "disabled", "result": None}

    packet = {key: value for key, value in evidence.items() if key != "output_excerpt"}
    packet["output_excerpt"] = _cap_self_check_output(str(evidence.get("output_excerpt", "")))
    try:
        serialized_packet = json.dumps(packet, sort_keys=True)
        request = AdvisoryRequest(
            prompt=serialized_packet[:19_500],
            system_prompt="Provide non-authoritative retry guidance. Do not decide gate validity.",
            json_mode=True,
        )
        if hasattr(advisory, "request_repair_brief"):
            result = advisory.request_repair_brief(
                request.prompt, system_prompt=request.system_prompt, json_mode=True
            )
        elif hasattr(advisory, "advise"):
            result = advisory.advise(request)
        else:
            result = AdvisoryResult(
                ok=False,
                error_code=AdvisoryErrorCode.CONFIGURATION,
                error_message="advisory does not implement the advisory contract",
            )
    except Exception as exc:
        result = AdvisoryResult(
            ok=False,
            error_code=AdvisoryErrorCode.UNKNOWN,
            error_message=str(exc)[:500],
        )

    if not isinstance(result, AdvisoryResult):
        result = AdvisoryResult(
            ok=False,
            error_code=AdvisoryErrorCode.MALFORMED_RESPONSE,
            error_message="advisory returned an invalid result",
        )
    if result.ok and result.brief is not None:
        return {
            "status": "success",
            "result": result.model_dump(mode="json"),
            "feedback": _format_repair_brief(result.brief),
        }
    return {
        "status": "error",
        "result": result.model_dump(mode="json"),
        "feedback": _format_advisory_failure(result),
    }


def _apply_advisory(
    advisory: Optional[Any], evidence: Dict[str, Any], state: State
) -> Dict[str, Any]:
    telemetry = _invoke_advisory(advisory, evidence)
    evidence["advisory"] = telemetry
    feedback = telemetry.get("feedback")
    if feedback:
        state["last_feedback"] = f"{feedback}\n\n{state.get('last_feedback') or ''}".strip()
    return telemetry


def _feedback_items(value: Any) -> List[str]:
    """Normalize question/diagnostic-like values without rejecting legacy data."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    items: List[str] = []
    for item in values:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("question") or item.get("message") or item.get("text") or item)
        else:
            text = str(item)
        if text and text not in items:
            items.append(text)
    return items


def normalize_jev_feedback(response: Any) -> JevFeedback:
    """Convert structured, JSON-like, and legacy Jev feedback to one shape.

    Parsing is deliberately best-effort. A malformed or legacy string is kept
    as a diagnostic rather than being allowed to alter the gate decision.
    """
    if isinstance(response, JevFeedback):
        return response.model_copy(deep=True)
    if isinstance(response, ValidationVerdict):
        diagnostics = _feedback_items(response.reason)
        if response.kind:
            diagnostics.append(f"kind: {response.kind}")
        return JevFeedback(
            verdict=response.valid,
            confidence=response.probability,
            diagnostics=diagnostics,
            questions=list((response.extra_answers or {}).keys()),
        )

    data: Any = response
    if isinstance(response, str):
        text = response.strip()
        try:
            data = json.loads(text) if text else {}
        except (TypeError, ValueError):
            return JevFeedback(diagnostics=_feedback_items(text))
    if not isinstance(data, dict):
        return JevFeedback(diagnostics=_feedback_items(data))

    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    verdict = data.get("verdict", data.get("valid"))
    valid_answer = answers.get("valid")
    if verdict is None and isinstance(valid_answer, dict):
        verdict = valid_answer.get("value")
        if not isinstance(verdict, bool) and isinstance(valid_answer.get("noul"), (int, float)):
            verdict = float(valid_answer["noul"]) >= 0.5
    if not isinstance(verdict, bool):
        verdict = None
    confidence = data.get("confidence", data.get("probability"))
    if confidence is None and isinstance(valid_answer, dict):
        confidence = valid_answer.get("probability", valid_answer.get("noul"))
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = None
    diagnostics = _feedback_items(data.get("diagnostics", data.get("reason")))
    diagnostics.extend(_feedback_items(data.get("issues")))
    if not diagnostics and isinstance(valid_answer, dict):
        diagnostics.extend(_feedback_items(valid_answer.get("reason")))
    return JevFeedback(
        verdict=verdict,
        confidence=float(confidence) if confidence is not None else None,
        diagnostics=diagnostics,
        questions=_feedback_items(data.get("questions")),
        unknowns=_feedback_items(data.get("unknowns", data.get("uncertainties"))),
    )


def _record_failed_attempt_in_state(
    state: State,
    tier: str,
    outcome: str,
    failed_check: Optional[str],
    diff: Any,
    compile_info: Optional[Dict[str, Any]] = None,
    failure_evidence: Optional[Dict[str, Any]] = None,
    retry_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if "failed_attempts" not in state or state["failed_attempts"] is None:
        state["failed_attempts"] = []

    prev_attempts = state["failed_attempts"]
    attempt_no = (prev_attempts[-1].get("attempt_no", 0) + 1) if prev_attempts else 1
    if isinstance(diff, str):
        diff_bytes = diff.encode("utf-8")
    elif isinstance(diff, bytes):
        diff_bytes = diff
    else:
        diff_bytes = b""
    diff_sha = hashlib.sha256(diff_bytes).hexdigest()
    diff_excerpt = _extract_diff_excerpt(diff)

    norm_errors = _normalize_compile_errors(compile_info)
    norm_error_keys: List[str] = []
    for err in norm_errors:
        key = err.get("code") or err.get("message") or ""
        if err.get("file"):
            key = f"{err['file']}:{key}"
        norm_error_keys.append(key)

    entry = {
        "attempt_no": attempt_no,
        "tier": tier,
        "failed_check": failed_check,
        "outcome": outcome,
        "normalized_error_keys": norm_error_keys,
        "diff_sha": diff_sha,
        "diff_excerpt": diff_excerpt,
    }
    if failure_evidence:
        entry["failure_evidence"] = failure_evidence
        for key in ("failure_class", "known_facts", "unknowns", "required_inspection", "prior_attempts", "retry_constraints", "jev_feedback", "advisory"):
            entry[key] = failure_evidence.get(key)
    if isinstance(retry_metadata, dict):
        entry["retry_metadata"] = retry_metadata
        entry["retry_number"] = retry_metadata.get("retry_number")
        entry["evidence_added"] = retry_metadata.get("evidence_added")
        entry["hypothesis_changed"] = retry_metadata.get("hypothesis_changed")
        entry["hypothesis_change"] = retry_metadata.get("hypothesis_change")

    state["failed_attempts"].append(entry)
    if len(state["failed_attempts"]) > 2:
        state["failed_attempts"] = state["failed_attempts"][-2:]


def _implement_attempt(state: State) -> Optional[Dict[str, Any]]:
    """Return the latest worker trajectory entry, if one is available."""
    for entry in reversed(state.get("trajectory") or []):
        if isinstance(entry, dict) and entry.get("node") == "implement":
            return entry
    return None


def _read_facts(entry: Optional[Dict[str, Any]]) -> List[str]:
    facts: List[str] = []
    for call in (entry or {}).get("tool_calls", []) or []:
        if not isinstance(call, dict) or call.get("name") not in {"read_file", "grep", "list_dir"}:
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {"value": call.get("args", "")}
        fact = json.dumps({str(k): str(v) for k, v in sorted(args.items())}, sort_keys=True)
        if fact not in facts:
            facts.append(fact)
    return facts


def _worker_hypothesis(entry: Optional[Dict[str, Any]], state: State) -> Optional[str]:
    values: List[Any] = []
    if entry:
        values.extend([entry.get("hypothesis"), entry.get("hypothesis_change"), entry.get("strategy")])
        for call in entry.get("tool_calls", []) or []:
            if isinstance(call, dict) and call.get("name") == "submit_subgoal":
                args = call.get("args")
                if isinstance(args, dict):
                    values.extend([args.get("hypothesis"), args.get("hypothesis_change"), args.get("strategy"), args.get("notes")])
    values.extend([state.get("hypothesis"), state.get("retry_hypothesis"), state.get("retry_strategy")])
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _material_diff_sha(diff: str) -> str:
    """Hash semantic diff content so whitespace-only rewrites are not progress."""
    normalized = "\n".join(line.strip() for line in diff.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _retry_progress_metadata(state: State, diff: str, failure_class: str, failed_check: Optional[str]) -> Optional[Dict[str, Any]]:
    """Build deterministic Phase 7 progress facts from the worker evidence trail."""
    current_entry = _implement_attempt(state)
    if current_entry is None:
        return None
    previous = (state.get("failed_attempts") or [])[-1:]
    prior_meta = previous[0].get("retry_metadata") if previous else None
    prior_meta = prior_meta if isinstance(prior_meta, dict) else {}
    failure_class = str(current_entry.get("failure_class") or state.get("failure_class") or failure_class)
    failed_check = current_entry.get("failed_check") or state.get("failed_check") or failed_check
    current_reads = _read_facts(current_entry)
    prior_reads = set(prior_meta.get("read_evidence") or [])
    current_hypothesis = _worker_hypothesis(current_entry, state)
    prior_hypothesis = prior_meta.get("hypothesis")
    explicit_change = current_entry.get("hypothesis_changed") is True or current_entry.get("hypothesis_change") is not None
    hypothesis_changed = bool(explicit_change or (current_hypothesis and prior_hypothesis and current_hypothesis != prior_hypothesis))
    self_checks = [
        str(call.get("result", ""))[:MAX_SELF_CHECK_OUTPUT_CHARS]
        for call in current_entry.get("tool_calls", []) or []
        if isinstance(call, dict) and call.get("name") in {"validate_changes", "self_check"}
    ]
    prior_validation = prior_meta.get("validation_results") or []
    validation_changed = bool(self_checks and self_checks != prior_validation)
    return {
        "retry_number": len(state.get("failed_attempts") or []),
        "read_evidence": current_reads,
        "new_relevant_reads": sorted(set(current_reads) - prior_reads),
        "evidence_added": bool(set(current_reads) - prior_reads),
        "hypothesis": current_hypothesis,
        "hypothesis_changed": hypothesis_changed,
        "hypothesis_change": {"from": prior_hypothesis, "to": current_hypothesis} if hypothesis_changed else None,
        "material_diff_sha": _material_diff_sha(diff),
        "material_diff_changed": bool(prior_meta.get("material_diff_sha") and _material_diff_sha(diff) != prior_meta.get("material_diff_sha")),
        "strategy_changed": bool(current_entry.get("strategy_changed") is True),
        "failure_class": failure_class,
        "failure_class_addressed": bool(prior_meta.get("failure_class") and failure_class != prior_meta.get("failure_class")),
        "validation_results": self_checks,
        "validation_result_changed": validation_changed,
    }


def _retry_is_dead_end(state: State, metadata: Optional[Dict[str, Any]]) -> bool:
    """Reject a repeated hypothesis unless the worker supplied new evidence or strategy."""
    if not metadata or not (state.get("failed_attempts") or []):
        return False
    return not any(metadata.get(key) for key in (
        "evidence_added", "hypothesis_changed", "material_diff_changed",
        "strategy_changed", "failure_class_addressed", "validation_result_changed",
    ))


def _safe_record_memory(memory: Optional[Any], state: State, *, outcome: str,
                        failure_class: Optional[str] = None, summary: str = "",
                        diff: str = "", subgoal: Optional[Subgoal] = None) -> None:
    """Persist bounded advisory summaries without affecting gate authority."""
    if memory is None or not hasattr(memory, "write"):
        return
    try:
        memory.write(
            task=str(state.get("ticket") or "attempt")[:200],
            summary=(summary or getattr(subgoal, "description", "attempt"))[:1000],
            outcome=outcome[:1000],
            failure_class=failure_class[:1000] if failure_class else None,
            symbols=list(getattr(subgoal, "scope", []) or [])[:32],
            run_id=str(state.get("run_id") or "")[:1000] or None,
            attempt_id=str((state.get("failed_attempts") or [{}])[-1].get("attempt_no", ""))[:1000] or None,
            evidence_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff else None,
        )
    except Exception as exc:
        logger.warning("advisory memory write failed: %s", exc)


def _format_typed_semantic_feedback(
    verdict: ValidationVerdict,
    subgoal: Subgoal,
    diff: str,
    tests_ran: bool,
) -> str:
    lines = ["Semantic validation rejected by Gatekeeper."]
    if verdict.kind:
        if verdict.kind == "fine":
            prob_str = f" (validity probability: {verdict.probability:.2f}, pass needs 0.50)" if getattr(verdict, "probability", None) is not None else ""
            lines.append(f"No specific issue identified by the judge{prob_str}")
        else:
            conf_str = f" (confidence: {verdict.kind_confidence:.2f})" if verdict.kind_confidence is not None else ""
            lines.append(f"Main issue: {verdict.kind}{conf_str}")

    flagged = []
    if verdict.incomplete is not None and verdict.incomplete > 0.5:
        flagged.append(f"Incomplete implementation (probability: {verdict.incomplete:.2f})")
    if verdict.wrong_behavior is not None and verdict.wrong_behavior > 0.5:
        flagged.append(f"Wrong behavior contradicting subgoal or ticket (probability: {verdict.wrong_behavior:.2f})")
    if verdict.conflicts_with_notes is not None and verdict.conflicts_with_notes > 0.5:
        flagged.append(f"Conflicts with investigation notes or conventions (probability: {verdict.conflicts_with_notes:.2f})")
    if flagged:
        lines.append("Flagged issues:")
        for item in flagged:
            lines.append(f"- {item}")

    # Deterministic facts
    files = []
    if diff:
        try:
            files = sorted(list(Workspace._extract_files_from_diff(diff)))
        except Exception:
            files = []
    scope_str = ", ".join(subgoal.scope) if subgoal.scope else "none"
    files_str = ", ".join(files) if files else "none"
    lines.append(f"Files touched: {files_str} (Declared scope: {scope_str})")

    added = 0
    removed = 0
    if diff:
        for dline in diff.splitlines():
            if dline.startswith("+") and not dline.startswith("+++"):
                added += 1
            elif dline.startswith("-") and not dline.startswith("---"):
                removed += 1
    lines.append(f"Lines changed: +{added}, -{removed}")
    lines.append(f"Tests ran: {'Yes' if tests_ran else 'No'}")

    return "\n".join(lines)




class SqliteSaver(BaseCheckpointSaver):
    """SQLite-backed LangGraph checkpointer for persisting FSM state and strike counters."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        serde: Optional[SerializerProtocol] = None,
    ):
        if serde is None:
            serde = JsonPlusSerializer(
                allowed_msgpack_modules=STATE_CUSTOM_TYPES,
                allowed_json_modules=STATE_CUSTOM_TYPES,
            )
        super().__init__(serde=serde)
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
    def from_conn_string(
        cls,
        conn_string: str,
        *,
        serde: Optional[SerializerProtocol] = None,
    ) -> "SqliteSaver":
        if conn_string != ":memory:" and not conn_string.startswith("file:"):
            Path(conn_string).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(conn_string, check_same_thread=False)
        return cls(conn, serde=serde)

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


def _test_targets(workspace: Any, diff: str, scope: Sequence[str] = ()) -> List[str]:
    """Find existing, affected test files; an empty list means run the full suite."""
    root = getattr(workspace, "worktree_dir", None) or getattr(workspace, "repo_dir", None)
    if not isinstance(root, (str, Path)):
        return []
    base = Path(root).resolve()
    targets: set[str] = set()
    for filename in Workspace._extract_files_from_diff(diff) | set(scope):
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts:
            continue
        candidates = [path]
        if path.suffix == ".py" and not path.name.startswith("test_"):
            candidates.extend((Path("tests") / f"test_{path.stem}.py",
                               Path("tests") / path.parent / f"test_{path.stem}.py"))
        elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            candidates.extend(path.with_name(f"{path.stem}.{kind}{path.suffix}")
                              for kind in ("test", "spec"))
        for candidate in candidates:
            if (candidate.name.startswith("test_") or ".test." in candidate.name or ".spec." in candidate.name):
                resolved = (base / candidate).resolve()
                if resolved.is_relative_to(base) and resolved.is_file():
                    targets.add(candidate.as_posix())
    return sorted(targets)


def _test_evidence(policy: str, ran: bool, runner: Any, targets: List[str], *, required: bool) -> Dict[str, Any]:
    details = runner if ran and isinstance(runner, dict) else {}
    evidence: Dict[str, Any] = {
        "policy": policy, "ran": ran, "ecosystem": details.get("ecosystem"),
        "command": details.get("executed_command") or details.get("command"),
        "targets": targets if ran else [], "exit_code": details.get("exit_code"),
        "output_tail": str(details.get("output_tail") or "")[-MAX_SELF_CHECK_OUTPUT_CHARS:],
        "outcome": details.get("outcome", "UNKNOWN") if ran else
                   ("EXCLUDED" if policy == "never" or not required else "UNVERIFIED"),
    }
    for name in ("base_failures", "baseline_failures", "new_failures"):
        if name in details:
            evidence[name] = details[name]
    return evidence


def _safe_escalate_deadlock(
    gatekeeper: Any,
    trajectory: List[Dict[str, Any]],
    triggering_tier: str,
    integration_branch: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> None:
    if gatekeeper is None or not hasattr(gatekeeper, "escalate_deadlock"):
        return
    from jev.gatekeeper import Gatekeeper
    if isinstance(gatekeeper, Gatekeeper):
        eff_thread_id = thread_id
        if not eff_thread_id and integration_branch:
            eff_thread_id = integration_branch.replace("jev-ticket-", "")
        gatekeeper.escalate_deadlock(
            trajectory=trajectory,
            triggering_tier=triggering_tier,
            thread_id=eff_thread_id,
            integration_branch=integration_branch,
        )
    else:
        kwargs: Dict[str, Any] = {}
        target = getattr(gatekeeper.escalate_deadlock, "side_effect", None) or gatekeeper.escalate_deadlock
        try:
            sig = inspect.signature(target)
            has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not has_var_kwargs and "integration_branch" in sig.parameters:
                if integration_branch:
                    kwargs["integration_branch"] = integration_branch
            elif has_var_kwargs and integration_branch:
                kwargs["integration_branch"] = integration_branch
        except Exception:
            if integration_branch:
                kwargs["integration_branch"] = integration_branch
        gatekeeper.escalate_deadlock(
            trajectory=trajectory,
            triggering_tier=triggering_tier,
            **kwargs,
        )


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
    max_investigation_turns: int = 20,
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
        if is_secret_path(path):
            return f"Error: Access denied to protected secret file: {path}"

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
                    return _cap_read_text(output)
            except Exception:
                pass

        if worktree:
            if target.exists():
                if target.is_dir():
                    return f"Error: {path} is a directory, not a file. Use list_dir instead."
                try:
                    return _cap_read_text(target.read_text(encoding="utf-8", errors="replace"))
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

    max_turns = max_investigation_turns
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
    recorder: Optional[Any] = None,
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
                    id="sg-1",
                    description=ticket,
                    scope=[],
                    expects_tests=True,
                    status="pending",
                )
            ]
            state["plan_queue"] = plan_queue
        else:
            normalized_queue: List[Subgoal] = []
            for idx, item in enumerate(plan_queue):
                if isinstance(item, dict):
                    sg = Subgoal(**item)
                elif isinstance(item, Subgoal):
                    sg = item
                else:
                    continue
                if not sg.id:
                    sg.id = f"sg-{idx + 1}"
                if not sg.status:
                    sg.status = "pending"
                normalized_queue.append(sg)
            state["plan_queue"] = normalized_queue

        if recorder and state.get("plan_queue"):
            safe_rec = recorder if isinstance(recorder, SafeRecorder) else SafeRecorder(recorder)
            run_id = state.get("run_id") or state.get("thread_id") or "unknown"
            safe_rec.record_subgoals(run_id, state["plan_queue"])

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

    if state.get("repo_profile") == "on" and state.get("repo_profile_text"):
        prompt_lines.append(
            f"\nRepository Profile (hard facts detected from manifests, not guesses):\n{state['repo_profile_text']}"
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
                    _safe_escalate_deadlock(
                        gatekeeper,
                        trajectory=state["trajectory"],
                        triggering_tier="planning",
                        thread_id=state.get("thread_id"),
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
        for idx, sg in enumerate(subgoals):
            if not sg.id:
                sg.id = f"sg-{idx + 1}"
            if not sg.status:
                sg.status = "pending"
        state["plan_queue"] = subgoals

        if recorder and state.get("plan_queue"):
            safe_rec = recorder if isinstance(recorder, SafeRecorder) else SafeRecorder(recorder)
            run_id = state.get("run_id") or state.get("thread_id") or "unknown"
            safe_rec.record_subgoals(run_id, state["plan_queue"])

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
    _safe_escalate_deadlock(
        gatekeeper,
        trajectory=state["trajectory"],
        triggering_tier="planning",
        thread_id=state.get("thread_id"),
    )
    return state



def node_implement(
    state: State,
    workspace: Optional[Any] = None,
    llm: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
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
            state["failed_attempts"] = []

    subgoal: Optional[Subgoal] = None
    if isinstance(current_subgoal, dict):
        subgoal = Subgoal(**current_subgoal)
    elif isinstance(current_subgoal, Subgoal):
        subgoal = current_subgoal

    if subgoal is not None:
        subgoal.status = "in_progress"
        state["current_subgoal"] = subgoal

    state["worker_tokens_in"] = None
    state["worker_tokens_out"] = None

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
    self_check_calls = 0
    self_check_outcomes: List[str] = []

    if llm is not None and subgoal is not None and workspace is not None:
        submitted = False
        implementation_read_calls = 0

        raw_worktree = getattr(workspace, "worktree_dir", None) or getattr(workspace, "repo_dir", None)
        implementation_root = Path(raw_worktree).resolve() if raw_worktree else None

        def _resolve_implementation_read_path(path: str, expect_directory: Optional[bool] = None) -> Tuple[Optional[Path], str]:
            if implementation_root is None:
                return None, "Workspace unavailable; read tools require a worktree."
            if not path:
                path = "."
            candidate = Path(path)
            target = (implementation_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            try:
                target.relative_to(implementation_root)
            except ValueError:
                return None, f"Error: Access denied for path outside workspace: {path}"
            if not target.exists():
                return None, f"Path not found: {path}"
            if expect_directory is True and not target.is_dir():
                return None, f"Not a directory: {path}"
            if expect_directory is False and not target.is_file():
                return None, f"Not a file: {path}"
            return target, ""

        def _consume_implementation_read_budget(tool_name: str) -> Optional[str]:
            nonlocal implementation_read_calls
            if implementation_read_calls >= MAX_IMPLEMENT_READ_CALLS:
                return (
                    f"Error: implementation read-tool call budget exhausted ({MAX_IMPLEMENT_READ_CALLS} calls)."
                )
            implementation_read_calls += 1
            return None

        def _cap_implementation_output(output: Any) -> str:
            text = str(output)
            if len(text) > MAX_IMPLEMENT_READ_CHARS:
                return text[:MAX_IMPLEMENT_READ_CHARS] + (
                    f"\n[truncated: showing first {MAX_IMPLEMENT_READ_CHARS} of {len(text)} characters]"
                )
            return text

        def _scope_path(path: str) -> str:
            return Path(str(path).replace("\\", "/")).as_posix().lstrip("./")

        def _path_in_declared_scope(path: str) -> bool:
            """Allow inspection only in the active subgoal's declared scope."""
            normalized = _scope_path(path)
            scopes = {_scope_path(scope) for scope in (subgoal.scope or [])}
            return not scopes or any(
                normalized == scope or normalized.startswith(f"{scope}/")
                for scope in scopes
            )

        def _validate_changes(paths: Optional[List[str]] = None) -> str:
            """Run advisory compile and syntax checks on staged files."""
            nonlocal self_check_calls, self_check_outcomes
            self_check_calls += 1
            if self_check_calls > 4:
                self_check_outcomes.append("skipped (limit reached)")
                return "Self-check: skipped (limit reached)"

            if implementation_root is None:
                self_check_outcomes.append("failed")
                return "Self-check: failed\nReason: implementation worktree is unavailable."

            scope_paths = {
                Path(scope_path).as_posix().lstrip("./")
                for scope_path in (subgoal.scope or [])
            }
            requested = paths or []
            candidates = requested
            if not candidates:
                try:
                    diff = workspace.get_staged_diff() if hasattr(workspace, "get_staged_diff") else ""
                    candidates = sorted(Workspace._extract_files_from_diff(str(diff or "")))
                except Exception:
                    candidates = []
            if not candidates and hasattr(workspace, "worker_staged_paths") and workspace.worker_staged_paths:
                candidates = sorted(list(workspace.worker_staged_paths))
            if not candidates:
                candidates = list(scope_paths)

            for raw_path in candidates:
                normalized = Path(str(raw_path).replace("\\", "/")).as_posix().lstrip("./")
                if scope_paths and not _path_in_declared_scope(normalized):
                    self_check_outcomes.append("failed")
                    return _cap_self_check_output(
                        f"Self-check: failed\nReason: path '{raw_path}' is outside declared scope."
                    )
                target = (implementation_root / normalized).resolve()
                try:
                    target.relative_to(implementation_root)
                except ValueError:
                    self_check_outcomes.append("failed")
                    return _cap_self_check_output(
                        f"Self-check: failed\nReason: path '{raw_path}' escapes the implementation worktree."
                    )

            safe_python_files = sorted(set(
                f for f in candidates
                if Path(f).suffix.lower() == ".py" and (implementation_root / f).is_file()
            ))

            python_syntax_error: Optional[str] = None
            if safe_python_files:
                checker = (
                    "import ast, pathlib, sys\n"
                    "for name in sys.argv[1:]:\n"
                    "    source = pathlib.Path(name).read_text(encoding='utf-8')\n"
                    "    ast.parse(source, filename=name)\n"
                    "    print(f'OK: {name}')\n"
                )
                try:
                    completed = subprocess.run(
                        [sys.executable, "-c", checker, *safe_python_files],
                        cwd=str(implementation_root),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=120,
                        check=False,
                    )
                    if completed.returncode != 0:
                        commit_ref = state.get("subgoal_base_commit") or state.get("base_commit") or getattr(workspace, "base_commit", None)
                        is_new_error = True
                        if commit_ref and hasattr(workspace, "_run_repo_git"):
                            base_err_count = 0
                            for py_f in safe_python_files:
                                show_res = workspace._run_repo_git(["show", f"{commit_ref}:{py_f}"])
                                if show_res.returncode == 0:
                                    try:
                                        ast.parse(show_res.stdout, filename=py_f)
                                    except SyntaxError:
                                        base_err_count += 1
                            if base_err_count == len(safe_python_files):
                                is_new_error = False

                        if is_new_error:
                            output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
                            python_syntax_error = (
                                f"Self-check: failed\nCheck: Python syntax\nFiles: {', '.join(safe_python_files)}"
                                + (f"\nDiagnostics:\n{output}" if output else "")
                            )
                except subprocess.TimeoutExpired:
                    self_check_outcomes.append("timeout")
                    return _cap_self_check_output(
                        f"Self-check: timeout after 120 seconds\n"
                        f"Check: Python syntax ({', '.join(safe_python_files)})"
                    )
                except Exception as exc:
                    self_check_outcomes.append("failed")
                    return _cap_self_check_output(f"Self-check: failed\nReason: {type(exc).__name__}")

            if python_syntax_error is not None:
                self_check_outcomes.append("failed")
                return _cap_self_check_output(python_syntax_error)

            # Match compile handler for candidate source files
            commit_ref = state.get("subgoal_base_commit") or state.get("base_commit") or getattr(workspace, "base_commit", None)
            matched_handler = None
            if workspace is not None and hasattr(workspace, "check_compile"):
                touched_sources = {
                    Path(f).as_posix()
                    for f in candidates
                    if Path(f).suffix.lower() in ALL_SOURCE_EXTENSIONS
                }
                for h in ECOSYSTEM_COMPILE_HANDLERS:
                    if h.matches(touched_sources, implementation_root, getattr(workspace, "repo_dir", implementation_root)):
                        matched_handler = h
                        break

            if matched_handler is None:
                if safe_python_files:
                    self_check_outcomes.append("passed")
                    return _cap_self_check_output(
                        f"Self-check: passed\nCheck: Python syntax\nFiles: {', '.join(safe_python_files)}"
                    )
                self_check_outcomes.append("skipped")
                return "Self-check: skipped\nReason: no changed files require a compile or syntax check."

            if matched_handler.name == "python" and safe_python_files:
                self_check_outcomes.append("passed")
                return _cap_self_check_output(
                    f"Self-check: passed\nCheck: Python syntax\nFiles: {', '.join(safe_python_files)}"
                )

            # Call workspace.check_compile
            try:
                compile_res = workspace.check_compile(scope=candidates, base_commit=commit_ref)
            except Exception as exc:
                self_check_outcomes.append("skipped")
                return f"Self-check: skipped\nReason: {exc}"
            finally:
                if hasattr(workspace, "discard_engine_touched"):
                    workspace.discard_engine_touched()
                else:
                    if hasattr(workspace, "_compute_provenance_deltas"):
                        workspace._compute_provenance_deltas()
                    if hasattr(workspace, "_discard_engine_touched"):
                        workspace._discard_engine_touched()

            c_outcome = compile_res.get("outcome")
            detail = compile_res.get("detail", "")
            new_errors = compile_res.get("new_errors") or []
            ecosystem = compile_res.get("ecosystem") or matched_handler.name

            if c_outcome == CompileOutcome.PASSED.value:
                self_check_outcomes.append("passed")
                return _cap_self_check_output(f"Self-check: passed\nCheck: {ecosystem} compile\nDetail: {detail}")
            elif c_outcome == CompileOutcome.FAILED.value:
                self_check_outcomes.append("failed")
                err_lines = [f"Self-check: failed\nCheck: {ecosystem} compile\nDetail: {detail}"]
                if new_errors:
                    err_lines.append("Errors:\n" + "\n".join(new_errors))
                elif compile_res.get("output_tail"):
                    err_lines.append(f"Diagnostics:\n{compile_res.get('output_tail')}")
                return _cap_self_check_output("\n".join(err_lines))
            elif c_outcome in (CompileOutcome.EXEMPT.value, CompileOutcome.NO_COMPILE_COMMAND.value, CompileOutcome.ENV_NOT_READY.value):
                self_check_outcomes.append("skipped")
                reason = detail or "compile check not applicable"
                return _cap_self_check_output(f"Self-check: skipped\nReason: {reason}")
            else:
                self_check_outcomes.append("skipped")
                return _cap_self_check_output(f"Self-check: skipped\nReason: {detail}")

        @tool
        def validate_changes(paths: Optional[List[str]] = None) -> str:
            """Run a bounded, read-only check on changed files before submission.

            This is advisory worker feedback; authoritative Tier 0 still runs in node_gate.
            """
            return _validate_changes(paths)

        @tool
        def list_dir(path: str = ".") -> str:
            """List a directory in the implementation worktree without modifying it."""
            budget_error = _consume_implementation_read_budget("list_dir")
            if budget_error:
                return budget_error
            target, error = _resolve_implementation_read_path(path, expect_directory=True)
            if target is None:
                return error
            try:
                ignored = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}
                entries = [
                    f"{child.name}/" if child.is_dir() else child.name
                    for child in sorted(target.iterdir())
                    if child.name not in ignored
                ]
                return _cap_implementation_output("\n".join(entries) if entries else "(empty directory)")
            except OSError as exc:
                return _cap_implementation_output(f"Error listing directory {path}: {exc}")

        @tool
        def read_file(path: str) -> str:
            """Read a file in the implementation worktree without modifying it."""
            budget_error = _consume_implementation_read_budget("read_file")
            if budget_error:
                return budget_error
            if is_secret_path(path):
                return f"Error: Access denied to protected secret file: {path}"
            target, error = _resolve_implementation_read_path(path, expect_directory=False)
            if target is None:
                return error
            try:
                return _cap_implementation_output(target.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                return _cap_implementation_output(f"Error reading file {path}: {exc}")

        @tool
        def grep(query: str, path: Optional[str] = None) -> str:
            """Search files in the implementation worktree without modifying it."""
            budget_error = _consume_implementation_read_budget("grep")
            if budget_error:
                return budget_error
            if not query:
                return "Error: query parameter is required for grep."
            target, error = _resolve_implementation_read_path(path or ".")
            if target is None:
                return error
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error:
                pattern = re.compile(re.escape(query), re.IGNORECASE)
            ignored = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}
            files = [target] if target.is_file() else [
                item for item in target.rglob("*")
                if item.is_file() and not any(part in ignored for part in item.relative_to(implementation_root).parts)
            ]
            matches: List[str] = []
            for item in files:
                try:
                    resolved_item = item.resolve()
                    resolved_item.relative_to(implementation_root)
                    relative = item.relative_to(implementation_root).as_posix()
                    for line_no, line in enumerate(resolved_item.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if pattern.search(line):
                            matches.append(f"{relative}:{line_no}:{line}")
                            if len(matches) >= 50:
                                break
                except (OSError, ValueError):
                    continue
                if len(matches) >= 50:
                    break
            return _cap_implementation_output("\n".join(matches) if matches else "No matches found.")

        @tool
        def inspect_structure(
            path: Optional[str] = None,
            symbol: Optional[str] = None,
            query: Optional[str] = None,
        ) -> str:
            """Retrieve bounded Python symbols, definitions, imports, and references.

            Inspection is read-only and limited to the implementation worktree and
            the active subgoal scope. It consumes the same read-call budget as the
            other implementation inspection tools.
            """
            budget_error = _consume_implementation_read_budget("inspect_structure")
            if budget_error:
                return budget_error
            requested_path = path or "."
            target, error = _resolve_implementation_read_path(requested_path)
            if target is None:
                return error
            relative_path = target.relative_to(implementation_root).as_posix() if implementation_root else "."
            if not _path_in_declared_scope(relative_path):
                return (
                    f"Error: Access denied for path outside declared scope: {requested_path}"
                )
            try:
                retriever = AstWorkspaceRetriever(
                    implementation_root,
                    limits=RetrievalLimits(
                        max_files=MAX_IMPLEMENT_RETRIEVAL_FILES,
                        max_items=MAX_IMPLEMENT_RETRIEVAL_ITEMS,
                    ),
                )
                result = retriever.retrieve(path=relative_path, symbol=symbol, query=query)
                payload = {
                    "symbols": [item.__dict__ for item in result.symbols],
                    "definitions": [item.__dict__ for item in result.definitions],
                    "imports": [item.__dict__ for item in result.imports],
                    "references": [item.__dict__ for item in result.references],
                    "issues": [item.__dict__ for item in result.issues],
                    "files_scanned": result.files_scanned,
                    "truncated": result.truncated,
                }
                return _cap_implementation_output(json.dumps(payload, sort_keys=True))
            except (ValueError, PermissionError) as exc:
                return f"Error: Access denied for path {requested_path}: {exc}"
            except OSError as exc:
                return _cap_implementation_output(f"Error inspecting structure: {exc}")

        def _execute_stage_file_mutation(path: str, content: str) -> str:
            if not path:
                return "Error: path is required for stage_file_mutation."

            try:
                workspace.resolve_in_worktree(path)
            except (ValueError, PermissionError) as e:
                return f"Error: Access denied: path '{path}' escapes worktree: {e}"

            norm_path = Path(path).as_posix()
            if norm_path.startswith("./"):
                norm_path = norm_path[2:]

            norm_scope = set()
            for s in (subgoal.scope or []):
                s_norm = Path(s).as_posix()
                if s_norm.startswith("./"):
                    s_norm = s_norm[2:]
                norm_scope.add(s_norm)

            if norm_scope and norm_path not in norm_scope:
                return (
                    f"Error: Target path '{path}' is not within declared scope {subgoal.scope}. "
                    "You may only mutate files within the declared scope for this subgoal. "
                    "Subsequent subgoals will address other files."
                )

            if hasattr(workspace, "stage_file_mutation"):
                try:
                    workspace.stage_file_mutation(path, content)
                    return f"Successfully staged mutation for {path}"
                except (ValueError, PermissionError) as e:
                    return f"Error: Access denied staging mutation for '{path}': {e}"
                except Exception as e:
                    return f"Error staging mutation for {path}: {e}"
            return f"Workspace unavailable; could not stage mutation for {path}"

        @tool
        def stage_file_mutation(path: str, content: str) -> str:
            """Stage a file mutation (create or overwrite file content) in the workspace worktree.

            Args:
                path: Path to the file to modify, relative to repository root.
                content: The complete new content of the file.
            """
            return _execute_stage_file_mutation(path, content)

        @tool
        def submit_subgoal(notes: str = "") -> str:
            """Signal that the implementation for the current subgoal is complete and ready for gating.

            Args:
                notes: Optional summary or notes about the changes made.
            """
            nonlocal submitted
            submitted = True
            return "Subgoal submitted."

        tools = [list_dir, read_file, grep, inspect_structure, stage_file_mutation, validate_changes, submit_subgoal]

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

        if state.get("repo_profile") == "on" and state.get("repo_profile_text"):
            prompt_lines.append(
                f"Repository Profile (hard facts detected from manifests, not guesses):\n{state['repo_profile_text']}"
            )

        if state.get("last_feedback"):
            prompt_lines.append(f"Previous attempt failed gate verification. Feedback:\n{state['last_feedback']}")

        # Dead-end block (Phase 3 Item B)
        retry_intel = state.get("retry_intel", "on")
        if retry_intel != "off" and state.get("failed_attempts"):
            block_lines = ["Earlier attempts at this subgoal failed. Do not repeat them."]
            for fa in state["failed_attempts"]:
                att_lines = [
                    f"Attempt {fa.get('attempt_no', '?')} (Outcome: {fa.get('outcome')}, Failed check: {fa.get('failed_check')}):"
                ]
                errs = fa.get("normalized_error_keys") or fa.get("error_keys")
                if errs:
                    att_lines.append(f"Errors: {json.dumps(errs)}")
                if fa.get("diff_excerpt"):
                    att_lines.append(f"Diff excerpt:\n{fa['diff_excerpt']}")
                block_lines.append("\n".join(att_lines))
            prompt_lines.append("\n\n".join(block_lines))


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
                        file_content = target_file.read_text(encoding="utf-8", errors="replace")
                        if len(file_content) > MAX_IMPLEMENT_FILE_CHARS:
                            msg = (
                                f"File '{s_file}' ({len(file_content)} characters) is too large for a safe full-file rewrite "
                                f"(maximum allowed is {MAX_IMPLEMENT_FILE_CHARS} characters)."
                            )
                            state["status"] = "escalated"
                            state["gate_status"] = "escalated"
                            state["last_feedback"] = msg
                            state["trajectory"].append({
                                "node": "implement",
                                "subgoal": getattr(subgoal, "description", ""),
                                "error": msg,
                            })
                            _safe_escalate_deadlock(
                                gatekeeper,
                                trajectory=state.get("trajectory", []),
                                triggering_tier="implementation",
                                integration_branch=state.get("integration_branch"),
                                thread_id=state.get("thread_id"),
                            )
                            return state
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
            "5. Use `list_dir`, `read_file`, `grep`, and `inspect_structure` only for bounded, read-only inspection; they are limited to the worktree and a per-attempt call budget. `inspect_structure` is limited to the declared scope.\n"
            "6. Call `validate_changes` to type-check and validate your staged files before calling `submit_subgoal`. It provides bounded, read-only diagnostics on your changes (advisory only; it never replaces authoritative Tier 0).\n"
            "7. When all changes are staged and you are done, call the `submit_subgoal` tool to submit your work for gate verification."
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

            usage_meta = getattr(response, "usage_metadata", None)
            if isinstance(usage_meta, dict):
                in_t = usage_meta.get("input_tokens")
                out_t = usage_meta.get("output_tokens")
                if in_t is not None:
                    state["worker_tokens_in"] = (state.get("worker_tokens_in") or 0) + in_t
                if out_t is not None:
                    state["worker_tokens_out"] = (state.get("worker_tokens_out") or 0) + out_t

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

                if name == "submit_subgoal":
                    submitted = True
                    result = "Subgoal submitted."
                    stored_args = args
                elif name in ("stage_file_mutation", "write_file"):
                    actual_path = args.get("path") or args.get("file_path") or args.get("filename") or ""
                    actual_content = args.get("content")
                    if actual_content is None:
                        actual_content = args.get("code", args.get("text", ""))
                    result = _execute_stage_file_mutation(actual_path, str(actual_content))
                    c_str = str(actual_content)
                    stored_args = {
                        "path": actual_path,
                        "content_sha": hashlib.sha256(c_str.encode("utf-8")).hexdigest(),
                        "content_chars": len(c_str),
                    }
                elif name in ("list_dir", "read_file", "grep"):
                    if name == "list_dir":
                        read_path = args.get("path") or args.get("directory") or "."
                        result = list_dir.invoke({"path": read_path})
                        stored_args = {"path": read_path}
                    elif name == "read_file":
                        read_path = args.get("path") or args.get("file_path") or args.get("filename") or ""
                        result = read_file.invoke({"path": read_path})
                        stored_args = {"path": read_path}
                    else:
                        read_query = args.get("query") or args.get("pattern") or args.get("search_term") or ""
                        read_path = args.get("path") or args.get("directory") or args.get("file_path")
                        grep_args = {"query": read_query}
                        if read_path:
                            grep_args["path"] = read_path
                        result = grep.invoke(grep_args)
                        stored_args = grep_args
                elif name == "inspect_structure":
                    inspect_args = {
                        key: args.get(key)
                        for key in ("path", "symbol", "query")
                        if args.get(key) is not None
                    }
                    result = inspect_structure.invoke(inspect_args)
                    stored_args = inspect_args
                elif name in ("validate_changes", "self_check"):
                    check_paths = args.get("paths") or args.get("files")
                    if isinstance(check_paths, str):
                        check_paths = [check_paths]
                    result = validate_changes.invoke({"paths": check_paths})
                    stored_args = {"paths": check_paths} if check_paths else {}
                else:
                    result = f"Unknown tool: {name}. Available tools: list_dir, read_file, grep, inspect_structure, stage_file_mutation, validate_changes, submit_subgoal."
                    stored_args = args

                result_text = str(result)
                result_cap = (MAX_SELF_CHECK_OUTPUT_CHARS + 200) if name in ("validate_changes", "self_check") else (2_000 if name == "inspect_structure" else 200)
                executed_tools.append({"name": name, "args": stored_args, "result": result_text[:result_cap]})

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
    traj_entry["self_check_calls"] = self_check_calls
    traj_entry["self_check_outcomes"] = list(self_check_outcomes)
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


def _teardown_worktree(state: State, workspace: Optional[Any]) -> None:
    """Helper to tear down active worktree and restore workspace repo_dir."""
    if workspace is not None:
        is_wt = _is_active_worktree(state, workspace)
        wt_path, wt_branch = _get_active_worktree_info(state, workspace)
        if is_wt and hasattr(workspace, "discard_subgoal_worktree"):
            workspace.discard_subgoal_worktree(wt_path, wt_branch)
        elif hasattr(workspace, "rollback_subgoal"):
            workspace.rollback_subgoal()
        if hasattr(workspace, "repo_dir") and isinstance(workspace.repo_dir, Path):
            workspace.worktree_dir = workspace.repo_dir
    state["current_worktree_path"] = None
    state["current_worktree_branch"] = None


def _snapshot_attempt(
    recorder: Optional[Any],
    state: State,
    subgoal_id: str,
    tier: str,
    outcome: str,
    failed_check: Optional[str] = None,
    diff: str = "",
    compile_info: Optional[Dict[str, Any]] = None,
    tests_ran: bool = False,
    test_outcome: Optional[str] = None,
    verdict: Optional[Any] = None,
    feedback_sent: Optional[str] = None,
    evidence_level: Optional[str] = None,
    dependency_delta: Optional[str] = None,
    jev_state_keys: Optional[str] = None,
    failure_evidence: Optional[Any] = None,
    retry_metadata: Optional[Any] = None,
    baseline_failures: Optional[int] = None,
    new_failures: Optional[int] = None,
    full_suite_clean: Optional[bool] = None,
    mech_evidence: Optional[Any] = None,
    unverified_kind: Optional[str] = None,
) -> None:
    """Helper to snapshot an attempt row to the ledger."""
    if not recorder:
        return
    if baseline_failures is None:
        baseline_failures = state.get("baseline_failures")
    if new_failures is None:
        new_failures = state.get("new_failures")
    if full_suite_clean is None:
        full_suite_clean = state.get("full_suite_clean")

    classification = state.get("test_classification")
    if classification:
        if test_outcome is None or test_outcome in ("PASSED", "FAILED"):
            test_outcome = classification
        if outcome in ("mechanical_failure", "passed", None):
            outcome = classification
    if not isinstance(jev_state_keys, str):
        jev_state_keys = None
    safe_rec = recorder if isinstance(recorder, SafeRecorder) else SafeRecorder(recorder)
    run_id = state.get("run_id") or state.get("thread_id") or "unknown"
    if isinstance(diff, str):
        diff_bytes = diff.encode("utf-8")
    elif isinstance(diff, bytes):
        diff_bytes = diff
        diff = diff.decode("utf-8", errors="replace")
    else:
        diff_bytes = b""
        diff = ""
    diff_sha = hashlib.sha256(diff_bytes).hexdigest()
    files: List[str] = []
    if diff:
        try:
            files = sorted(list(Workspace._extract_files_from_diff(diff)))
        except Exception:
            files = []
    norm_errors = _normalize_compile_errors(compile_info)
    jev_valid = getattr(verdict, "valid", None) if verdict else None
    jev_prob = getattr(verdict, "probability", None) if verdict else None
    jev_model = getattr(verdict, "model", None) if verdict else None
    jev_raw = None
    if verdict and getattr(verdict, "raw_response", None):
        try:
            jev_raw = json.dumps(verdict.raw_response)
        except Exception:
            jev_raw = str(verdict.raw_response)
    jev_usage = getattr(verdict, "usage", None) if verdict else None
    jev_tokens_in = None
    jev_tokens_out = None
    if isinstance(jev_usage, dict):
        jev_tokens_in = jev_usage.get("tokens_in") or jev_usage.get("prompt_tokens") or jev_usage.get("input_tokens")
        jev_tokens_out = jev_usage.get("tokens_out") or jev_usage.get("completion_tokens") or jev_usage.get("output_tokens")

    implementation_tools: List[Dict[str, Any]] = []
    read_evidence: List[Dict[str, Any]] = []
    self_check_evidence: List[Dict[str, Any]] = []
    for entry in reversed(state.get("trajectory", [])):
        if entry.get("node") != "implement":
            continue
        for tool_call in entry.get("tool_calls", []):
            if not isinstance(tool_call, dict):
                continue
            implementation_tools.append(tool_call)
            if tool_call.get("name") in {"list_dir", "read_file", "grep"}:
                read_evidence.append(tool_call)
            if tool_call.get("name") in {"validate_changes", "self_check"}:
                self_check_evidence.append(tool_call)
        break

    mech_ev_obj = None
    if mech_evidence is not None:
        if isinstance(mech_evidence, MechanicalEvidence):
            mech_ev_obj = mech_evidence
        elif isinstance(mech_evidence, dict):
            try:
                mech_ev_obj = MechanicalEvidence(**mech_evidence)
            except Exception:
                mech_ev_obj = None

    safe_rec.record_attempt(
        run_id=run_id,
        subgoal_id=subgoal_id,
        tier=tier,
        outcome=outcome,
        failed_check=failed_check,
        diff=diff,
        diff_sha=diff_sha,
        files=files,
        compile_errors=norm_errors if norm_errors else None,
        tests_ran=tests_ran,
        test_outcome=test_outcome,
        jev_valid=jev_valid,
        jev_probability=jev_prob,
        jev_model=jev_model,
        jev_raw=jev_raw,
        jev_tokens_in=jev_tokens_in,
        jev_tokens_out=jev_tokens_out,
        feedback_sent=feedback_sent,
        tokens_in=state.get("worker_tokens_in") if subgoal_id != "__verify__" else None,
        tokens_out=state.get("worker_tokens_out") if subgoal_id != "__verify__" else None,
        evidence_level=evidence_level,
        dependency_delta=dependency_delta,
        jev_state_keys=jev_state_keys,
        failure_evidence=failure_evidence,
        tool_evidence=implementation_tools or None,
        read_evidence=read_evidence or None,
        self_check_evidence=self_check_evidence or None,
        retry_number=(retry_metadata or {}).get("retry_number") if isinstance(retry_metadata, dict) else None,
        evidence_added=(retry_metadata or {}).get("evidence_added") if isinstance(retry_metadata, dict) else None,
        hypothesis_changed=(retry_metadata or {}).get("hypothesis_changed") if isinstance(retry_metadata, dict) else None,
        hypothesis_change=(retry_metadata or {}).get("hypothesis_change") if isinstance(retry_metadata, dict) else None,
        retry_metadata=retry_metadata,
        baseline_failures=baseline_failures,
        new_failures=new_failures,
        full_suite_clean=full_suite_clean,
        mech_evidence=mech_ev_obj,
        unverified_kind=unverified_kind,
    )


def node_gate(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
    recorder: Optional[Any] = None,
    advisory: Optional[Any] = None,
    memory: Optional[Any] = None,
    replan_enabled: bool = False,
    replan_planner: Optional[Any] = None,
    replan_validator: Optional[Any] = None,
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
    if state.get("status") == "escalated":
        return state

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

    safe_rec = SafeRecorder(recorder) if recorder and not isinstance(recorder, SafeRecorder) else recorder
    run_id = state.get("run_id") or state.get("thread_id") or "unknown"
    subgoal_id = getattr(subgoal, "id", None) or "sg-1"

    raw_diff = workspace.get_staged_diff() if hasattr(workspace, "get_staged_diff") else ""
    if isinstance(raw_diff, str):
        diff = raw_diff
    elif isinstance(raw_diff, bytes):
        diff = raw_diff.decode("utf-8", errors="replace")
    else:
        diff = ""
    diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest()

    # Duplicate-diff guard (Phase 3 Item C)
    retry_intel = state.get("retry_intel")
    if retry_intel is None:
        retry_intel = "on" if state.get("failed_attempts") is not None else "off"
    if retry_intel != "off":
        matching_fa = None
        for fa in state.get("failed_attempts", []):
            if fa.get("diff_sha") == diff_sha:
                matching_fa = fa
                break
        if matching_fa is not None:
            orig_tier = matching_fa.get("tier", "mechanical")
            if orig_tier == "semantic":
                current_strikes = state.get("semantic_strike_count", 0) + 1
                state["semantic_strike_count"] = current_strikes
                state["gate_status"] = "semantic_failure"
            else:
                current_strikes = state.get("mechanical_strike_count", 0) + 1
                state["mechanical_strike_count"] = current_strikes
                state["gate_status"] = "mechanical_failure"

            prev_att_no = matching_fa.get("attempt_no", 1)
            prev_reason = matching_fa.get("failed_check") or matching_fa.get("outcome") or "failure"
            duplicate_evidence = _attempt_evidence(
                state,
                failure_class="duplicate_diff",
                failed_check=matching_fa.get("failed_check"),
                diff=diff,
                detail=f"Identical to attempt {prev_att_no}.",
                subgoal=subgoal,
                workspace=workspace,
            )
            duplicate_evidence["prior_attempts"] = [{
                "hash": matching_fa.get("diff_sha", diff_sha),
                "excerpt": _extract_diff_excerpt(matching_fa.get("diff_excerpt", ""), max_chars=1200),
            }]
            advisory_telemetry = _apply_advisory(advisory, duplicate_evidence, state)
            advisory_feedback = advisory_telemetry.get("feedback", "")
            feedback_msg = (
                f"Duplicate diff detected: identical to attempt {prev_att_no} which failed ({prev_reason}). "
                "Do not submit identical diffs.\n"
                f"{advisory_feedback}\n"
                f"Failure evidence: {_feedback_packet(duplicate_evidence)}"
            )
            state["last_feedback"] = feedback_msg

            _record_failed_attempt_in_state(
                state,
                tier=orig_tier,
                outcome="duplicate_diff",
                failed_check=matching_fa.get("failed_check"),
                diff=diff,
                failure_evidence=duplicate_evidence,
            )
            _snapshot_attempt(
                safe_rec,
                state,
                subgoal_id,
                tier=orig_tier,
                outcome="duplicate_diff",
                failed_check=matching_fa.get("failed_check"),
                diff=diff,
                feedback_sent=feedback_msg,
                failure_evidence=duplicate_evidence,
            )
            _teardown_worktree(state, workspace)

            state["trajectory"].append({
                "node": "gate",
                "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
                "gate_status": "duplicate_diff",
                "tier0_result": None,
                "compile": None,
                "test_runner": None,
                "jev_request": None,
                "jev_verdict": None,
                "advisory": advisory_telemetry,
            })

            if current_strikes >= 3:
                subgoal.status = "failed"
                state["current_subgoal"] = subgoal
                if safe_rec:
                    safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")
                _safe_escalate_deadlock(
                    gatekeeper,
                    trajectory=state.get("trajectory", []),
                    triggering_tier=orig_tier,
                    integration_branch=state.get("integration_branch"),
                    thread_id=state.get("thread_id"),
                )
            _safe_record_memory(memory, state, outcome="duplicate_diff", failure_class="duplicate_diff",
                                summary=feedback_msg, diff=diff, subgoal=subgoal)
            return state

    # Phase 7: once retry metadata exists, a worker must change its evidence,
    # hypothesis, validation, strategy, failure remediation, or material diff.
    # Older states have no metadata and deliberately retain legacy behavior.
    retry_progress = _retry_progress_metadata(
        state, diff, failure_class=(state.get("failed_attempts") or [{}])[-1].get("failure_class", ""),
        failed_check=(state.get("failed_attempts") or [{}])[-1].get("failed_check"),
    )
    if retry_intel != "off" and _retry_is_dead_end(state, retry_progress):
        if replan_enabled and replan_planner is not None and replan_validator is not None:
            evidence = dict(retry_progress or {})
            evidence.update({"dead_end": True, "attempt_count": len(state.get("failed_attempts") or []) + 1,
                             "failure_class": evidence.get("failure_class") or "retry_dead_end",
                             "reason": "retry_dead_end"})
            try:
                replan_result = replan_remaining_queue(
                    state, evidence, replan_planner, validate_scope=replan_validator,
                    max_budget=state.get("replan_budget", 1),
                )
            except Exception as exc:
                replan_result = None
                logger.warning("advisory replanning failed: %s", exc)
            if replan_result is not None and replan_result.replanned:
                state["current_subgoal"] = None
                state["failed_attempts"] = []
                state["gate_status"] = "replanned"
                state["status"] = None
                state["trajectory"].append({"node": "gate", "gate_status": "replanned"})
                return state
        state["gate_status"] = "retry_dead_end"
        state["status"] = "escalated"
        state["last_feedback"] = (
            "Retry rejected as a dead-end: no new relevant evidence, changed hypothesis, "
            "material diff, validation result, or strategy was supplied."
        )
        _teardown_worktree(state, workspace)
        subgoal.status = "failed"
        state["current_subgoal"] = subgoal
        state["trajectory"].append({
            "node": "gate", "gate_status": "retry_dead_end", "retry_metadata": retry_progress,
            "tier0_result": None, "jev_request": None, "jev_verdict": None,
            "advisory": {"status": "not_applicable", "result": None},
        })
        _safe_escalate_deadlock(
            gatekeeper, trajectory=state.get("trajectory", []),
            triggering_tier="retry_policy", integration_branch=state.get("integration_branch"),
            thread_id=state.get("thread_id"),
        )
        _safe_record_memory(memory, state, outcome="retry_dead_end", failure_class="retry_dead_end",
                            summary=state.get("last_feedback", ""), diff=diff, subgoal=subgoal)
        return state

    # An empty implementation is a deterministic mechanical failure.  It is
    # not a semantic question: there is no candidate for the judge to assess.
    if not diff.strip():
        evidence = _attempt_evidence(
            state,
            failure_class="empty_diff",
            failed_check="empty_diff",
            diff=diff,
            detail="Implementation produced no diff.",
            subgoal=subgoal,
            workspace=workspace,
        )
        state["mechanical_strike_count"] = state.get("mechanical_strike_count", 0) + 1
        state["gate_status"] = "mechanical_failure"
        state["last_feedback"] = (
            "Implementation diff is empty; semantic validation was skipped.\n"
            f"Failure evidence: {_feedback_packet(evidence)}"
        )
        advisory_telemetry = _apply_advisory(advisory, evidence, state)
        state["last_feedback"] = _cap_self_check_output(
            f"{state.get('last_feedback') or ''}\nFailure evidence: {_feedback_packet(evidence)}"
        )
        if retry_intel != "off":
            _record_failed_attempt_in_state(
                state, "mechanical", "empty_diff", "empty_diff", diff,
                failure_evidence=evidence,
                retry_metadata=retry_progress,
            )
        _snapshot_attempt(
            safe_rec, state, subgoal_id, "mechanical", "empty_diff",
            failed_check="empty_diff", diff=diff,
            feedback_sent=state["last_feedback"], failure_evidence=evidence,
            retry_metadata=retry_progress,
        )
        _teardown_worktree(state, workspace)
        state["trajectory"].append({
            "node": "gate", "gate_status": "empty_diff", "advisory": advisory_telemetry,
            "failure_evidence": evidence, "jev_request": None, "jev_verdict": None,
            "retry_metadata": retry_progress,
        })
        if state["mechanical_strike_count"] >= 3:
            subgoal.status = "failed"
            state["current_subgoal"] = subgoal
            _safe_escalate_deadlock(
                gatekeeper, state.get("trajectory", []), "mechanical",
                integration_branch=state.get("integration_branch"),
                thread_id=state.get("thread_id"),
            )
        _safe_record_memory(memory, state, outcome="empty_diff", failure_class="empty_diff",
                            summary=state.get("last_feedback", ""), diff=diff, subgoal=subgoal)
        return state

    # Tier 0: Mechanical checks
    subgoal_base_commit = state.get("subgoal_base_commit")
    test_policy = state.get("test_policy", "auto")
    kwargs: Dict[str, Any] = {}
    if _accepts_param(workspace.run_mechanical_checks, "base_commit"):
        kwargs["base_commit"] = subgoal_base_commit
    if _accepts_param(workspace.run_mechanical_checks, "test_policy"):
        kwargs["test_policy"] = test_policy
    targets = _test_targets(workspace, diff, subgoal.scope)
    should_target = bool(targets and (test_policy == "always" or
                                      (test_policy == "auto" and subgoal.expects_tests)))
    targeted_run = False
    if should_target and _accepts_param(workspace.run_mechanical_checks, "test_targets"):
        kwargs["test_targets"] = targets
        mech_result = workspace.run_mechanical_checks(subgoal, **kwargs)
        targeted_run = True
    elif should_target and hasattr(workspace, "run_tests") and _accepts_param(workspace.run_tests, "targets"):
        # Keep the workspace's compile/scope gate intact while selecting its supported targeted runner.
        full_runner = workspace.run_tests
        workspace.run_tests = lambda: full_runner(targets=targets)
        try:
            mech_result = workspace.run_mechanical_checks(subgoal, **kwargs)
            targeted_run = True
        finally:
            workspace.run_tests = full_runner
    if not targeted_run:
        mech_result = workspace.run_mechanical_checks(subgoal, **kwargs)

    classification = getattr(mech_result, "test_classification", None)
    full_suite_clean = getattr(mech_result, "full_suite_clean", None)
    baseline_failures = getattr(mech_result, "baseline_failures", None)
    new_failures = getattr(mech_result, "new_failures", None)
    baseline_failing_ids = getattr(mech_result, "baseline_failing_ids", None)
    new_failing_ids = getattr(mech_result, "new_failing_ids", None)

    if classification is not None:
        state["test_classification"] = classification
    if full_suite_clean is not None:
        state["full_suite_clean"] = full_suite_clean
    if baseline_failures is not None:
        state["baseline_failures"] = baseline_failures
    if new_failures is not None:
        state["new_failures"] = new_failures
    if baseline_failing_ids is not None:
        state["baseline_failing_ids"] = baseline_failing_ids
    if new_failing_ids is not None:
        state["new_failing_ids"] = new_failing_ids

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
    if test_policy in ("never", "verify-only") or (test_policy == "auto" and not subgoal.expects_tests):
        tests_ran = False

    if mech_result.failed_check in ("build", "compile", "no_compile_command", "env_not_ready") or (not tests_ran and not mech_result.passed):
        test_runner_telemetry = {"ran": False}
    else:
        test_runner_telemetry = getattr(mech_result, "test_runner_outcome", None)
        if test_runner_telemetry is None and hasattr(workspace, "last_test_run"):
            test_runner_telemetry = workspace.last_test_run
        if test_runner_telemetry is None:
            test_runner_telemetry = {
                "ran": tests_ran,
                "ecosystem": None,
                "command": None,
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "output_tail": "",
                "outcome": "PASSED" if mech_result.passed or mech_result.failed_check != "tests" else ("NO_TESTS_COLLECTED" if not tests_ran else "FAILED"),
            }
        elif isinstance(test_runner_telemetry, dict):
            test_runner_telemetry = dict(test_runner_telemetry)
            if "ran" not in test_runner_telemetry:
                test_runner_telemetry["ran"] = tests_ran

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

    if mech_result.failed_check in ("build", "compile", "no_compile_command", "env_not_ready") and not tests_ran:
        test_runner_telemetry = {"ran": False}
    else:
        test_runner_telemetry = _test_evidence(
            test_policy, tests_ran, test_runner_telemetry, targets if targeted_run else [],
            required=bool(subgoal.expects_tests),
        )
    mechanical_evidence = _attempt_evidence(
        state,
        failure_class=mech_result.failed_check or "mechanical_failure",
        failed_check=mech_result.failed_check,
        diff=diff,
        detail=mech_result.detail,
        compile_info=compile_telemetry,
        test_info=test_runner_telemetry,
        subgoal=subgoal,
        tests_ran=tests_ran,
        workspace=workspace,
    )

    mech_ev = getattr(mech_result, "mech_evidence", None)
    if (mech_result.failed_check == "test_surface_changed" or getattr(mech_result, "test_surface_changed", False)) and mech_ev is None:
        mech_ev = MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.TEST_SURFACE_CHANGED,
            test_surface_changed=True,
        )
    is_unmeasured = (
        mech_result.failed_check == "tests_unmeasured"
        or (
            mech_ev is not None
            and getattr(mech_ev, "mech_status", None) == MechStatus.UNMEASURED
            and mech_result.failed_check not in ("env_not_ready", "no_compile_command")
        )
    )

    if not mech_result.passed:
        if mech_result.failed_check in ("no_compile_command", "env_not_ready"):
            state["status"] = "escalated"
            state["gate_status"] = mech_result.failed_check
            advisory_telemetry = _apply_advisory(advisory, mechanical_evidence, state)
            state["last_feedback"] = _cap_self_check_output(
                (
                f"{mech_result.detail}\nFailure evidence: "
                f"{_feedback_packet(mechanical_evidence)}"
                )
            )

            if retry_intel != "off":
                _record_failed_attempt_in_state(
                    state,
                    tier="mechanical",
                    outcome=mech_result.failed_check,
                    failed_check=mech_result.failed_check,
                    diff=diff,
                    compile_info=compile_telemetry,
                    failure_evidence=mechanical_evidence,
                    retry_metadata=retry_progress,
                )
            _snapshot_attempt(
                safe_rec,
                state,
                subgoal_id,
                tier="mechanical",
                outcome=mech_result.failed_check,
                failed_check=mech_result.failed_check,
                diff=diff,
                compile_info=compile_telemetry,
                tests_ran=tests_ran,
                test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
                feedback_sent=state["last_feedback"],
                failure_evidence=mechanical_evidence,
                retry_metadata=retry_progress,
                mech_evidence=mech_ev,
            )
            _teardown_worktree(state, workspace)
            subgoal.status = "failed"
            state["current_subgoal"] = subgoal
            if safe_rec:
                safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")

            state["trajectory"].append({
                "node": "gate",
                "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
                "gate_status": mech_result.failed_check,
                "tier0_result": tier0_telemetry,
                "compile": compile_telemetry,
                "test_runner": test_runner_telemetry,
                "failure_evidence": mechanical_evidence,
                "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
                "retry_metadata": retry_progress,
                "jev_request": None,
                "jev_verdict": None,
                "advisory": advisory_telemetry,
            })

            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
                integration_branch=state.get("integration_branch"),
                thread_id=state.get("thread_id"),
            )
            _safe_record_memory(memory, state, outcome=mech_result.failed_check,
                                failure_class=mech_result.failed_check, summary=state.get("last_feedback", ""),
                                diff=diff, subgoal=subgoal)
            return state

        elif is_unmeasured:
            current_unmeasured_retries = state.get("unmeasured_retry_count", 0) + 1
            state["unmeasured_retry_count"] = current_unmeasured_retries

            reason_val = "unmeasured"
            if mech_ev and getattr(mech_ev, "reason", None):
                reason_val = mech_ev.reason.value if hasattr(mech_ev.reason, "value") else str(mech_ev.reason)

            base_fb = mech_result.detail or "Unit tests could not be measured."
            output_tail = None
            if isinstance(test_runner_telemetry, dict):
                output_tail = test_runner_telemetry.get("output_tail")
            elif hasattr(mech_result, "test_runner_outcome") and isinstance(mech_result.test_runner_outcome, dict):
                output_tail = mech_result.test_runner_outcome.get("output_tail")
            if output_tail:
                tail_capped = output_tail[-4000:]
                if tail_capped not in base_fb:
                    formatted_fb = f"{base_fb}\n\n{tail_capped}"
                else:
                    formatted_fb = base_fb
            else:
                formatted_fb = base_fb

            state["last_feedback"] = f"Verification could not attribute this failure to your change (reason: {reason_val}). Fix it if it is yours.\n\n{formatted_fb}"
            advisory_telemetry = _apply_advisory(advisory, mechanical_evidence, state)
            _teardown_worktree(state, workspace)

            if current_unmeasured_retries >= 3:
                state["status"] = "escalated"
                state["gate_status"] = "verification_inconclusive"
                subgoal.status = "failed"
                state["current_subgoal"] = subgoal
                if safe_rec:
                    safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")

                _snapshot_attempt(
                    safe_rec,
                    state,
                    subgoal_id,
                    tier="mechanical",
                    outcome="verification_inconclusive",
                    failed_check="tests_unmeasured",
                    diff=diff,
                    compile_info=compile_telemetry,
                    tests_ran=tests_ran,
                    test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
                    feedback_sent=state["last_feedback"],
                    failure_evidence=mechanical_evidence,
                    mech_evidence=mech_ev,
                )

                state["trajectory"].append({
                    "node": "gate",
                    "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
                    "gate_status": "verification_inconclusive",
                    "status": "escalated",
                    "tier0_result": tier0_telemetry,
                    "compile": compile_telemetry,
                    "test_runner": test_runner_telemetry,
                    "failure_evidence": mechanical_evidence,
                    "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
                    "retry_metadata": retry_progress,
                    "jev_request": None,
                    "jev_verdict": None,
                    "advisory": advisory_telemetry,
                })

                _safe_escalate_deadlock(
                    gatekeeper,
                    trajectory=state.get("trajectory", []),
                    triggering_tier="verification",
                    integration_branch=state.get("integration_branch"),
                    thread_id=state.get("thread_id"),
                )
                _safe_record_memory(memory, state, outcome="verification_inconclusive",
                                    failure_class="tests_unmeasured", summary=state.get("last_feedback", ""),
                                    diff=diff, subgoal=subgoal)
                return state
            else:
                state["gate_status"] = "unmeasured"
                _snapshot_attempt(
                    safe_rec,
                    state,
                    subgoal_id,
                    tier="mechanical",
                    outcome="unmeasured",
                    failed_check="tests_unmeasured",
                    diff=diff,
                    compile_info=compile_telemetry,
                    tests_ran=tests_ran,
                    test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
                    feedback_sent=state["last_feedback"],
                    failure_evidence=mechanical_evidence,
                    mech_evidence=mech_ev,
                )

                state["trajectory"].append({
                    "node": "gate",
                    "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
                    "gate_status": "unmeasured",
                    "tier0_result": tier0_telemetry,
                    "compile": compile_telemetry,
                    "test_runner": test_runner_telemetry,
                    "failure_evidence": mechanical_evidence,
                    "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
                    "retry_metadata": retry_progress,
                    "jev_request": None,
                    "jev_verdict": None,
                    "advisory": advisory_telemetry,
                })
                _safe_record_memory(memory, state, outcome="unmeasured",
                                    failure_class="tests_unmeasured", summary=state.get("last_feedback", ""),
                                    diff=diff, subgoal=subgoal)
                return state

        current_mech_strikes = state.get("mechanical_strike_count", 0) + 1
        state["mechanical_strike_count"] = current_mech_strikes
        state["gate_status"] = "mechanical_failure"
        if mech_result.failed_check == "compile":
            state["last_feedback"] = _format_compile_feedback(
                compile_telemetry or getattr(mech_result, "compile_outcome", None) or {},
                fallback_detail=mech_result.detail,
            )
        elif mech_result.failed_check == "tests":
            base_fb = mech_result.detail or "Unit tests failed."
            output_tail = None
            if isinstance(test_runner_telemetry, dict):
                output_tail = test_runner_telemetry.get("output_tail")
            elif hasattr(mech_result, "test_runner_outcome") and isinstance(mech_result.test_runner_outcome, dict):
                output_tail = mech_result.test_runner_outcome.get("output_tail")
            if output_tail:
                tail_capped = output_tail[-4000:]
                if tail_capped not in base_fb:
                    state["last_feedback"] = f"{base_fb}\n\n{tail_capped}"
                else:
                    state["last_feedback"] = base_fb
            else:
                state["last_feedback"] = base_fb
        else:
            state["last_feedback"] = mech_result.detail

        mechanical_feedback = (
            f"{state.get('last_feedback') or 'Mechanical validation failed.'}\n"
            f"Failure evidence: {_feedback_packet(mechanical_evidence)}"
        )
        advisory_telemetry = _apply_advisory(advisory, mechanical_evidence, state)
        mechanical_feedback = (
            f"{state.get('last_feedback') or 'Mechanical validation failed.'}\n"
            f"Failure evidence: {_feedback_packet(mechanical_evidence)}"
        )
        # Preserve a complete test tail when it already consumes the feedback budget.
        if len(mechanical_feedback) <= MAX_SELF_CHECK_OUTPUT_CHARS:
            state["last_feedback"] = mechanical_feedback

        if retry_intel != "off":
            _record_failed_attempt_in_state(
                state,
                tier="mechanical",
                outcome="mechanical_failure",
                failed_check=mech_result.failed_check,
                diff=diff,
                compile_info=compile_telemetry,
                failure_evidence=mechanical_evidence,
            )
        _snapshot_attempt(
            safe_rec,
            state,
            subgoal_id,
            tier="mechanical",
            outcome="mechanical_failure",
            failed_check=mech_result.failed_check,
            diff=diff,
            compile_info=compile_telemetry,
            tests_ran=tests_ran,
            test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
            feedback_sent=state.get("last_feedback"),
            failure_evidence=mechanical_evidence,
            mech_evidence=mech_ev,
        )
        _teardown_worktree(state, workspace)

        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "mechanical_failure",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "failure_evidence": mechanical_evidence,
            "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
            "retry_metadata": retry_progress,
            "jev_request": None,
            "jev_verdict": None,
            "advisory": advisory_telemetry,
        })

        if current_mech_strikes >= 3:
            subgoal.status = "failed"
            state["current_subgoal"] = subgoal
            if safe_rec:
                safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
                integration_branch=state.get("integration_branch"),
                thread_id=state.get("thread_id"),
            )
        _safe_record_memory(memory, state, outcome="mechanical_failure",
                            failure_class=mech_result.failed_check, summary=state.get("last_feedback", ""),
                            diff=diff, subgoal=subgoal)
        return state

    # Tier 0 Passed -> Proceed to Tier 1 (Semantic check via Gatekeeper)
    if gatekeeper is None:
        raise ValueError("Gatekeeper must be provided for Tier 1 validation.")

    ev_level = evidence_level(
        compile_outcome=compile_telemetry,
        test_outcome=test_runner_telemetry,
        test_policy=state.get("test_policy"),
    )

    attempt_dep_delta_obj = None
    attempt_dep_delta = None
    if workspace is not None and hasattr(workspace, "get_manifest_changes"):
        try:
            changes = workspace.get_manifest_changes("HEAD")
            delta = compute_dependency_delta(changes)
            attempt_dep_delta_obj = delta
            attempt_dep_delta = delta.to_json()
        except Exception as e:
            logger.warning(f"{type(e).__name__}: {e}")
            attempt_dep_delta_obj = None
            attempt_dep_delta = None

    inv_notes = state.get("investigation_notes")

    dep_delta_flag = state.get("dep_delta_jev", "off")
    pass_delta = False
    if dep_delta_flag == "on" and attempt_dep_delta_obj is not None:
        if (not attempt_dep_delta_obj.is_empty) or bool(attempt_dep_delta_obj.unparsed):
            pass_delta = True

    gate_kwargs: Dict[str, Any] = {}
    if _accepts_param(gatekeeper.validate_subgoal, "investigation_notes"):
        gate_kwargs["investigation_notes"] = inv_notes
    if pass_delta and _accepts_param(gatekeeper.validate_subgoal, "dependency_delta"):
        gate_kwargs["dependency_delta"] = attempt_dep_delta_obj.model_dump()
    if _accepts_param(gatekeeper.validate_subgoal, "test_evidence"):
        gate_kwargs["test_evidence"] = test_runner_telemetry

    if isinstance(test_runner_telemetry, dict):
        if classification:
            test_runner_telemetry["classification"] = classification
        if baseline_failures is not None and "baseline_failures" not in test_runner_telemetry:
            test_runner_telemetry["baseline_failures"] = baseline_failures
        if new_failures is not None and "new_failures" not in test_runner_telemetry:
            test_runner_telemetry["new_failures"] = new_failures
        if full_suite_clean is not None:
            test_runner_telemetry["full_suite_clean"] = full_suite_clean

    if test_policy == "never":
        mechanical_detail = "tests_excluded_by_policy=true"
    elif mech_ev is not None:
        m_status = mech_ev.mech_status.value if hasattr(mech_ev.mech_status, "value") else str(mech_ev.mech_status)
        m_ev_level = mech_ev.evidence_level.value if hasattr(mech_ev.evidence_level, "value") else str(mech_ev.evidence_level)
        m_unverified = mech_ev.mechanically_unverified
        m_comp = mech_ev.comparison.value if hasattr(mech_ev.comparison, "value") else (str(mech_ev.comparison) if mech_ev.comparison else "none")
        m_base = mech_ev.base_status.value if hasattr(mech_ev.base_status, "value") else (str(mech_ev.base_status) if mech_ev.base_status else "none")
        m_target_mode = "targeted" if targeted_run else "untargeted"
        m_tests_ran = tests_ran

        standard_block = (
            f"Mechanical Evidence:\n"
            f"- mechanical_status: {m_status}\n"
            f"- evidence_level: {m_ev_level}\n"
            f"- mechanically_unverified: {m_unverified}\n"
            f"- comparison/base_status: {m_comp}/{m_base}\n"
            f"- target_mode: {m_target_mode}\n"
            f"- tests_ran: {m_tests_ran}"
        )
        if m_ev_level == "exit_only":
            standard_block += "\n- note: not a full-suite verification"

        if classification == "REGRESSION_FREE_WITH_BASELINE_FAILURES":
            base_desc = f"Regression-free with baseline failures (baseline_failures={baseline_failures}, new_failures=0)."
            mechanical_detail = f"{standard_block}\n{base_desc}\n{mech_result.detail}".strip()
        elif m_ev_level == "exit_only":
            detail_text = mech_result.detail or ""
            mechanical_detail = f"{standard_block}\n{detail_text}".strip()
        elif tests_ran or subgoal.expects_tests or test_policy == "verify-only":
            mechanical_detail = (
                f"{standard_block}\n{mech_result.detail}\nTest evidence: "
                f"{json.dumps(test_runner_telemetry, sort_keys=True)}"
            ).strip()
        else:
            mechanical_detail = f"{standard_block}\n{mech_result.detail}".strip()
    elif classification == "REGRESSION_FREE_WITH_BASELINE_FAILURES":
        base_desc = f"Regression-free with baseline failures (baseline_failures={baseline_failures}, new_failures=0)."
        mechanical_detail = (
            f"{base_desc}\n{mech_result.detail}\nTest evidence: "
            f"{json.dumps(test_runner_telemetry, sort_keys=True)}"
        ).strip()
    elif tests_ran or subgoal.expects_tests or test_policy == "verify-only":
        mechanical_detail = (
            f"{mech_result.detail}\nTest evidence: "
            f"{json.dumps(test_runner_telemetry, sort_keys=True)}"
        ).strip()
    else:
        mechanical_detail = mech_result.detail
    verdict = gatekeeper.validate_subgoal(
        subgoal, diff, mechanical_detail=mechanical_detail, **gate_kwargs
    )
    jev_keys = getattr(gatekeeper, "last_state_keys", None)
    if not isinstance(jev_keys, str):
        jev_keys = None

    jev_request_telemetry = {
        "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
        "diff_size": len(diff),
        "investigation_notes_included": bool(inv_notes),
    }
    jev_verdict_telemetry = {
        "valid": verdict.valid,
        "probability": getattr(verdict, "probability", 1.0),
        "reason": getattr(verdict, "reason", None),
        "feedback": normalize_jev_feedback(verdict).model_dump(),
    }
    semantic_evidence = _attempt_evidence(
        state,
        failure_class="semantic_rejection",
        failed_check="jev_rejection",
        diff=diff,
        detail=getattr(verdict, "reason", None) or "Gatekeeper rejected the candidate.",
        test_info=test_runner_telemetry,
        subgoal=subgoal,
        tests_ran=tests_ran,
        workspace=workspace,
    )
    semantic_evidence["known_facts"].extend([
        f"semantic validity probability: {getattr(verdict, 'probability', None)}",
        f"declared scope versus touched files: {list(subgoal.scope)} versus {semantic_evidence['files']}",
        f"evidence level: {ev_level}",
    ])
    semantic_evidence["unknowns"] = ["the judge did not provide a deterministic repair location"]
    semantic_evidence["jev_feedback"] = normalize_jev_feedback(verdict).model_dump()
    advisory_telemetry = _apply_advisory(advisory, semantic_evidence, state) if not verdict.valid else {
        "status": "not_applicable", "result": None
    }

    if verdict.valid:
        wt_path = state.get("current_worktree_path")
        wt_branch = state.get("current_worktree_branch")
        is_worktree_active = _is_active_worktree(state, workspace)

        parent_sha: Optional[str] = None
        commit_sha: Optional[str] = None

        if wt_path and hasattr(workspace, "merge_subgoal_worktree"):
            target_branch = state.get("integration_branch")
            if not target_branch:
                state["gate_status"] = "merge_failed"
                state["status"] = "escalated"
                err_msg = "Worktree merge failed: integration_branch is missing from state"
                state["last_feedback"] = err_msg
                _snapshot_attempt(
                    safe_rec, state, subgoal_id, tier="merge", outcome="merge_failed",
                    diff=diff, feedback_sent=err_msg, evidence_level=ev_level,
                    dependency_delta=attempt_dep_delta, jev_state_keys=jev_keys,
                )
                _teardown_worktree(state, workspace)
                subgoal.status = "failed"
                state["current_subgoal"] = subgoal
                if safe_rec:
                    safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")
                state["trajectory"].append({
                    "node": "gate",
                    "error_type": "missing_integration_branch",
                    "error": err_msg,
                    "gate_status": "merge_failed",
                })
                _safe_escalate_deadlock(
                    gatekeeper,
                    trajectory=state.get("trajectory", []),
                    triggering_tier="merge",
                    thread_id=state.get("thread_id"),
                )
                return state
            if hasattr(workspace, "get_branch_commit"):
                try:
                    parent_sha = workspace.get_branch_commit(target_branch)
                except Exception:
                    parent_sha = None
            try:
                workspace.merge_subgoal_worktree(
                    wt_path,
                    wt_branch,
                    target_branch=target_branch,
                )
                if hasattr(workspace, "get_branch_commit"):
                    try:
                        commit_sha = workspace.get_branch_commit(target_branch)
                    except Exception:
                        commit_sha = None
            except Exception as e:
                state["gate_status"] = "merge_failed"
                state["status"] = "escalated"
                state["last_feedback"] = f"Worktree merge failed: {e}"
                _snapshot_attempt(
                    safe_rec, state, subgoal_id, tier="merge", outcome="merge_failed",
                    diff=diff, feedback_sent=str(e), evidence_level=ev_level,
                    dependency_delta=attempt_dep_delta, jev_state_keys=jev_keys,
                )
                _teardown_worktree(state, workspace)
                subgoal.status = "failed"
                state["current_subgoal"] = subgoal
                if safe_rec:
                    safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")
                state["trajectory"].append({
                    "node": "gate",
                    "error_type": "merge_failure",
                    "error": str(e),
                    "gate_status": "merge_failed",
                })
                _safe_escalate_deadlock(
                    gatekeeper,
                    trajectory=state.get("trajectory", []),
                    triggering_tier="merge",
                    integration_branch=target_branch,
                    thread_id=state.get("thread_id"),
                )
                return state
        elif is_worktree_active:
            err_msg = "Worktree was created but worktree state was missing or invalid at gate time."
            _snapshot_attempt(
                safe_rec, state, subgoal_id, tier="worktree", outcome="worktree_state_missing",
                diff=diff, feedback_sent=err_msg, evidence_level=ev_level,
                dependency_delta=attempt_dep_delta, jev_state_keys=jev_keys,
            )
            _teardown_worktree(state, workspace)
            subgoal.status = "failed"
            state["current_subgoal"] = subgoal
            if safe_rec:
                safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")
            state["gate_status"] = "merge_failed"
            state["status"] = "escalated"
            state["last_feedback"] = err_msg
            state["trajectory"].append({
                "node": "gate",
                "error_type": "worktree_state_missing",
                "error": err_msg,
                "gate_status": "merge_failed",
            })
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="worktree",
                integration_branch=state.get("integration_branch"),
                thread_id=state.get("thread_id"),
            )
            return state
        elif hasattr(workspace, "commit_subgoal"):
            if hasattr(workspace, "base_commit"):
                parent_sha = workspace.base_commit
            elif hasattr(workspace, "_get_head_commit"):
                parent_sha = workspace._get_head_commit()
            workspace.commit_subgoal()
            if hasattr(workspace, "_get_head_commit"):
                commit_sha = workspace._get_head_commit()

        state["current_worktree_path"] = None
        state["current_worktree_branch"] = None
        state["mechanical_strike_count"] = 0
        state["semantic_strike_count"] = 0
        state["unmeasured_retry_count"] = 0
        if "failed_attempts" in state or retry_intel != "off":
            state["failed_attempts"] = []
        state["gate_status"] = "passed"
        state["last_feedback"] = ""
        subgoal.status = "committed"
        state["current_subgoal"] = subgoal
        state["committed_subgoals"] = list(state.get("committed_subgoals", [])) + [subgoal.model_copy(deep=True)]

        is_unv = False
        unv_kind = None
        if not tests_ran or not getattr(subgoal, "expects_tests", True) or test_policy in ("never", "verify-only"):
            is_unv = True
            unv_kind = "untested"
        elif (
            (mech_ev is not None and (
                getattr(mech_ev, "mechanically_unverified", False)
                or getattr(mech_ev, "evidence_level", None) in (EvidenceLevel.EXIT_ONLY, EvidenceLevel.NONE, "exit_only", "none")
            ))
            or ev_level in ("exit_only", "none")
        ):
            is_unv = True
            unv_kind = "exit_only"
        elif mech_ev is not None and getattr(mech_ev, "mechanically_unverified", False):
            is_unv = True
            unv_kind = getattr(mech_ev, "unverified_kind", None) or "untested"

        if is_unv and mech_ev is not None:
            mech_ev.mechanically_unverified = True
            if not getattr(mech_ev, "unverified_kind", None):
                mech_ev.unverified_kind = unv_kind

        if is_unv and getattr(subgoal, "id", None):
            curr_unv = list(state.get("mechanically_unverified_subgoals") or [])
            if subgoal.id not in curr_unv:
                curr_unv.append(subgoal.id)
            state["mechanically_unverified_subgoals"] = curr_unv
            unv_kinds = dict(state.get("mechanically_unverified_kinds") or {})
            unv_kinds[subgoal.id] = unv_kind
            state["mechanically_unverified_kinds"] = unv_kinds

        _snapshot_attempt(
            safe_rec,
            state,
            subgoal_id,
            tier="semantic",
            outcome="passed",
            diff=diff,
            verdict=verdict,
            tests_ran=tests_ran,
            test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
            feedback_sent="",
            evidence_level=ev_level,
            dependency_delta=attempt_dep_delta,
            jev_state_keys=jev_keys,
            mech_evidence=mech_ev,
            unverified_kind=unv_kind,
        )
        if safe_rec:
            safe_rec.record_subgoal_committed(run_id, subgoal_id, parent_sha=parent_sha, commit_sha=commit_sha)

        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "passed",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
            "mechanically_unverified": is_unv,
            "unverified_kind": unv_kind,
            "jev_request": jev_request_telemetry,
            "jev_verdict": jev_verdict_telemetry,
            "advisory": advisory_telemetry,
        })
        _safe_record_memory(memory, state, outcome="passed", summary="Subgoal passed Tier 0 and Tier 1 gates.",
                            diff=diff, subgoal=subgoal)
        return state
    else:
        current_sem_strikes = state.get("semantic_strike_count", 0) + 1
        state["semantic_strike_count"] = current_sem_strikes
        state["gate_status"] = "semantic_failure"

        has_extra_answers = (
            getattr(verdict, "kind", None) is not None
            or any(
                getattr(verdict, attr, None) is not None
                for attr in ("incomplete", "wrong_behavior", "conflicts_with_notes")
            )
        )
        if retry_intel != "off" and has_extra_answers:
            state["last_feedback"] = _format_typed_semantic_feedback(
                verdict, subgoal, diff, tests_ran
            )
        else:
            prob_str = f" (confidence: {verdict.probability:.2f})" if hasattr(verdict, "probability") and isinstance(verdict.probability, (int, float)) else ""
            state["last_feedback"] = (
                verdict.reason
                or f"Semantic validation rejected by Gatekeeper{prob_str}. "
                   "Diff contradicted project conventions, exceeded declared scope, or failed to implement the required subgoal. "
                   "Ensure changes match the project architecture, dependencies, and styling in Investigation Notes. "
                "Ensure changes strictly adhere to the application's actual domain, product identity, and branding documented in Investigation Notes, without introducing conflicting domain concepts, alternate sports/business models, or fabricated external entities."
            )

        advisory_feedback = advisory_telemetry.get("feedback", "")
        state["last_feedback"] = _cap_self_check_output(
            (
            f"{advisory_feedback}\n"
            f"{state['last_feedback']}\nFailure evidence: "
            f"{_feedback_packet(semantic_evidence)}"
            )
        )

        if retry_intel != "off":
            _record_failed_attempt_in_state(
                state,
                tier="semantic",
                outcome="semantic_failure",
                failed_check="jev_rejection",
                diff=diff,
                failure_evidence=semantic_evidence,
                retry_metadata=retry_progress,
            )
        _snapshot_attempt(
            safe_rec,
            state,
            subgoal_id,
            tier="semantic",
            outcome="semantic_failure",
            diff=diff,
            verdict=verdict,
            tests_ran=tests_ran,
            test_outcome=test_runner_telemetry.get("outcome") if isinstance(test_runner_telemetry, dict) else None,
            feedback_sent=state.get("last_feedback"),
            evidence_level=ev_level,
            dependency_delta=attempt_dep_delta,
            jev_state_keys=jev_keys,
            failure_evidence=semantic_evidence,
            retry_metadata=retry_progress,
            mech_evidence=mech_ev,
        )
        _teardown_worktree(state, workspace)

        if current_sem_strikes >= 3:
            subgoal.status = "failed"
            state["current_subgoal"] = subgoal
            if safe_rec:
                safe_rec.record_subgoal_status(run_id, subgoal_id, "failed")

        state["trajectory"].append({
            "node": "gate",
            "subgoal": subgoal.model_dump() if hasattr(subgoal, "model_dump") else dict(subgoal),
            "gate_status": "semantic_failure",
            "tier0_result": tier0_telemetry,
            "compile": compile_telemetry,
            "test_runner": test_runner_telemetry,
            "mech_evidence": mech_ev.model_dump() if mech_ev and hasattr(mech_ev, "model_dump") else None,
            "jev_request": jev_request_telemetry,
            "jev_verdict": jev_verdict_telemetry,
            "failure_evidence": semantic_evidence,
            "retry_metadata": retry_progress,
            "advisory": advisory_telemetry,
        })

        if current_sem_strikes >= 3:
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="semantic",
                integration_branch=state.get("integration_branch"),
                thread_id=state.get("thread_id"),
            )

        _safe_record_memory(memory, state, outcome="semantic_failure", failure_class="semantic_rejection",
                            summary=state.get("last_feedback", ""), diff=diff, subgoal=subgoal)

        return state


def node_verify(
    state: State,
    workspace: Optional[Any] = None,
    gatekeeper: Optional[Any] = None,
    recorder: Optional[Any] = None,
) -> State:
    """State 4: VERIFICATION (Executes full test suite and final ticket verification on a dedicated verify worktree)."""
    safe_rec = SafeRecorder(recorder) if recorder and not isinstance(recorder, SafeRecorder) else recorder
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
            _snapshot_attempt(
                safe_rec,
                state,
                subgoal_id="__verify__",
                tier="mechanical",
                outcome="mechanical_failure" if compile_info.get("outcome") == "FAILED" else compile_info.get("outcome", "").lower(),
                failed_check=compile_info.get("outcome"),
                diff=final_diff,
                compile_info=compile_info,
                tests_ran=False,
                feedback_sent=state.get("last_feedback"),
            )
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
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
                integration_branch=integration_branch,
                thread_id=state.get("thread_id"),
            )
            return state

        # 2. Run unit tests
        test_output = ""
        tests_ran = False
        outcome = None
        test_policy = state.get("test_policy", "auto")
        targets = _test_targets(workspace, final_diff)
        if test_policy == "never":
            test_output = "tests_excluded_by_policy=true (test_policy=never)"
        elif targets and workspace is not None and hasattr(workspace, "run_targeted_tests"):
            outcome = workspace.run_targeted_tests(targets)
            tests_ran = True
            test_output = getattr(outcome, "value", str(outcome))
        elif targets and workspace is not None and hasattr(workspace, "run_tests") and _accepts_param(workspace.run_tests, "targets"):
            outcome = workspace.run_tests(targets=targets)
            tests_ran = True
            test_output = getattr(outcome, "value", str(outcome))
        elif workspace is not None and hasattr(workspace, "run_tests"):
            outcome = workspace.run_tests()
            tests_ran = True
            test_output = getattr(outcome, "value", str(outcome))

        if outcome == TestOutcome.ENV_NOT_READY or test_output == "ENV_NOT_READY":
            test_runner_info = getattr(workspace, "last_test_run", None)
            err_detail = (test_runner_info or {}).get("stderr_tail") or "Test runner executable not found."
            state["gate_status"] = "verification_failed"
            state["status"] = "escalated"
            state["last_feedback"] = f"Final test check failed: Environment not ready: {err_detail}"
            _snapshot_attempt(
                safe_rec,
                state,
                subgoal_id="__verify__",
                tier="mechanical",
                outcome="env_not_ready",
                failed_check="env_not_ready",
                diff=final_diff,
                compile_info=compile_info,
                tests_ran=tests_ran,
                test_outcome="ENV_NOT_READY",
                feedback_sent=state.get("last_feedback"),
            )
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
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="mechanical",
                integration_branch=integration_branch,
                thread_id=state.get("thread_id"),
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
            test_runner_info = {
                "ran": False,
            }
        else:
            test_runner_info = getattr(workspace, "last_test_run", None)
            if not isinstance(test_runner_info, dict):
                test_runner_info = {
                    "ran": True,
                    "ecosystem": None,
                    "command": None,
                    "exit_code": None,
                    "stdout_tail": "",
                    "stderr_tail": "",
                    "output_tail": "",
                    "outcome": getattr(outcome, "value", str(outcome)) if 'outcome' in locals() else "UNKNOWN",
                }
            else:
                test_runner_info = dict(test_runner_info)
                test_runner_info["ran"] = True
                test_runner_info.setdefault("outcome", getattr(outcome, "value", str(outcome)))

        test_runner_info = _test_evidence(
            test_policy, tests_ran, test_runner_info,
            targets if tests_ran and (hasattr(workspace, "run_targeted_tests") or
                                      (hasattr(workspace, "run_tests") and _accepts_param(workspace.run_tests, "targets"))) else [],
            required=any_expects_tests,
        )

        verify_classification = None
        verify_baseline_failures = 0
        verify_new_failures = 0
        verify_full_suite_clean = None

        is_untargeted = not bool(targets)
        verify_ev_level = evidence_level(
            compile_outcome=compile_info if 'compile_info' in locals() else None,
            test_outcome=test_runner_info,
            test_policy=state.get("test_policy"),
        )
        has_counted_or_named = verify_ev_level in ("counted", "named", EvidenceLevel.COUNTED, EvidenceLevel.NAMED)

        if outcome in (TestOutcome.PASSED, "PASSED"):
            if is_untargeted and has_counted_or_named:
                verify_classification = "FULL_SUITE_PASSED"
                verify_full_suite_clean = True
                verify_baseline_failures = 0
                verify_new_failures = 0
            else:
                verify_classification = "PASSED_EXIT_ONLY"
                verify_full_suite_clean = None
                verify_baseline_failures = 0
                verify_new_failures = 0
        elif outcome in (TestOutcome.FAILED, "FAILED"):
            verify_classification = "FAILED"
            verify_full_suite_clean = False
            verify_new_failures = 1
        elif test_policy == "never":
            verify_classification = "EXEMPT_BY_POLICY"
            verify_full_suite_clean = None

        if verify_classification is not None:
            state["test_classification"] = verify_classification
            state["baseline_failures"] = verify_baseline_failures
            state["new_failures"] = verify_new_failures
            state["full_suite_clean"] = verify_full_suite_clean
            if isinstance(test_runner_info, dict):
                test_runner_info["classification"] = verify_classification
                if "baseline_failures" not in test_runner_info:
                    test_runner_info["baseline_failures"] = verify_baseline_failures
                if "new_failures" not in test_runner_info:
                    test_runner_info["new_failures"] = verify_new_failures
                test_runner_info["full_suite_clean"] = verify_full_suite_clean

        unverified_subgoals = list(state.get("mechanically_unverified_subgoals") or [])
        for entry in state.get("trajectory", []):
            if isinstance(entry, dict) and entry.get("node") == "gate" and entry.get("gate_status") == "passed":
                sg = entry.get("subgoal") or {}
                sg_id = sg.get("id") if isinstance(sg, dict) else getattr(sg, "id", None)
                m_ev = entry.get("mech_evidence") or {}
                if (
                    entry.get("mechanically_unverified")
                    or entry.get("unverified_kind")
                    or (isinstance(m_ev, dict) and m_ev.get("mechanically_unverified"))
                ):
                    if sg_id and sg_id not in unverified_subgoals:
                        unverified_subgoals.append(sg_id)
        state["mechanically_unverified_subgoals"] = unverified_subgoals

        if outcome == TestOutcome.FAILED or test_output == "FAILED":
            output_tail = None
            if isinstance(test_runner_info, dict):
                output_tail = test_runner_info.get("output_tail")
            elif hasattr(workspace, "last_test_run") and isinstance(workspace.last_test_run, dict):
                output_tail = workspace.last_test_run.get("output_tail")
            if output_tail:
                tail_capped = output_tail[-4000:]
                test_output = f"FAILED\n{tail_capped}"

        if test_policy != "never":
            test_output = f"{test_output}\nTest evidence: {json.dumps(test_runner_info, sort_keys=True)}".strip()

        verdict = None
        if gatekeeper is not None and hasattr(gatekeeper, "verify_ticket"):
            verify_kwargs: Dict[str, Any] = {}
            if _accepts_param(gatekeeper.verify_ticket, "investigation_notes"):
                verify_kwargs["investigation_notes"] = inv_notes
            if _accepts_param(gatekeeper.verify_ticket, "test_evidence"):
                verify_kwargs["test_evidence"] = test_runner_info
            verdict = gatekeeper.verify_ticket(ticket, final_diff, test_output, **verify_kwargs)

            verify_keys = getattr(gatekeeper, "last_state_keys", None)
            if not isinstance(verify_keys, str):
                verify_keys = None

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
                verify_ev_level = evidence_level(
                    compile_outcome=compile_info,
                    test_outcome=test_runner_info,
                    test_policy=state.get("test_policy"),
                )
                _snapshot_attempt(
                    safe_rec,
                    state,
                    subgoal_id="__verify__",
                    tier="semantic",
                    outcome="passed",
                    diff=final_diff,
                    compile_info=compile_info,
                    tests_ran=tests_ran,
                    test_outcome=test_runner_info.get("outcome") if isinstance(test_runner_info, dict) else test_output,
                    verdict=verdict,
                    feedback_sent="",
                    evidence_level=verify_ev_level,
                    jev_state_keys=verify_keys,
                )
            else:
                state["gate_status"] = "verification_failed"
                state["status"] = "verification_failed"
                prob_str = f" (confidence: {verdict.probability:.2f})" if hasattr(verdict, "probability") and isinstance(verdict.probability, (int, float)) else ""
                base_fb = verdict.reason or f"Verification rejected by Gatekeeper{prob_str}."
                if outcome == TestOutcome.FAILED or "FAILED" in str(test_output):
                    output_tail = None
                    if isinstance(test_runner_info, dict):
                        output_tail = test_runner_info.get("output_tail")
                    elif hasattr(workspace, "last_test_run") and isinstance(workspace.last_test_run, dict):
                        output_tail = workspace.last_test_run.get("output_tail")
                    if output_tail:
                        tail_capped = output_tail[-4000:]
                        if tail_capped not in base_fb:
                            base_fb = f"{base_fb}\n\nUnit tests failed:\n{tail_capped}"
                state["last_feedback"] = base_fb
                verify_ev_level = evidence_level(
                    compile_outcome=compile_info,
                    test_outcome=test_runner_info,
                    test_policy=state.get("test_policy"),
                )
                _snapshot_attempt(
                    safe_rec,
                    state,
                    subgoal_id="__verify__",
                    tier="semantic",
                    outcome="semantic_failure",
                    diff=final_diff,
                    compile_info=compile_info,
                    tests_ran=tests_ran,
                    test_outcome=test_runner_info.get("outcome") if isinstance(test_runner_info, dict) else test_output,
                    verdict=verdict,
                    feedback_sent=state.get("last_feedback"),
                    evidence_level=verify_ev_level,
                    jev_state_keys=verify_keys,
                )
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
            verify_ev_level = evidence_level(
                compile_outcome=compile_info,
                test_outcome=test_runner_info,
                test_policy=state.get("test_policy"),
            )
            _snapshot_attempt(
                safe_rec,
                state,
                subgoal_id="__verify__",
                tier="semantic",
                outcome="passed",
                diff=final_diff,
                compile_info=compile_info,
                tests_ran=tests_ran,
                test_outcome=test_runner_info.get("outcome") if isinstance(test_runner_info, dict) else test_output,
                verdict=None,
                feedback_sent="",
                evidence_level=verify_ev_level,
            )

        verify_entry: Dict[str, Any] = {
            "node": "verify",
            "gate_status": state.get("gate_status"),
            "status": state.get("status"),
            "compile": compile_info,
            "test_runner": test_runner_info,
            "mechanically_unverified_subgoals": unverified_subgoals,
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

        if state.get("gate_status") == "verification_failed":
            _safe_escalate_deadlock(
                gatekeeper,
                trajectory=state.get("trajectory", []),
                triggering_tier="verification",
                integration_branch=integration_branch,
                thread_id=state.get("thread_id"),
            )
        return state

    except (MainDivergedError, TrackedModificationsError) as e:
        status_key = "main_diverged" if isinstance(e, MainDivergedError) else "tracked_modifications"
        state["gate_status"] = status_key
        state["status"] = "escalated"
        state["last_feedback"] = str(e)
        _snapshot_attempt(
            safe_rec,
            state,
            subgoal_id="__verify__",
            tier="git",
            outcome=status_key,
            diff=final_diff,
            compile_info=compile_info if 'compile_info' in locals() else None,
            feedback_sent=str(e),
        )
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
        _safe_escalate_deadlock(
            gatekeeper,
            trajectory=state.get("trajectory", []),
            triggering_tier="verification",
            integration_branch=integration_branch,
            thread_id=state.get("thread_id"),
        )
        return state

    except Exception as e:
        state["gate_status"] = "verification_failed"
        state["status"] = "escalated"
        state["last_feedback"] = f"Verification error: {str(e)}"
        _snapshot_attempt(
            safe_rec,
            state,
            subgoal_id="__verify__",
            tier="verify",
            outcome="verification_exception",
            diff=final_diff,
            compile_info=compile_info if 'compile_info' in locals() else getattr(workspace, "last_compile_run", None),
            feedback_sent=f"Verification error: {str(e)}",
        )
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
        _safe_escalate_deadlock(
            gatekeeper,
            trajectory=state.get("trajectory", []),
            triggering_tier="verification",
            integration_branch=integration_branch,
            thread_id=state.get("thread_id"),
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
        or state.get("gate_status") in ("merge_failed", "verification_inconclusive")
        or state.get("status") == "escalated"
    ):
        return "escalate"
    if state.get("gate_status") == "unmeasured":
        return "implement"
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
        test_policy: str = "auto",
        recorder: Optional[Any] = None,
        retry_intel: str = "on",
        repo_profile: str = "off",
        dep_delta_jev: str = "off",
        max_investigation_turns: int = 20,
        advisory: Optional[Any] = None,
        advisory_policy: str = "on",
        role_provenance: Optional[Dict[str, Dict[str, Any]]] = None,
        memory: Optional[Any] = None,
        replan_enabled: bool = False,
        replan_planner: Optional[Any] = None,
        replan_validator: Optional[Any] = None,
    ):
        self.workspace = workspace
        self.gatekeeper = gatekeeper
        self.llm = llm
        self.test_policy = test_policy
        self.retry_intel = retry_intel
        self.repo_profile = repo_profile
        self.dep_delta_jev = dep_delta_jev
        self.max_investigation_turns = max_investigation_turns
        self.advisory = advisory
        self.advisory_policy = advisory_policy
        self.role_provenance = role_provenance or build_role_provenance(llm, gatekeeper, advisory)
        self.memory = memory
        self.replan_enabled = replan_enabled
        self.replan_planner = replan_planner
        self.replan_validator = replan_validator
        if recorder is None:
            self.recorder = SafeRecorder(NullRecorder())
        elif isinstance(recorder, SafeRecorder):
            self.recorder = recorder
        else:
            self.recorder = SafeRecorder(recorder)

        if checkpointer is not None:
            self.checkpointer = checkpointer
        elif db_path is not None:
            self.checkpointer = SqliteSaver.from_conn_string(str(db_path))
        else:
            self.checkpointer = None

        self.app = self.build_graph()

    def _node_investigate(self, state: State) -> State:
        return node_investigate(
            state,
            workspace=self.workspace,
            llm=self.llm,
            max_investigation_turns=self.max_investigation_turns,
        )

    def _node_plan(self, state: State) -> State:
        return node_plan(state, workspace=self.workspace, llm=self.llm, gatekeeper=self.gatekeeper, recorder=self.recorder)

    def _node_implement(self, state: State) -> State:
        return node_implement(state, workspace=self.workspace, llm=self.llm, gatekeeper=self.gatekeeper)

    def node_gate(self, state: State) -> State:
        return node_gate(
            state, workspace=self.workspace, gatekeeper=self.gatekeeper,
            recorder=self.recorder, advisory=self.advisory, memory=self.memory,
            replan_enabled=self.replan_enabled, replan_planner=self.replan_planner,
            replan_validator=self.replan_validator,
        )

    def _node_verify(self, state: State) -> State:
        return node_verify(state, workspace=self.workspace, gatekeeper=self.gatekeeper, recorder=self.recorder)

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

    def execute(self, ticket: str, thread_id: Optional[str] = None) -> State:
        if thread_id is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rnd = uuid.uuid4().hex[:4]
            thread_id = f"run-{ts}-{rnd}"

        run_id = thread_id
        if self.recorder.has_run(run_id):
            run_id = f"{thread_id}_{uuid.uuid4().hex[:6]}"

        base_commit = None
        if self.workspace is not None:
            if hasattr(self.workspace, "base_commit") and self.workspace.base_commit:
                base_commit = self.workspace.base_commit
            elif hasattr(self.workspace, "_get_head_commit"):
                base_commit = self.workspace._get_head_commit()

        integration_branch = f"jev-ticket-{thread_id}"

        has_checkpoint = False
        if self.checkpointer is not None:
            try:
                cp = self.checkpointer.get({"configurable": {"thread_id": thread_id}})
                if cp is not None:
                    has_checkpoint = True
            except Exception:
                pass

        branch_exists = False
        if self.workspace is not None and hasattr(self.workspace, "_run_repo_git"):
            try:
                res = self.workspace._run_repo_git(["rev-parse", "--verify", integration_branch])
                if res.returncode == 0:
                    branch_exists = True
            except Exception:
                pass

        if has_checkpoint or branch_exists:
            warning_msg = (
                f"WARNING: Thread '{thread_id}' already has existing checkpoints or integration branch '{integration_branch}'. "
                f"Restarting thread from scratch and deleting prior integration branch (no resume)."
            )
            print(warning_msg, file=sys.stderr, flush=True)

        if self.workspace is not None and hasattr(self.workspace, "create_integration_branch"):
            self.workspace.create_integration_branch(integration_branch, base_commit=base_commit)

        worker_model = None
        if self.llm is not None:
            worker_model = getattr(self.llm, "model", None) or getattr(self.llm, "model_name", None)
            if not worker_model and hasattr(self.llm, "__class__"):
                worker_model = self.llm.__class__.__name__

        profile_text = None
        profile_chars = None
        if self.repo_profile == "on":
            try:
                from jev.repo_profile import get_or_build_repo_profile

                main_repo_dir = self.workspace.repo_dir if self.workspace is not None else Path.cwd()
                prof = get_or_build_repo_profile(main_repo_dir)
                rendered = prof.to_prompt_text()
                profile_text = rendered if rendered else None
                profile_chars = len(rendered) if rendered else None
            except Exception as e:
                logger.warning(f"{type(e).__name__}: {e}")
                profile_text = None
                profile_chars = None

        engine_commit = _get_engine_commit()
        self.recorder.record_run_start(
            run_id=run_id,
            thread_id=thread_id,
            ticket=ticket,
            base_commit=base_commit,
            engine_commit=engine_commit,
            worker_model=worker_model,
            test_policy=self.test_policy,
            retry_intel=self.retry_intel,
            repo_profile=self.repo_profile,
            profile_chars=profile_chars,
            dep_delta_jev=self.dep_delta_jev,
            provenance=json.dumps(self.role_provenance, sort_keys=True),
        )

        initial_state: State = {
            "ticket": ticket,
            "thread_id": thread_id,
            "run_id": run_id,
            "base_commit": base_commit,
            "integration_branch": integration_branch,
            "subgoal_base_commit": base_commit,
            "test_policy": self.test_policy,
            "retry_intel": self.retry_intel,
            "repo_profile": self.repo_profile,
            "repo_profile_text": profile_text,
            "dep_delta_jev": self.dep_delta_jev,
            "replan_count": 0,
            "replan_budget": 1,
            "committed_subgoals": [],
            "pruned_subgoals": [],
            "replan_history": [],
            "failed_attempts": [],
            "plan_queue": [],
            "current_subgoal": None,
            "mechanical_strike_count": 0,
            "semantic_strike_count": 0,
            "unmeasured_retry_count": 0,
            "mechanically_unverified_subgoals": [],
            "trajectory": [],
            "gate_status": None,
            "last_feedback": None,
            "status": None,
            "role_provenance": self.role_provenance,
        }

        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        final_state = self.app.invoke(initial_state, config=config)
        for entry in final_state.get("trajectory", []) or []:
            if isinstance(entry, dict):
                entry.setdefault("role_provenance", self.role_provenance)

        final_status = final_state.get("status") or "unknown"
        final_commit = None
        if self.workspace is not None:
            if hasattr(self.workspace, "_get_head_commit"):
                try:
                    final_commit = self.workspace._get_head_commit()
                except Exception:
                    pass
            if not final_commit and hasattr(self.workspace, "base_commit"):
                final_commit = self.workspace.base_commit

        inv_notes = final_state.get("investigation_notes") or ""
        notes_chars = len(inv_notes)

        run_dep_delta = None
        if final_status == "completed" and final_commit and base_commit:
            if final_commit != base_commit:
                if self.workspace is not None and hasattr(self.workspace, "get_manifest_changes"):
                    try:
                        changes = self.workspace.get_manifest_changes(base_commit, final_commit)
                        delta = compute_dependency_delta(changes)
                        run_dep_delta = delta.to_json()
                    except Exception as e:
                        logger.warning(f"{type(e).__name__}: {e}")
                        run_dep_delta = None
            else:
                run_dep_delta = compute_dependency_delta({}).to_json()

        self.recorder.record_run_end(
            run_id=run_id,
            final_status=final_status,
            final_commit=final_commit,
            notes_chars=notes_chars,
            dependency_delta=run_dep_delta,
        )

        return final_state
