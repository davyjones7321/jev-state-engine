import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from jev.models import Subgoal, ValidationVerdict


class ShadowGatekeeper:
    """Shadow testing wrapper for dual Jev + Laya evaluation.

    - Primary Authority: Jev Gatekeeper (governs engine transitions)
    - Shadow Evaluator: Laya Model (runs in background on CPU)
    - Telemetry: Records comparative metrics to a JSONL log
    """

    def __init__(
        self,
        primary_gatekeeper: Any,
        laya_agent: Any,
        log_path: Union[str, Path] = "laya/judge_comparison.jsonl",
    ):
        self.primary = primary_gatekeeper
        self.laya = laya_agent
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _append_log(self, record: Dict[str, Any]) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            print(f"[SHADOW JUDGE WARNING] Could not write comparison log: {exc}")

    def validate_subgoal(
        self,
        subgoal: Subgoal,
        diff: str,
        mechanical_detail: str = "",
        investigation_notes: Optional[str] = None,
    ) -> ValidationVerdict:
        # 1. Primary Authority: Jev
        t0_jev = time.perf_counter()
        if hasattr(self.primary, "validate_subgoal"):
            jev_verdict = self.primary.validate_subgoal(
                subgoal, diff, mechanical_detail=mechanical_detail, investigation_notes=investigation_notes
            )
        else:
            raise ValueError("Primary gatekeeper missing validate_subgoal")
        jev_duration_ms = round((time.perf_counter() - t0_jev) * 1000, 2)

        # 2. Shadow Evaluator: Laya
        laya_valid: Optional[bool] = None
        laya_prob: Optional[float] = None
        laya_duration_ms: Optional[float] = None
        try:
            t0_laya = time.perf_counter()
            laya_state: Dict[str, Any] = {
                "subgoal": subgoal.description,
                "scope": subgoal.scope,
                "diff": diff,
            }
            if investigation_notes:
                laya_state["investigation_notes"] = investigation_notes
            if mechanical_detail:
                laya_state["mechanical_detail"] = mechanical_detail

            questions = {
                "valid": {
                    "type": "noul",
                    "instructions": (
                        "Does the diff fully and correctly implement the subgoal described in state, "
                        "without exceeding its declared scope?"
                    ),
                }
            }
            laya_res = self.laya.predict(state=laya_state, questions=questions)
            laya_duration_ms = round((time.perf_counter() - t0_laya) * 1000, 2)

            answers = laya_res.get("answers", {})
            valid_ans = answers.get("valid", {})
            noul_val = valid_ans.get("noul")
            if noul_val is not None:
                laya_prob = float(noul_val)
                laya_valid = laya_prob >= 0.5
        except Exception as exc:
            print(f"[SHADOW JUDGE WARNING] Laya evaluation error: {exc}")

        # 3. Log Comparison Telemetry
        agreement = (jev_verdict.valid == laya_valid) if (laya_valid is not None) else None
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "validate_subgoal",
            "subgoal": {
                "description": subgoal.description,
                "scope": subgoal.scope,
            },
            "diff_bytes": len(diff.encode("utf-8")),
            "jev": {
                "valid": jev_verdict.valid,
                "probability": getattr(jev_verdict, "probability", None),
                "reason": getattr(jev_verdict, "reason", None),
                "duration_ms": jev_duration_ms,
            },
            "laya": {
                "valid": laya_valid,
                "probability": laya_prob,
                "duration_ms": laya_duration_ms,
            },
            "agreement": agreement,
        }
        self._append_log(record)

        # 4. Console Telemetry
        prob_str = f"{laya_prob:.4f}" if laya_prob is not None else "N/A"
        print(
            f"\n[SHADOW JUDGE] >>> Subgoal: '{subgoal.description[:40]}...'\n"
            f"  • Jev  (Authority): valid={jev_verdict.valid} (p={getattr(jev_verdict, 'probability', 1.0):.2f}) [{jev_duration_ms}ms]\n"
            f"  • Laya (Shadow)   : valid={laya_valid} (p={prob_str}) [{laya_duration_ms}ms]\n"
            f"  • Agreement       : {agreement}\n"
        )

        # 5. ALWAYS return Jev's verdict to preserve engine control loop
        return jev_verdict

    def verify_ticket(
        self,
        ticket: str,
        final_diff: str,
        test_output: str,
        investigation_notes: Optional[str] = None,
    ) -> ValidationVerdict:
        # 1. Primary Authority: Jev
        t0_jev = time.perf_counter()
        if hasattr(self.primary, "verify_ticket"):
            jev_verdict = self.primary.verify_ticket(
                ticket, final_diff, test_output, investigation_notes=investigation_notes
            )
        else:
            raise ValueError("Primary gatekeeper missing verify_ticket")
        jev_duration_ms = round((time.perf_counter() - t0_jev) * 1000, 2)

        # 2. Shadow Evaluator: Laya
        laya_valid: Optional[bool] = None
        laya_prob: Optional[float] = None
        laya_duration_ms: Optional[float] = None
        try:
            t0_laya = time.perf_counter()
            laya_state: Dict[str, Any] = {
                "ticket": ticket,
                "final_diff": final_diff,
                "test_output": test_output,
            }
            if investigation_notes:
                laya_state["investigation_notes"] = investigation_notes

            questions = {
                "valid": {
                    "type": "noul",
                    "instructions": (
                        "Does the final diff completely resolve the ticket, and does the test output "
                        "satisfy verification requirements?"
                    ),
                }
            }
            laya_res = self.laya.predict(state=laya_state, questions=questions)
            laya_duration_ms = round((time.perf_counter() - t0_laya) * 1000, 2)

            answers = laya_res.get("answers", {})
            valid_ans = answers.get("valid", {})
            noul_val = valid_ans.get("noul")
            if noul_val is not None:
                laya_prob = float(noul_val)
                laya_valid = laya_prob >= 0.5
        except Exception as exc:
            print(f"[SHADOW JUDGE WARNING] Laya verify error: {exc}")

        # 3. Log Comparison Telemetry
        agreement = (jev_verdict.valid == laya_valid) if (laya_valid is not None) else None
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "verify_ticket",
            "ticket": ticket,
            "final_diff_bytes": len(final_diff.encode("utf-8")),
            "jev": {
                "valid": jev_verdict.valid,
                "probability": getattr(jev_verdict, "probability", None),
                "reason": getattr(jev_verdict, "reason", None),
                "duration_ms": jev_duration_ms,
            },
            "laya": {
                "valid": laya_valid,
                "probability": laya_prob,
                "duration_ms": laya_duration_ms,
            },
            "agreement": agreement,
        }
        self._append_log(record)

        # 4. Console Telemetry
        prob_str = f"{laya_prob:.4f}" if laya_prob is not None else "N/A"
        print(
            f"\n[SHADOW JUDGE] >>> Final Verification:\n"
            f"  • Jev  (Authority): valid={jev_verdict.valid} (p={getattr(jev_verdict, 'probability', 1.0):.2f}) [{jev_duration_ms}ms]\n"
            f"  • Laya (Shadow)   : valid={laya_valid} (p={prob_str}) [{laya_duration_ms}ms]\n"
            f"  • Agreement       : {agreement}\n"
        )

        return jev_verdict

    def escalate_deadlock(self, *args, **kwargs) -> Any:
        if hasattr(self.primary, "escalate_deadlock"):
            return self.primary.escalate_deadlock(*args, **kwargs)
