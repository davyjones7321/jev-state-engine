import pytest

from jev.calibration import CalibrationCase, CalibrationEvaluator
from jev.models import ValidationVerdict


def test_calibration_reports_metrics_and_provenance():
    cases = [
        CalibrationCase("pass", {"value": 1}, True, {"source": "fixture", "commit": "abc"}),
        CalibrationCase("false-pass", {"value": 2}, False, {"source": "ledger"}),
        CalibrationCase("false-reject", {"value": 3}, True, {"source": "fixture"}),
    ]
    jev = {1: True, 2: False, 3: True}
    replacement = {1: True, 2: True, 3: False}

    report = CalibrationEvaluator(
        lambda item: ValidationVerdict(valid=jev[item["value"]]),
        lambda item: replacement[item["value"]],
    ).evaluate(cases)

    assert report.total_cases == 3
    assert report.evaluated_cases == 3
    assert report.agreement_count == 1
    assert report.agreement_rate == pytest.approx(1 / 3)
    assert report.false_pass_count == 1
    assert report.false_reject_count == 1
    assert dict(report.confusion_matrix) == {"tp": 1, "tn": 0, "fp": 1, "fn": 1}
    assert dict(report.jev_confusion_matrix) == {"tp": 1, "tn": 0, "fp": 1, "fn": 1}
    assert report.cases[1].provenance == {"source": "ledger"}


def test_replacement_errors_are_non_authoritative_and_recorded():
    report = CalibrationEvaluator(lambda _: True, lambda _: 1).evaluate(
        [CalibrationCase("bad", "input", True, {"source": "x"})]
    )

    assert report.evaluated_cases == 0
    assert report.agreement_count == 0
    assert report.agreement_rate is None
    assert report.cases[0].jev_valid is True
    assert report.cases[0].replacement_valid is None
    assert report.cases[0].error.startswith("replacement:")


def test_calibration_bounds_are_enforced():
    evaluator = CalibrationEvaluator(lambda _: True, lambda _: False, max_cases=1)
    with pytest.raises(ValueError, match="max_cases"):
        evaluator.evaluate([
            CalibrationCase("one", None, True),
            CalibrationCase("two", None, False),
        ])


def test_provenance_and_errors_are_bounded():
    report = CalibrationEvaluator(
        lambda _: True,
        lambda _: (_ for _ in ()).throw(RuntimeError("x" * 2_000)),
        max_provenance_items=1,
    ).evaluate(
        [CalibrationCase("case", None, True, {"source": "s" * 2_000, "extra": "ignored"})]
    )

    assert list(report.cases[0].provenance) == ["source"]
    assert len(report.cases[0].provenance["source"]) == 500
    assert len(report.cases[0].error) <= 520
