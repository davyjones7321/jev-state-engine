import datetime
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jev.models import Subgoal, ValidationVerdict
from advisor.advisor_gatekeeper import AdvisorGatekeeper


class TriShadowGatekeeper:
    """Wraps primary Jev Gatekeeper and compares in shadow with Advisor Model and optionally Alternative Decision Model."""

    def __init__(
        self,
        primary_gatekeeper: Any,
        advisor_gatekeeper: Optional[AdvisorGatekeeper] = None,
        alternative_model_agent: Optional[Any] = None,
        log_path: Union[str, Path] = "judge_tri_comparison.jsonl",
    ):
        self.primary = primary_gatekeeper
        self.advisor = advisor_gatekeeper or AdvisorGatekeeper()
        self.alternative_model = alternative_model_agent
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)

    def validate_subgoal(
        self,
        subgoal: Subgoal,
        diff: str,
        mechanical_detail: str = "",
        investigation_notes: Optional[str] = None,
    ) -> ValidationVerdict:
        # 1. Authority: Jev
        t0 = time.time()
        if hasattr(self.primary, "validate_subgoal"):
            jev_verdict = self.primary.validate_subgoal(
                subgoal,
                diff,
                mechanical_detail=mechanical_detail,
                investigation_notes=investigation_notes,
            )
        else:
            jev_verdict = ValidationVerdict(valid=True, probability=1.0)
        jev_ms = (time.time() - t0) * 1000

        # 2. Shadow: Advisor Model (Kaggle GPU)
        advisor_verdict = self.advisor.validate_subgoal(
            subgoal,
            diff,
            mechanical_detail=mechanical_detail,
            investigation_notes=investigation_notes,
        )
        advisor_ms = getattr(advisor_verdict, "duration_ms", 0.0)
        advisor_cot = getattr(advisor_verdict, "cot", "")

        # 3. Shadow: Alternative Decision Model (Local CPU, optional)
        alt_valid: Optional[bool] = None
        alt_prob: Optional[float] = None
        alt_ms: Optional[float] = None
        if self.alternative_model is not None:
            t_alt = time.time()
            try:
                alt_state: Dict[str, Any] = {
                    "subgoal": subgoal.description,
                    "scope": subgoal.scope,
                    "diff": diff,
                }
                if investigation_notes:
                    alt_state["investigation_notes"] = investigation_notes
                if mechanical_detail:
                    alt_state["mechanical_detail"] = mechanical_detail

                questions = {
                    "valid": {
                        "type": "noul",
                        "instructions": (
                            "Does the diff fully and correctly implement the subgoal described in state, "
                            "without exceeding its declared scope?"
                        ),
                    }
                }
                alt_res = self.alternative_model.predict(state=alt_state, questions=questions)
                answers = alt_res.get("answers", {})
                valid_ans = answers.get("valid", {})
                noul_val = valid_ans.get("noul")
                if noul_val is not None:
                    alt_prob = round(float(noul_val), 4)
                    alt_valid = alt_prob >= 0.5
            except Exception as e:
                print(f"[SHADOW JUDGE WARNING] Alternative Decision Model evaluation error: {e}")
                alt_valid = None
                alt_prob = 0.0
            alt_ms = (time.time() - t_alt) * 1000

        self._log_and_print(
            phase="Subgoal",
            title=subgoal.description,
            diff_size=len(diff),
            jev_verdict=jev_verdict,
            jev_ms=jev_ms,
            advisor_verdict=advisor_verdict,
            advisor_ms=advisor_ms,
            advisor_cot=advisor_cot,
            alt_valid=alt_valid,
            alt_prob=alt_prob,
            alt_ms=alt_ms,
        )

        return jev_verdict

    def verify_ticket(
        self,
        ticket: str,
        final_diff: str,
        test_output: str = "",
        investigation_notes: Optional[str] = None,
    ) -> ValidationVerdict:
        # 1. Authority: Jev
        t0 = time.time()
        if hasattr(self.primary, "verify_ticket"):
            jev_verdict = self.primary.verify_ticket(
                ticket,
                final_diff,
                test_output=test_output,
                investigation_notes=investigation_notes,
            )
        else:
            jev_verdict = ValidationVerdict(valid=True, probability=1.0)
        jev_ms = (time.time() - t0) * 1000

        # 2. Shadow: Advisor Model (Kaggle GPU)
        advisor_verdict = self.advisor.verify_ticket(
            ticket,
            final_diff,
            test_output=test_output,
            investigation_notes=investigation_notes,
        )
        advisor_ms = getattr(advisor_verdict, "duration_ms", 0.0)
        advisor_cot = getattr(advisor_verdict, "cot", "")

        # 3. Shadow: Alternative Decision Model (Local CPU, optional)
        alt_valid: Optional[bool] = None
        alt_prob: Optional[float] = None
        alt_ms: Optional[float] = None
        if self.alternative_model is not None:
            t_alt = time.time()
            try:
                alt_state: Dict[str, Any] = {
                    "ticket": ticket,
                    "final_diff": final_diff,
                    "test_output": test_output,
                }
                if investigation_notes:
                    alt_state["investigation_notes"] = investigation_notes

                questions = {
                    "valid": {
                        "type": "noul",
                        "instructions": (
                            "Does the final diff completely resolve the ticket, and does the test output "
                            "satisfy verification requirements?"
                        ),
                    }
                }
                alt_res = self.alternative_model.predict(state=alt_state, questions=questions)
                answers = alt_res.get("answers", {})
                valid_ans = answers.get("valid", {})
                noul_val = valid_ans.get("noul")
                if noul_val is not None:
                    alt_prob = round(float(noul_val), 4)
                    alt_valid = alt_prob >= 0.5
            except Exception as e:
                print(f"[SHADOW JUDGE WARNING] Alternative Decision Model evaluation error: {e}")
                alt_valid = None
                alt_prob = 0.0
            alt_ms = (time.time() - t_alt) * 1000

        self._log_and_print(
            phase="Final Verification",
            title=ticket,
            diff_size=len(final_diff),
            jev_verdict=jev_verdict,
            jev_ms=jev_ms,
            advisor_verdict=advisor_verdict,
            advisor_ms=advisor_ms,
            advisor_cot=advisor_cot,
            alt_valid=alt_valid,
            alt_prob=alt_prob,
            alt_ms=alt_ms,
        )

        return jev_verdict

    def _log_and_print(
        self,
        phase: str,
        title: str,
        diff_size: int,
        jev_verdict: ValidationVerdict,
        jev_ms: float,
        advisor_verdict: ValidationVerdict,
        advisor_ms: float,
        advisor_cot: str,
        alt_valid: Optional[bool],
        alt_prob: Optional[float],
        alt_ms: Optional[float],
    ) -> None:
        short_title = (title[:55] + "...") if len(title) > 55 else title
        print(f"\n[TRI-JUDGE EVALUATION] >>> {phase}: '{short_title}' (Diff: {diff_size} B)")
        print(f"  * Jev                        (Authority): valid={jev_verdict.valid} (p={jev_verdict.probability:.2f}) [{jev_ms:.1f}ms]")
        if alt_valid is not None:
            print(f"  * Alternative Decision Model (Shadow)   : valid={alt_valid} (p={alt_prob:.4f}) [{alt_ms:.1f}ms]")
        print(f"  * Advisor Model              (Shadow)   : valid={advisor_verdict.valid} (p={advisor_verdict.probability:.2f}) [{advisor_ms:.1f}ms]")
        if advisor_verdict.reason:
            print(f"    -> Reason: {advisor_verdict.reason}")
        if advisor_cot:
            cot_preview = advisor_cot.strip().replace("\n", " ")[:140]
            print(f"    -> CoT <think>: {cot_preview}...")

        # Structured comparison log
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "phase": phase,
            "title": title,
            "diff_bytes": diff_size,
            "jev": {
                "valid": jev_verdict.valid,
                "probability": jev_verdict.probability,
                "duration_ms": round(jev_ms, 2),
            },
            "advisor_model": {
                "valid": advisor_verdict.valid,
                "probability": advisor_verdict.probability,
                "reason": advisor_verdict.reason,
                "violations": getattr(advisor_verdict, "violations", []),
                "cot": advisor_cot,
                "duration_ms": round(advisor_ms, 2),
            },
            "alternative_decision_model": (
                {
                    "valid": alt_valid,
                    "probability": alt_prob,
                    "duration_ms": round(alt_ms, 2),
                }
                if alt_valid is not None
                else None
            ),
        }

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
