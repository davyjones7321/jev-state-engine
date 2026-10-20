import pytest

from jev.threshold_policy import LabeledOutcome, evaluate_threshold, recommend_threshold


def test_threshold_metrics_report_false_passes_and_failures():
    outcomes = [
        LabeledOutcome(0.9, True),
        LabeledOutcome(0.8, False),
        LabeledOutcome(0.2, True),
        LabeledOutcome(0.1, False),
    ]

    metrics = evaluate_threshold(outcomes, 0.5, false_pass_cost=3, false_failure_cost=2)

    assert (metrics.true_passes, metrics.true_failures) == (1, 1)
    assert (metrics.false_passes, metrics.false_failures) == (1, 1)
    assert metrics.false_pass_rate == pytest.approx(0.5)
    assert metrics.false_failure_rate == pytest.approx(0.5)
    assert metrics.weighted_loss == 5


def test_recommendation_is_withheld_until_minimum_data():
    report = recommend_threshold([LabeledOutcome(0.9, True)], minimum_samples=2)

    assert report.sufficient_data is False
    assert report.recommended_threshold is None
    assert report.recommended_metrics is None


def test_recommendation_makes_false_pass_tradeoff_explicit():
    outcomes = [LabeledOutcome(0.9, False), LabeledOutcome(0.6, True)]

    report = recommend_threshold(
        outcomes, minimum_samples=1, false_pass_cost=10, false_failure_cost=1
    )

    assert report.recommended_threshold == 1.0
    assert report.false_pass_cost == 10
    assert report.false_failure_cost == 1


def test_bounds_reject_invalid_probabilities_and_oversized_sets():
    with pytest.raises(ValueError, match="probability"):
        evaluate_threshold([LabeledOutcome(1.1, True)], 0.5)
    with pytest.raises(ValueError, match="max_outcomes"):
        recommend_threshold([LabeledOutcome(0.5, True)], max_outcomes=0)


def test_report_preserves_jev_authority_and_never_enables_enforcement():
    report = recommend_threshold([LabeledOutcome(0.8, True)], minimum_samples=1)

    assert report.authority_preserved is True
    assert report.enforcement_enabled is False
