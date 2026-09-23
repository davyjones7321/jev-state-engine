import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx
from dotenv import dotenv_values

from jev.models import Subgoal, ValidationVerdict


class Gatekeeper:
    """Isolates external HTTP calls to TypeSafe AI's Jev model (Tier 1 Gate)."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        env_file: Optional[Union[str, Path]] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        timeout: float = 30.0,
    ):
        self.env_file = Path(env_file) if env_file is not None else Path(".env")
        env_dict: Dict[str, str] = {}
        if self.env_file.exists():
            parsed = dotenv_values(self.env_file)
            env_dict = {k: v for k, v in parsed.items() if v is not None}

        # Read exclusively from environment or .env file with no hardcoded fallback
        self.api_url = (
            api_url
            or os.environ.get("JEV_API_URL")
            or env_dict.get("JEV_API_URL")
        )
        self.api_key = (
            api_key
            or os.environ.get("JEV_API_KEY")
            or env_dict.get("JEV_API_KEY")
        )

        if not self.api_url:
            raise ValueError("JEV_API_URL must be configured in environment or .env file.")
        if not self.api_key:
            raise ValueError("JEV_API_KEY must be configured in environment or .env file.")

        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.client = httpx.Client(timeout=self.timeout)

    def _post_with_retry(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.post(
                    self.api_url,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                last_exception = exc
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(self.backoff_factor * (2**attempt))
                continue

            req = getattr(response, "_request", None) or httpx.Request("POST", self.api_url)
            if 500 <= response.status_code < 600:
                last_exception = httpx.HTTPStatusError(
                    f"Server error {response.status_code}",
                    request=req,
                    response=response,
                )
                if attempt == self.max_retries - 1:
                    raise last_exception
                time.sleep(self.backoff_factor * (2**attempt))
                continue
            elif 400 <= response.status_code < 500:
                raise httpx.HTTPStatusError(
                    f"Client error {response.status_code}: {response.text}",
                    request=req,
                    response=response,
                )
            elif response.is_error:
                raise httpx.HTTPStatusError(
                    f"HTTP error {response.status_code}: {response.text}",
                    request=req,
                    response=response,
                )
            else:
                try:
                    return response.json()
                except Exception as exc:
                    raise ValueError(f"Malformed JSON in response: {exc}") from exc

        if last_exception:
            raise last_exception
        raise RuntimeError("Failed to obtain response after retries")

    def validate_subgoal(
        self,
        subgoal: Subgoal,
        diff: str,
        mechanical_detail: str = "",
    ) -> ValidationVerdict:
        """Tier 1 validation using Jev System One model."""
        state: Dict[str, Any] = {
            "description": subgoal.description,
            "scope": subgoal.scope,
            "diff": diff,
        }

        if "untested" in mechanical_detail.lower():
            state["untested_pass"] = True
            state["mechanical_detail"] = mechanical_detail
        elif mechanical_detail:
            state["mechanical_detail"] = mechanical_detail

        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": {
                "valid": {
                    "type": "noul",
                    "instructions": "Does the diff fully and correctly implement the subgoal described in state, without exceeding its declared scope?",
                }
            },
        }

        data = self._post_with_retry(payload)
        return self._parse_noul_verdict(data)

    def _parse_noul_verdict(self, data: Dict[str, Any]) -> ValidationVerdict:
        valid_entry = None
        if isinstance(data, dict):
            if "answers" in data and isinstance(data["answers"], dict):
                valid_entry = data["answers"].get("valid")
            elif "valid" in data:
                valid_entry = data.get("valid")
            elif "questions" in data and isinstance(data["questions"], dict):
                valid_entry = data["questions"].get("valid")

        if not isinstance(valid_entry, dict):
            raise ValueError(f"Malformed response schema: missing 'valid' object in {data}")

        reason = valid_entry.get("reason")

        # Format 1: Mock / explicit format with 'value' and 'probability'
        if "value" in valid_entry and "probability" in valid_entry:
            value = valid_entry["value"]
            probability = valid_entry["probability"]
            if not isinstance(value, bool):
                raise ValueError(f"Malformed response schema: 'value' must be a boolean, got {type(value)}")
            if not isinstance(probability, (int, float)):
                raise ValueError(f"Malformed response schema: 'probability' must be a number, got {type(probability)}")
            return ValidationVerdict(
                valid=value,
                probability=float(probability),
                reason=reason,
            )

        # Format 2: Real TypeSafe System One response with 'noul' probability
        if "noul" in valid_entry:
            probability = valid_entry["noul"]
            if not isinstance(probability, (int, float)):
                raise ValueError(f"Malformed response schema: 'noul' must be a number, got {type(probability)}")
            prob_float = float(probability)
            val = valid_entry.get("value")
            is_valid = val if isinstance(val, bool) else (prob_float >= 0.5)
            return ValidationVerdict(
                valid=is_valid,
                probability=prob_float,
                reason=reason,
            )

        raise ValueError(f"Malformed response schema: missing required fields in {valid_entry}")

    def verify_ticket(
        self,
        ticket: str,
        final_diff: str,
        test_output: str,
    ) -> ValidationVerdict:
        """Phase 4 final verification post to Jev."""
        state = {
            "ticket": ticket,
            "final_diff": final_diff,
            "test_output": test_output,
        }
        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": {
                "valid": {
                    "type": "noul",
                    "instructions": "Does the final diff and test output completely resolve and verify the ticket?",
                }
            },
        }
        data = self._post_with_retry(payload)
        return self._parse_noul_verdict(data)

    def escalate_deadlock(
        self,
        trajectory: List[Dict[str, Any]],
        triggering_tier: str,
        log_path: Union[str, Path] = "escalation.log",
    ) -> None:
        """Logs deadlock escalation details and dumps escalation.log for HITL review."""
        log_file = Path(log_path)
        content = {
            "triggering_tier": triggering_tier,
            "trajectory": trajectory,
        }
        log_file.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")
