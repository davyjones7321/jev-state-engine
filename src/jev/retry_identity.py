"""src/jev/retry_identity.py

Data structures and utilities for retry attempt identity (Phase R1).
Keyed by (run_id, subgoal_id, attempt, phase).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


def create_execution_record(
    subgoal_id: str,
    attempt: int,
    phase: str,
    process_launched: bool,
    argv: Optional[List[str]] = None,
    exit_code: Optional[int] = None,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    duration_s: Optional[float] = None,
    reason: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a standardized execution record dict."""
    start = started_at if started_at is not None else time.time()
    end = ended_at if ended_at is not None else time.time()
    dur = duration_s if duration_s is not None else round(max(0.0, end - start), 4)

    return {
        "execution_id": str(uuid.uuid4()),
        "run_id": run_id,
        "subgoal_id": subgoal_id,
        "attempt": int(attempt),
        "phase": phase,
        "process_launched": bool(process_launched),
        "argv": list(argv) if argv is not None else None,
        "exit_code": exit_code,
        "started_at": start,
        "ended_at": end,
        "duration_s": dur,
        "reason": reason,
    }


def create_event(
    event_name: str,
    subgoal_id: str,
    attempt: int,
    phase: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Create a standardized trajectory event dict without secrets or file contents."""
    event: Dict[str, Any] = {
        "event": event_name,
        "subgoal_id": subgoal_id,
        "attempt": int(attempt),
        "timestamp": time.time(),
    }
    if phase is not None:
        event["phase"] = phase
    for k, v in kwargs.items():
        if k not in ("diff", "content", "file_content", "patch", "secret"):
            event[k] = v
    return event
