import datetime
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jev.models import Subgoal, ValidationVerdict
from deepseek.deepseek_gatekeeper import DeepSeekGatekeeper


class TriShadowGatekeeper:
    """Wraps primary Jev Gatekeeper and compares in shadow with DeepSeek-R1 and optionally Laya."""

    def __init__(
        self,
        primary_gatekeeper: Any,
        deepseek_gatekeeper: Optional[DeepSeekGatekeeper] = None,
        laya_agent: Optional[Any] = None,
        log_path: Union[str, Path] = "judge_tri_comparison.jsonl",
    ):
        self.primary = primary_gatekeeper
        self.deepseek = deepseek_gatekeeper or DeepSeekGatekeeper()
        self.laya = laya_agent
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

        # 2. Shadow: DeepSeek-R1 (Kaggle GPU)
        ds_verdict = self.deepseek.validate_subgoal(
            subgoal,
            diff,
            mechanical_detail=mechanical_detail,
            investigation_notes=investigation_notes,
        )
        ds_ms = getattr(ds_verdict, "duration_ms", 0.0)
        ds_cot = getattr(ds_verdict, "cot", "")

        # 3. Shadow: Laya (Local CPU, optional)
        laya_valid: Optional[bool] = None
        laya_prob: Optional[float] = None
        laya_ms: Optional[float] = None
        if self.laya is not None:
            t_laya = time.time()
            try:
                l_state: Dict[str, Any] = {
                    "description": subgoal.description,
                    "scope": subgoal.scope,
                    "diff": diff,
                }
                if investigation_notes:
                    l_state["investigation_notes"] = investigation_notes
                if mechanical_detail:
                    l_state["mechanical_detail"] = mechanical_detail

                questions = {
                    "valid": {
                        "type": "noul",
                        "instructions": "Does the diff fully and correctly implement the subgoal described in state, without exceeding its declared scope?",
                    }
                }
                decisions = self.laya.decide(l_state, questions)
                laya_ans = decisions.get("valid")
                laya_valid = bool(laya_ans.value)
                laya_prob = round(float(laya_ans.confidence), 4)
            except Exception as e:
                laya_valid = None
                laya_prob = 0.0
            laya_ms = (time.time() - t_laya) * 1000

        self._log_and_print(
            phase="Subgoal",
            title=subgoal.description,
            diff_size=len(diff),
            jev_verdict=jev_verdict,
            jev_ms=jev_ms,
            ds_verdict=ds_verdict,
            ds_ms=ds_ms,
            ds_cot=ds_cot,
            laya_valid=laya_valid,
            laya_prob=laya_prob,
            laya_ms=laya_ms,
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

        # 2. Shadow: DeepSeek-R1 (Kaggle GPU)
        ds_verdict = self.deepseek.verify_ticket(
            ticket,
            final_diff,
            test_output=test_output,
            investigation_notes=investigation_notes,
        )
        ds_ms = getattr(ds_verdict, "duration_ms", 0.0)
        ds_cot = getattr(ds_verdict, "cot", "")

        # 3. Shadow: Laya (Local CPU, optional)
        laya_valid: Optional[bool] = None
        laya_prob: Optional[float] = None
        laya_ms: Optional[float] = None
        if self.laya is not None:
            t_laya = time.time()
            try:
                l_state: Dict[str, Any] = {
                    "ticket": ticket,
                    "final_diff": final_diff,
                    "test_output": test_output,
                }
                if investigation_notes:
                    l_state["investigation_notes"] = investigation_notes

                questions = {
                    "valid": {
                        "type": "noul",
                        "instructions": "Does the final diff completely resolve the ticket, and does the test output satisfy verification requirements?",
                    }
                }
                decisions = self.laya.decide(l_state, questions)
                laya_ans = decisions.get("valid")
                laya_valid = bool(laya_ans.value)
                laya_prob = round(float(laya_ans.confidence), 4)
            except Exception:
                laya_valid = None
                laya_prob = 0.0
            laya_ms = (time.time() - t_laya) * 1000

        self._log_and_print(
            phase="Final Verification",
            title=ticket,
            diff_size=len(final_diff),
            jev_verdict=jev_verdict,
            jev_ms=jev_ms,
            ds_verdict=ds_verdict,
            ds_ms=ds_ms,
            ds_cot=ds_cot,
            laya_valid=laya_valid,
            laya_prob=laya_prob,
            laya_ms=laya_ms,
        )

        return jev_verdict

    def _log_and_print(
        self,
        phase: str,
        title: str,
        diff_size: int,
        jev_verdict: ValidationVerdict,
        jev_ms: float,
        ds_verdict: ValidationVerdict,
        ds_ms: float,
        ds_cot: str,
        laya_valid: Optional[bool],
        laya_prob: Optional[float],
        laya_ms: Optional[float],
    ) -> None:
        short_title = (title[:55] + "...") if len(title) > 55 else title
        print(f"\n[TRI-JUDGE EVALUATION] >>> {phase}: '{short_title}' (Diff: {diff_size} B)")
        print(f"  * Jev         (Authority): valid={jev_verdict.valid} (p={jev_verdict.probability:.2f}) [{jev_ms:.1f}ms]")
        if laya_valid is not None:
            print(f"  * Laya        (Shadow)   : valid={laya_valid} (p={laya_prob:.4f}) [{laya_ms:.1f}ms]")
        print(f"  * DeepSeek-R1 (Shadow)   : valid={ds_verdict.valid} (p={ds_verdict.probability:.2f}) [{ds_ms:.1f}ms]")
        if ds_verdict.reason:
            print(f"    -> Reason: {ds_verdict.reason}")
        if ds_cot:
            cot_preview = ds_cot.strip().replace("\n", " ")[:140]
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
            "deepseek": {
                "valid": ds_verdict.valid,
                "probability": ds_verdict.probability,
                "reason": ds_verdict.reason,
                "violations": getattr(ds_verdict, "violations", []),
                "cot": ds_cot,
                "duration_ms": round(ds_ms, 2),
            },
            "laya": (
                {
                    "valid": laya_valid,
                    "probability": laya_prob,
                    "duration_ms": round(laya_ms, 2),
                }
                if laya_valid is not None
                else None
            ),
        }

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
