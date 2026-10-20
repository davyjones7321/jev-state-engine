"""Offline threshold calibration reports.

The policy is intentionally advisory: it scores labeled outcomes and returns a
shadow recommendation.  It never changes Jev, ``node_gate``, or engine state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


_DEFAULT_MAX_OUTCOMES = 1_000
_DEFAULT_MIN_OUTCOMES = 10


@dataclass(frozen=True)
class LabeledOutcome:
    """A bounded observation of a semantic probability and trusted label."""

    probability: float
    label: bool
    case_id: str = ""


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    sample_count: int
    true_passes: int
    true_failures: int
    false_passes: int
    false_failures: int
    false_pass_rate: Optional[float]
    false_failure_rate: Optional[float]
    weighted_loss: float


@dataclass(frozen=True)
class ThresholdRecommendation:
    """A shadow-only recommendation and the evidence behind it."""

    sufficient_data: bool
    minimum_samples: int
    observed_samples: int
    recommended_threshold: Optional[float]
    recommended_metrics: Optional[ThresholdMetrics]
    candidates: tuple[ThresholdMetrics, ...]
    false_pass_cost: float
    false_failure_cost: float
    authority_preserved: bool = True
    enforcement_enabled: bool = False


def _validate_outcomes(
    outcomes: Sequence[LabeledOutcome], max_outcomes: int
) -> None:
    if max_outcomes < 1 or len(outcomes) > max_outcomes:
        raise ValueError(f"outcomes exceed max_outcomes={max_outcomes}")
    for outcome in outcomes:
        if not 0.0 <= outcome.probability <= 1.0:
            raise ValueError("probability must be between 0 and 1")


def evaluate_threshold(
    outcomes: Sequence[LabeledOutcome],
    threshold: float,
    *,
    false_pass_cost: float = 1.0,
    false_failure_cost: float = 1.0,
    max_outcomes: int = _DEFAULT_MAX_OUTCOMES,
) -> ThresholdMetrics:
    """Compute confusion metrics for ``probability >= threshold``."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if false_pass_cost < 0 or false_failure_cost < 0:
        raise ValueError("error costs must be non-negative")
    _validate_outcomes(outcomes, max_outcomes)

    true_passes = true_failures = false_passes = false_failures = 0
    for outcome in outcomes:
        predicted = outcome.probability >= threshold
        if predicted and outcome.label:
            true_passes += 1
        elif not predicted and not outcome.label:
            true_failures += 1
        elif predicted:
            false_passes += 1
        else:
            false_failures += 1
    negatives = false_passes + true_failures
    positives = true_passes + false_failures
    return ThresholdMetrics(
        threshold=threshold,
        sample_count=len(outcomes),
        true_passes=true_passes,
        true_failures=true_failures,
        false_passes=false_passes,
        false_failures=false_failures,
        false_pass_rate=false_passes / negatives if negatives else None,
        false_failure_rate=false_failures / positives if positives else None,
        weighted_loss=false_pass_cost * false_passes + false_failure_cost * false_failures,
    )


def recommend_threshold(
    outcomes: Sequence[LabeledOutcome],
    *,
    minimum_samples: int = _DEFAULT_MIN_OUTCOMES,
    false_pass_cost: float = 2.0,
    false_failure_cost: float = 1.0,
    max_outcomes: int = _DEFAULT_MAX_OUTCOMES,
) -> ThresholdRecommendation:
    """Return a conservative, shadow-only threshold recommendation.

    Candidate thresholds are the observed probabilities plus the endpoints.
    Ties choose the higher threshold, preferring fewer false passes.
    """
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be positive")
    if false_pass_cost < 0 or false_failure_cost < 0:
        raise ValueError("error costs must be non-negative")
    _validate_outcomes(outcomes, max_outcomes)
    thresholds = sorted({0.0, 1.0, *(item.probability for item in outcomes)})
    candidates = tuple(
        evaluate_threshold(
            outcomes,
            threshold,
            false_pass_cost=false_pass_cost,
            false_failure_cost=false_failure_cost,
            max_outcomes=max_outcomes,
        )
        for threshold in thresholds
    )
    sufficient = len(outcomes) >= minimum_samples
    chosen = (
        min(candidates, key=lambda item: (item.weighted_loss, -item.threshold))
        if sufficient
        else None
    )
    return ThresholdRecommendation(
        sufficient_data=sufficient,
        minimum_samples=minimum_samples,
        observed_samples=len(outcomes),
        recommended_threshold=chosen.threshold if chosen else None,
        recommended_metrics=chosen,
        candidates=candidates,
        false_pass_cost=false_pass_cost,
        false_failure_cost=false_failure_cost,
    )


__all__ = [
    "LabeledOutcome",
    "ThresholdMetrics",
    "ThresholdRecommendation",
    "evaluate_threshold",
    "recommend_threshold",
]
