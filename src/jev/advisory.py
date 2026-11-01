"""Provider-agnostic, non-authoritative advisory contracts.

The advisory layer is deliberately independent from the engine and from any
particular model vendor.  Provider failures are represented as results so an
advisory can never become a gate by accident.
"""

import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class AdvisoryErrorCode(str, Enum):
    CONFIGURATION = "configuration_error"
    TIMEOUT = "timeout"
    HTTP = "http_error"
    CONNECTION = "connection_error"
    MALFORMED_RESPONSE = "malformed_response"
    VALIDATION = "validation_error"
    UNKNOWN = "unknown_error"


class RepairBrief(BaseModel):
    """Small, bounded shape suitable for displaying provider suggestions."""

    summary: str = Field(..., min_length=1, max_length=500)
    cause: str = Field(..., min_length=1, max_length=500)
    actions: List[str] = Field(..., min_length=1, max_length=8)

    model_config = ConfigDict(extra="forbid")

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if any(not isinstance(action, str) or not 1 <= len(action) <= 500 for action in self.actions):
            raise ValueError("actions must contain 1-500 character strings")


class AdvisoryRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000)
    system_prompt: Optional[str] = Field(default=None, max_length=5_000)
    json_schema: Optional[Dict[str, Any]] = None
    json_mode: bool = False
    temperature: Optional[float] = Field(default=None, ge=0, le=2)

    def __init__(self, **data: Any) -> None:
        from jev.proposals import redact_secrets_in_text
        if "prompt" in data and isinstance(data["prompt"], str):
            data["prompt"] = redact_secrets_in_text(data["prompt"])
        if "system_prompt" in data and isinstance(data["system_prompt"], str):
            data["system_prompt"] = redact_secrets_in_text(data["system_prompt"])
        super().__init__(**data)


class AdvisoryResult(BaseModel):
    """Non-fatal outcome of an advisory request."""

    ok: bool
    brief: Optional[RepairBrief] = None
    error_code: Optional[AdvisoryErrorCode] = None
    error_message: Optional[str] = None
    model: Optional[str] = None


class AdvisoryProvider(Protocol):
    def advise(self, request: AdvisoryRequest) -> AdvisoryResult:
        """Return an advisory result without raising provider failures."""


_REASONING_TRACE_PATTERNS = (
    re.compile(r"(?i)<think>.*?(?:</think>|\Z)"),
    re.compile(r"(?i)\b(?:chain[- ]of[- ]thought|reasoning[- ]trace|hidden[- ]reasoning|private reasoning)\b"),
)
_REASONING_KEYS = {"cot", "chain_of_thought", "reasoning_trace", "hidden_reasoning"}


def _safe_message(message: str, secret: Optional[str] = None) -> str:
    """Bound diagnostics and ensure credentials cannot appear in them."""
    from jev.proposals import redact_secrets_in_text
    if secret:
        message = message.replace(secret, "[REDACTED]")
    message = redact_secrets_in_text(message)
    message = re.sub(r"(?i)(bearer\s+|api[-_ ]?key[=: ]+)[^\s,;]+", r"\1[REDACTED]", message)
    return message[:500]


def parse_repair_brief(content: Any) -> RepairBrief:
    """Parse JSON content from a compatible chat response, enforcing strict schema and rejecting reasoning traces."""
    if not isinstance(content, str):
        raise ValueError("message content must be a string")
    text = content.strip()

    # Reject reasoning traces and <think> blocks
    if "<think>" in text or "</think>" in text:
        raise ValueError("reasoning-trace text is rejected")
    for pat in _REASONING_TRACE_PATTERNS:
        if pat.search(text):
            raise ValueError("reasoning-trace text is rejected")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("advisory content is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("advisory JSON must be an object")

    # Reject raw reasoning keys or nested reasoning traces
    for k in payload.keys():
        if str(k).lower() in _REASONING_KEYS:
            raise ValueError("reasoning-trace text is rejected")
    for v in payload.values():
        if isinstance(v, str) and ("<think>" in v or "</think>" in v):
            raise ValueError("reasoning-trace text is rejected")
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and ("<think>" in item or "</think>" in item):
                    raise ValueError("reasoning-trace text is rejected")

    try:
        return RepairBrief(**payload)
    except ValidationError as exc:
        raise ValueError("advisory JSON does not match RepairBrief") from exc


class AdvisoryService:
    """Thin injectable facade useful for engine callers and fake adapters."""

    def __init__(self, provider: AdvisoryProvider):
        self.provider = provider

    def request_repair_brief(self, prompt: str, *, system_prompt: Optional[str] = None,
                             json_schema: Optional[Dict[str, Any]] = None,
                             json_mode: bool = False) -> AdvisoryResult:
        return self.provider.advise(AdvisoryRequest(prompt=prompt, system_prompt=system_prompt,
                                                    json_schema=json_schema, json_mode=json_mode))


__all__ = ["AdvisoryErrorCode", "AdvisoryProvider", "AdvisoryRequest", "AdvisoryResult",
           "AdvisoryService", "RepairBrief", "parse_repair_brief", "_safe_message"]
