"""Small, local, deterministic episodic memory.

Memory is deliberately advisory: callers must explicitly write and retrieve it,
and current repository checks remain authoritative.  Records are JSONL so a
single damaged line cannot make the remaining memory unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


MAX_ENTRIES = 256
MAX_TEXT_CHARS = 1_000
MAX_SYMBOLS = 32
MAX_QUERY_CHARS = 200

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|token|password|passwd|secret|api[-_ ]?key)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(?:sk|pk)_[a-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),
)
_REASONING_PATTERN = re.compile(r"(?is)\b(?:chain[- ]of[- ]thought|private reasoning|internal reasoning)\s*[:=].*$")


def redact_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    """Return bounded text with common credentials and private reasoning removed."""
    text = "" if value is None else str(value)
    text = _REASONING_PATTERN.sub("[REDACTED]", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def _clean_query(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = redact_text(value, limit=MAX_QUERY_CHARS).strip()
    return value.casefold() or None


@dataclass(frozen=True)
class MemoryEntry:
    """A bounded hint with enough provenance to audit its source."""

    task: str
    summary: str
    outcome: str
    failure_class: Optional[str] = None
    symbols: tuple[str, ...] = ()
    run_id: Optional[str] = None
    attempt_id: Optional[str] = None
    commit: Optional[str] = None
    evidence_hash: Optional[str] = None
    created_at: Optional[str] = None
    entry_id: str = ""
    sequence: int = 0

    def __post_init__(self) -> None:
        for name in ("task", "summary", "outcome"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        text_values = (self.task, self.summary, self.outcome, self.failure_class,
                       self.run_id, self.attempt_id, self.commit, self.evidence_hash,
                       self.created_at)
        if any(value is not None and len(value) > MAX_TEXT_CHARS for value in text_values):
            raise ValueError("memory text exceeds bound")
        if len(self.symbols) > MAX_SYMBOLS:
            raise ValueError("too many symbols")
        if any(not isinstance(symbol, str) or not symbol or len(symbol) > MAX_TEXT_CHARS for symbol in self.symbols):
            raise ValueError("invalid memory symbol")


class EpisodicMemory:
    """Explicit JSONL-backed memory store; no reads or writes happen at init."""

    def __init__(self, path: str | Path, *, max_entries: int = MAX_ENTRIES,
                 max_text_chars: int = MAX_TEXT_CHARS) -> None:
        if max_entries <= 0 or max_text_chars <= 0:
            raise ValueError("memory limits must be positive")
        self.path = Path(path)
        self.max_entries = max_entries
        self.max_text_chars = max_text_chars

    def write(self, *, task: str, summary: str, outcome: str,
              failure_class: Optional[str] = None, symbols: Iterable[str] = (),
              run_id: Optional[str] = None, attempt_id: Optional[str] = None,
              commit: Optional[str] = None, evidence_hash: Optional[str] = None,
              created_at: Optional[str] = None) -> Optional[MemoryEntry]:
        """Redact, deduplicate, and persist one hint. Returns None for duplicates."""
        clean = self._build_entry(task=task, summary=summary, outcome=outcome,
                                  failure_class=failure_class, symbols=symbols,
                                  run_id=run_id, attempt_id=attempt_id, commit=commit,
                                  evidence_hash=evidence_hash, created_at=created_at)
        entries = self._load()
        fingerprint = self._fingerprint(clean)
        if any(self._fingerprint(item) == fingerprint for item in entries):
            return None
        sequence = max((item.sequence for item in entries), default=0) + 1
        entry = MemoryEntry(**{**clean.__dict__, "entry_id": fingerprint, "sequence": sequence})
        self._save((entries + [entry])[-self.max_entries:])
        return entry

    def retrieve(self, *, task: Optional[str] = None,
                 failure_class: Optional[str] = None,
                 symbol: Optional[str] = None, limit: int = 20) -> tuple[MemoryEntry, ...]:
        """Return exact-match hints in stable newest-first order."""
        if limit <= 0:
            return ()
        task_key, failure_key, symbol_key = map(_clean_query, (task, failure_class, symbol))
        if any(value is not None and len(value) > MAX_QUERY_CHARS for value in (task_key, failure_key, symbol_key)):
            return ()
        matches = []
        for entry in self._load():
            if task_key is not None and entry.task.casefold() != task_key:
                continue
            if failure_key is not None and (entry.failure_class or "").casefold() != failure_key:
                continue
            if symbol_key is not None and symbol_key not in {item.casefold() for item in entry.symbols}:
                continue
            matches.append(entry)
        return tuple(sorted(matches, key=lambda item: (-item.sequence, item.entry_id))[:limit])

    def list(self) -> tuple[MemoryEntry, ...]:
        return tuple(sorted(self._load(), key=lambda item: (-item.sequence, item.entry_id)))

    def forget(self, entry_id: str) -> bool:
        entries = self._load()
        remaining = [entry for entry in entries if entry.entry_id != entry_id]
        if len(remaining) == len(entries):
            return False
        self._save(remaining)
        return True

    def rebuild(self) -> tuple[MemoryEntry, ...]:
        """Rewrite valid records, dropping corrupt lines and reapplying bounds."""
        entries = self._load()
        self._save(entries[-self.max_entries:]) if entries else self._remove_if_present()
        return self.list()

    def _build_entry(self, **values: Any) -> MemoryEntry:
        limit = self.max_text_chars
        symbols = tuple(dict.fromkeys(redact_text(item, limit=limit).strip() for item in values.pop("symbols") if str(item).strip()))
        values["task"] = redact_text(values["task"], limit=limit).strip()
        values["summary"] = redact_text(values["summary"], limit=limit).strip()
        values["outcome"] = redact_text(values["outcome"], limit=limit).strip()
        values["failure_class"] = redact_text(values["failure_class"], limit=limit).strip() or None
        for key in ("run_id", "attempt_id", "commit", "evidence_hash", "created_at"):
            if values[key] is not None:
                values[key] = redact_text(values[key], limit=limit).strip() or None
        values["symbols"] = symbols[:MAX_SYMBOLS]
        return MemoryEntry(**values)

    @staticmethod
    def _fingerprint(entry: MemoryEntry) -> str:
        payload = {key: getattr(entry, key) for key in ("task", "summary", "outcome", "failure_class", "symbols", "run_id", "attempt_id", "commit", "evidence_hash")}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=list).encode()).hexdigest()

    def _load(self) -> list[MemoryEntry]:
        if not self.path.is_file():
            return []
        entries: list[MemoryEntry] = []
        seen: set[str] = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        for line in lines:
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                payload["symbols"] = tuple(payload.get("symbols") or ())
                entry = MemoryEntry(**payload)
                if entry.entry_id != self._fingerprint(entry):
                    continue
                bounded = (entry.task, entry.summary, entry.outcome, entry.failure_class,
                           entry.run_id, entry.attempt_id, entry.commit, entry.evidence_hash,
                           entry.created_at)
                if any(value is not None and len(value) > self.max_text_chars for value in bounded):
                    continue
                if entry.entry_id not in seen:
                    entries.append(entry)
                    seen.add(entry.entry_id)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(entries, key=lambda item: (item.sequence, item.entry_id))[-self.max_entries:]

    def _save(self, entries: Iterable[MemoryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(entry.__dict__, sort_keys=True, ensure_ascii=False) + "\n" for entry in entries)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _remove_if_present(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


MemoryStore = EpisodicMemory

__all__ = ["EpisodicMemory", "MemoryEntry", "MemoryStore", "redact_text"]
