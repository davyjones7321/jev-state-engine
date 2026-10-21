import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx
from dotenv import dotenv_values

from jev.models import Subgoal, ValidationVerdict

logger = logging.getLogger(__name__)


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
        self._valid_only_mode: bool = False
        self._dep_delta_unsupported: bool = False
        self.last_state_keys: Optional[str] = None

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
        investigation_notes: Optional[str] = None,
        dependency_delta: Optional[Dict[str, Any]] = None,
        test_evidence: Optional[Dict[str, Any]] = None,
    ) -> ValidationVerdict:
        """Tier 1 validation using Jev System One model."""
        state: Dict[str, Any] = {
            "description": subgoal.description,
            "scope": subgoal.scope,
            "diff": diff,
        }

        if investigation_notes:
            state["investigation_notes"] = investigation_notes

        if "tests_excluded_by_policy=true" in mechanical_detail:
            state["tests_excluded_by_policy"] = True
            state["mechanical_detail"] = mechanical_detail
        elif "untested" in mechanical_detail.lower():
            state["untested_pass"] = True
            state["mechanical_detail"] = mechanical_detail
        elif mechanical_detail:
            state["mechanical_detail"] = mechanical_detail
        if test_evidence is not None and not state.get("tests_excluded_by_policy"):
            state["test_evidence"] = test_evidence

        if dependency_delta is not None and not self._dep_delta_unsupported:
            delta_dict = dict(dependency_delta)
            dropped = 0
            for k in ("added", "removed", "changed"):
                if k in delta_dict and isinstance(delta_dict[k], list):
                    items = delta_dict[k]
                    if len(items) > 50:
                        dropped += len(items) - 50
                        delta_dict[k] = items[:50]
            if dropped > 0:
                delta_dict["truncated"] = dropped
            state["dependency_delta"] = delta_dict

        questions = {
            "valid": {
                "type": "noul",
                "instructions": "Does the diff fully and correctly implement the subgoal described in state, without exceeding its declared scope?",
            },
            "incomplete": {
                "type": "noul",
                "instructions": "Does the diff leave any part of the subgoal description unimplemented?",
            },
            "wrong_behavior": {
                "type": "noul",
                "instructions": "Does the diff implement behavior that contradicts the subgoal description or the ticket?",
            },
            "conflicts_with_notes": {
                "type": "noul",
                "instructions": "Does the diff contradict conventions or facts stated in the investigation notes?",
            },
            "kind": {
                "type": "choice",
                "instructions": "What is the main problem with this diff?",
                "criteria": {
                    "incomplete": "Part of the subgoal is not implemented",
                    "wrong_behavior": "Behavior contradicts the subgoal or ticket",
                    "conflicts_with_conventions": "Contradicts repository conventions or investigation notes",
                    "low_quality": "Correct in intent but poorly executed",
                    "fine": "No problem",
                },
            },
        }

        if self._valid_only_mode:
            fallback_payload = {
                "model": "jev-latest",
                "state": state,
                "questions": {
                    "valid": questions["valid"]
                },
            }
            try:
                data = self._post_with_retry(fallback_payload)
                self.last_state_keys = ",".join(state.keys())
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 422) and "dependency_delta" in state:
                    self._dep_delta_unsupported = True
                    status_code = e.response.status_code
                    body_truncated = (e.response.text or "")[:500]
                    logger.warning(
                        f"Jev API returned HTTP {status_code}: {body_truncated}. "
                        "dependency_delta is unsupported and will not be sent again."
                    )
                    state_no_delta = dict(state)
                    del state_no_delta["dependency_delta"]
                    fallback_payload2 = {
                        "model": "jev-latest",
                        "state": state_no_delta,
                        "questions": {
                            "valid": questions["valid"]
                        },
                    }
                    data = self._post_with_retry(fallback_payload2)
                    self.last_state_keys = ",".join(state_no_delta.keys())
                else:
                    raise
            return self._parse_noul_verdict(data)

        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": questions,
        }

        try:
            data = self._post_with_retry(payload)
            self.last_state_keys = ",".join(state.keys())
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 422) and "dependency_delta" in state:
                self._dep_delta_unsupported = True
                status_code = e.response.status_code
                body_truncated = (e.response.text or "")[:500]
                logger.warning(
                    f"Jev API returned HTTP {status_code}: {body_truncated}. "
                    "dependency_delta is unsupported and will not be sent again."
                )
                state_no_delta = dict(state)
                del state_no_delta["dependency_delta"]
                retry_payload = {
                    "model": "jev-latest",
                    "state": state_no_delta,
                    "questions": questions,
                }
                try:
                    data = self._post_with_retry(retry_payload)
                    self.last_state_keys = ",".join(state_no_delta.keys())
                except httpx.HTTPStatusError as e2:
                    if e2.response.status_code in (400, 422):
                        self._valid_only_mode = True
                        status_code2 = e2.response.status_code
                        body_truncated2 = (e2.response.text or "")[:500]
                        logger.warning(
                            f"Jev API returned HTTP {status_code2}: {body_truncated2}. "
                            "Retry intelligence is degraded and Gatekeeper is now valid-only for this instance."
                        )
                        fallback_payload = {
                            "model": "jev-latest",
                            "state": state_no_delta,
                            "questions": {
                                "valid": questions["valid"]
                            },
                        }
                        data = self._post_with_retry(fallback_payload)
                        self.last_state_keys = ",".join(state_no_delta.keys())
                    else:
                        raise
            elif e.response.status_code in (400, 422):
                self._valid_only_mode = True
                status_code = e.response.status_code
                body_truncated = (e.response.text or "")[:500]
                logger.warning(
                    f"Jev API returned HTTP {status_code}: {body_truncated}. "
                    "Retry intelligence is degraded and Gatekeeper is now valid-only for this instance."
                )
                fallback_payload = {
                    "model": "jev-latest",
                    "state": state,
                    "questions": {
                        "valid": questions["valid"]
                    },
                }
                data = self._post_with_retry(fallback_payload)
                self.last_state_keys = ",".join(state.keys())
            else:
                raise

        return self._parse_noul_verdict(data)

    def _parse_noul_verdict(self, data: Dict[str, Any]) -> ValidationVerdict:
        valid_entry = None
        answers_dict: Dict[str, Any] = {}
        if isinstance(data, dict):
            if "answers" in data and isinstance(data["answers"], dict):
                answers_dict = data["answers"]
                valid_entry = answers_dict.get("valid")
            elif "valid" in data:
                answers_dict = data
                valid_entry = data.get("valid")
            elif "questions" in data and isinstance(data["questions"], dict):
                answers_dict = data["questions"]
                valid_entry = answers_dict.get("valid")

        if not isinstance(valid_entry, dict):
            raise ValueError(f"Malformed response schema: missing 'valid' object in {data}")

        reason = valid_entry.get("reason")

        model_name = data.get("model") if isinstance(data, dict) else None
        usage = data.get("usage") if isinstance(data, dict) else None
        raw_resp = data if isinstance(data, dict) else None

        # Parse extra answers (incomplete, wrong_behavior, conflicts_with_notes, kind)
        def _parse_noul_prob(entry: Any) -> Optional[float]:
            if isinstance(entry, dict):
                if "noul" in entry and isinstance(entry["noul"], (int, float)):
                    return float(entry["noul"])
                if "probability" in entry and isinstance(entry["probability"], (int, float)):
                    return float(entry["probability"])
                if "value" in entry and isinstance(entry["value"], bool):
                    return 1.0 if entry["value"] else 0.0
            elif isinstance(entry, (int, float)):
                return float(entry)
            return None

        incomplete_prob = _parse_noul_prob(answers_dict.get("incomplete"))
        wrong_behavior_prob = _parse_noul_prob(answers_dict.get("wrong_behavior"))
        conflicts_prob = _parse_noul_prob(answers_dict.get("conflicts_with_notes"))

        kind_val: Optional[str] = None
        kind_conf: Optional[float] = None
        kind_probs: Optional[Dict[str, float]] = None
        kind_entry = answers_dict.get("kind")
        if isinstance(kind_entry, dict):
            kind_val = (
                kind_entry.get("choice")
                or kind_entry.get("value")
                or kind_entry.get("chosen")
                or kind_entry.get("selected")
            )
            if kind_val is not None:
                kind_val = str(kind_val)
            conf = kind_entry.get("confidence") or kind_entry.get("probability")
            if isinstance(conf, (int, float)):
                kind_conf = float(conf)
            probs = kind_entry.get("probabilities") or kind_entry.get("probs")
            if isinstance(probs, dict):
                kind_probs = {str(k): float(v) for k, v in probs.items() if isinstance(v, (int, float))}
        elif isinstance(kind_entry, str):
            kind_val = kind_entry

        extra_answers = {}
        for k in ("incomplete", "wrong_behavior", "conflicts_with_notes", "kind"):
            if k in answers_dict:
                extra_answers[k] = answers_dict[k]

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
                model=model_name,
                raw_response=raw_resp,
                usage=usage,
                kind=kind_val,
                kind_confidence=kind_conf,
                kind_probabilities=kind_probs,
                incomplete=incomplete_prob,
                wrong_behavior=wrong_behavior_prob,
                conflicts_with_notes=conflicts_prob,
                extra_answers=extra_answers or None,
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
                model=model_name,
                raw_response=raw_resp,
                usage=usage,
                kind=kind_val,
                kind_confidence=kind_conf,
                kind_probabilities=kind_probs,
                incomplete=incomplete_prob,
                wrong_behavior=wrong_behavior_prob,
                conflicts_with_notes=conflicts_prob,
                extra_answers=extra_answers or None,
            )

        raise ValueError(f"Malformed response schema: missing required fields in {valid_entry}")

    def verify_ticket(
        self,
        ticket: str,
        final_diff: str,
        test_output: str,
        investigation_notes: Optional[str] = None,
        test_evidence: Optional[Dict[str, Any]] = None,
    ) -> ValidationVerdict:
        """Phase 4 final verification post to Jev."""
        state: Dict[str, Any] = {
            "ticket": ticket,
            "final_diff": final_diff,
        }
        if "tests_excluded_by_policy=true" in test_output:
            state["tests_excluded_by_policy"] = True
        else:
            state["test_output"] = test_output
            if test_evidence is not None:
                state["test_evidence"] = test_evidence
        if investigation_notes:
            state["investigation_notes"] = investigation_notes
        if state.get("tests_excluded_by_policy"):
            instructions = (
                "Does the final diff completely resolve the ticket considering the investigation notes, "
                "compile and scope evidence? Tests are excluded by policy; do not infer a pass or failure "
                "from missing test results or reject solely because tests were not run."
            )
        elif investigation_notes and investigation_notes.strip():
            instructions = (
                "Considering the investigation notes regarding what parts of the ticket were already satisfied "
                "in the repository, does the final diff complete the remaining requirements of the ticket, and does "
                "the test output satisfy verification requirements (allowing an untested pass if the ticket does not "
                "require automated tests)?"
            )
        else:
            instructions = (
                "Does the final diff completely resolve the ticket, and does the test output "
                "satisfy verification requirements (allowing an untested pass if the ticket "
                "does not require automated tests)?"
            )

        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": {
                "valid": {
                    "type": "noul",
                    "instructions": instructions,
                }
            },
        }
        self.last_state_keys = ",".join(state.keys())
        data = self._post_with_retry(payload)
        return self._parse_noul_verdict(data)


    def escalate_deadlock(
        self,
        trajectory: List[Dict[str, Any]],
        triggering_tier: str,
        log_path: Union[str, Path] = "escalation.log",
        integration_branch: Optional[str] = None,
        thread_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Logs deadlock escalation details and dumps escalation output for HITL review."""
        content: Dict[str, Any] = {
            "triggering_tier": triggering_tier,
            "trajectory": trajectory,
        }
        if integration_branch:
            content["integration_branch"] = integration_branch
        if thread_id:
            content["thread_id"] = thread_id

        # Write to $JEV_HOME/runs/<thread_id>/escalation.json
        jev_home_dir = Path(os.environ.get("JEV_HOME", Path.home() / ".jev")).expanduser().resolve()
        if thread_id:
            run_dir = jev_home_dir / "runs" / thread_id
            run_dir.mkdir(parents=True, exist_ok=True)
            run_file = run_dir / "escalation.json"
            run_file.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")

        # Also write to local log_path (e.g. escalation.log) for backwards compatibility
        log_file = Path(log_path)
        try:
            log_file.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
