"""Offline, non-authoritative judge calibration utilities.

This module only measures a replacement judge.  It never supplies a gate
verdict to the engine and deliberately has no provider or network dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from jev.models import ValidationVerdict


Judge = Callable[[Any], Any]
_MAX_TEXT = 500
_MAX_PROVENANCE_ITEMS = 32


def _bounded_text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value)[:limit]


def _verdict_value(value: Any) -> bool:
    """Extract a strict boolean from the supported injected judge contracts."""
    if isinstance(value, ValidationVerdict):
        return value.valid
    if isinstance(value, bool):
        return value
    if hasattr(value, "valid") and isinstance(value.valid, bool):
        return value.valid
    raise TypeError("judge must return bool or an object with boolean 'valid'")


@dataclass(frozen=True)
class CalibrationCase:
    """One bounded, labeled input and its source provenance."""

    case_id: str
    input: Any
    label: Optional[bool]
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseEvaluation:
    case_id: str
    label: Optional[bool]
    jev_valid: Optional[bool]
    replacement_valid: Optional[bool]
    agreement: Optional[bool]
    provenance: Mapping[str, str]
    error: Optional[str] = None


@dataclass(frozen=True)
class CalibrationReport:
    """Bounded aggregate metrics; Jev remains the reference, not the replacement."""

    total_cases: int
    evaluated_cases: int
    agreement_count: int
    agreement_rate: Optional[float]
    false_pass_count: int
    false_reject_count: int
    confusion_matrix: Mapping[str, int]
    jev_confusion_matrix: Mapping[str, int]
    cases: tuple[CaseEvaluation, ...]


def _empty_matrix() -> dict[str, int]:
    return {"tp": 0, "tn": 0, "fp": 0, "fn": 0}


class CalibrationEvaluator:
    """Compare an injected replacement judge with recorded/fixture Jev calls.

    The evaluator is intentionally a plain callable boundary.  Callers can
    inject recorded Jev results, a deterministic fixture judge, or a local
    replacement; this class never constructs an HTTP client or changes engine
    state.
    """

    def __init__(
        self,
        jev_judge: Judge,
        replacement_judge: Judge,
        *,
        max_cases: int = 1_000,
        max_provenance_items: int = _MAX_PROVENANCE_ITEMS,
    ) -> None:
        if max_cases < 1 or max_provenance_items < 1:
            raise ValueError("calibration bounds must be positive")
        self.jev_judge = jev_judge
        self.replacement_judge = replacement_judge
        self.max_cases = max_cases
        self.max_provenance_items = max_provenance_items

    def _provenance(self, case: CalibrationCase) -> dict[str, str]:
        return {
            _bounded_text(key, 100): _bounded_text(value)
            for key, value in list(case.provenance.items())[: self.max_provenance_items]
        }

    @staticmethod
    def _add_matrix(matrix: dict[str, int], actual: bool, predicted: bool) -> None:
        key = ("tp" if actual else "tn") if actual == predicted else ("fp" if predicted else "fn")
        matrix[key] += 1

    def evaluate(self, cases: Sequence[CalibrationCase]) -> CalibrationReport:
        if len(cases) > self.max_cases:
            raise ValueError(f"calibration set exceeds max_cases={self.max_cases}")

        label_matrix = _empty_matrix()
        jev_matrix = _empty_matrix()
        evaluations: list[CaseEvaluation] = []
        agreement_count = false_pass_count = false_reject_count = evaluated = 0

        for case in cases:
            jev_valid: Optional[bool] = None
            replacement_valid: Optional[bool] = None
            errors: list[str] = []
            try:
                jev_valid = _verdict_value(self.jev_judge(case.input))
            except Exception as exc:
                errors.append(f"jev: {_bounded_text(exc)}")
            try:
                replacement_valid = _verdict_value(self.replacement_judge(case.input))
            except Exception as exc:
                errors.append(f"replacement: {_bounded_text(exc)}")

            agreement = (
                jev_valid == replacement_valid
                if jev_valid is not None and replacement_valid is not None
                else None
            )
            if replacement_valid is not None:
                evaluated += 1
                if case.label is not None:
                    self._add_matrix(label_matrix, case.label, replacement_valid)
                    if replacement_valid and not case.label:
                        false_pass_count += 1
                    elif not replacement_valid and case.label:
                        false_reject_count += 1
            if jev_valid is not None and replacement_valid is not None:
                self._add_matrix(jev_matrix, jev_valid, replacement_valid)
                if agreement:
                    agreement_count += 1

            evaluations.append(
                CaseEvaluation(
                    case_id=_bounded_text(case.case_id, 200),
                    label=case.label,
                    jev_valid=jev_valid,
                    replacement_valid=replacement_valid,
                    agreement=agreement,
                    provenance=self._provenance(case),
                    error="; ".join(errors) if errors else None,
                )
            )

        comparable = sum(case.agreement is not None for case in evaluations)
        return CalibrationReport(
            total_cases=len(cases),
            evaluated_cases=evaluated,
            agreement_count=agreement_count,
            agreement_rate=agreement_count / comparable if comparable else None,
            false_pass_count=false_pass_count,
            false_reject_count=false_reject_count,
            confusion_matrix=label_matrix,
            jev_confusion_matrix=jev_matrix,
            cases=tuple(evaluations),
        )


__all__ = ["CalibrationCase", "CalibrationEvaluator", "CalibrationReport", "CaseEvaluation"]
