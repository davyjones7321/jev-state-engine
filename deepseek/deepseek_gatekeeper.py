import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx
from pydantic import Field
from jev.models import Subgoal, ValidationVerdict


class DeepSeekVerdict(ValidationVerdict):
    """Extends standard ValidationVerdict with CoT and violation diagnostics."""
    cot: str = Field(default="")
    violations: List[str] = Field(default_factory=list)
    duration_ms: float = Field(default=0.0)


class DeepSeekGatekeeper:
    """Client for Remote DeepSeek-R1 Gatekeeper running on Kaggle GPU."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        timeout: float = 90.0,
        log_path: Optional[Union[str, Path]] = None,
    ):
        self.endpoint_url = (
            endpoint_url
            or os.environ.get("DEEPSEEK_ENDPOINT_URL")
            or "https://letter-consensus-bottle-hammer.trycloudflare.com"
        ).rstrip("/")
        self.validate_url = f"{self.endpoint_url}/validate"
        self.timeout = timeout
        self.log_path = Path(log_path) if log_path else Path("deepseek/reasoning_log.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=self.timeout)

    def validate_subgoal(
        self,
        subgoal: Subgoal,
        diff: str,
        mechanical_detail: str = "",
        investigation_notes: Optional[str] = None,
    ) -> DeepSeekVerdict:
        """Evaluates a subgoal using DeepSeek-R1 with step-by-step reasoning."""
        payload = {
            "subgoal": subgoal.description,
            "scope": subgoal.scope,
            "diff": diff,
            "mechanical_detail": mechanical_detail,
            "investigation_notes": investigation_notes or "",
            "is_final_verification": False,
        }
        return self._send_request(payload, context=f"Subgoal: {subgoal.description[:60]}")

    def verify_ticket(
        self,
        ticket: str,
        final_diff: str,
        test_output: str = "",
        investigation_notes: Optional[str] = None,
    ) -> DeepSeekVerdict:
        """Evaluates final ticket verification using DeepSeek-R1."""
        payload = {
            "ticket": ticket,
            "diff": final_diff,
            "mechanical_detail": test_output,
            "investigation_notes": investigation_notes or "",
            "is_final_verification": True,
        }
        return self._send_request(payload, context=f"Ticket Verification: {ticket[:60]}")

    def _send_request(self, payload: Dict[str, Any], context: str = "") -> DeepSeekVerdict:
        t0 = time.time()
        try:
            resp = self.client.post(self.validate_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            fallback_reason = f"DeepSeek-R1 request failed: {e}"
            return DeepSeekVerdict(
                valid=False,
                probability=0.0,
                reason=fallback_reason,
                cot="",
                duration_ms=duration_ms,
            )

        duration_ms = float(data.get("duration_ms", (time.time() - t0) * 1000))
        valid = bool(data.get("valid", False))
        prob = float(data.get("probability", 0.5))
        reason = str(data.get("reason", ""))
        cot = str(data.get("cot", ""))
        violations = list(data.get("violations", []))

        # Log reasoning entry
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "context": context,
            "valid": valid,
            "probability": prob,
            "reason": reason,
            "violations": violations,
            "cot": cot,
            "duration_ms": duration_ms,
        }
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception:
            pass

        return DeepSeekVerdict(
            valid=valid,
            probability=prob,
            reason=reason,
            cot=cot,
            violations=violations,
            duration_ms=duration_ms,
        )
