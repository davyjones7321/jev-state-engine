"""Regression checks for Phase 12A review of Phase 11 metrics."""

from jev.ledger import SqliteLedger
from jev.mech_evidence import EvidenceLevel, MechanicalEvidence, MechStatus


def test_unknown_runner_profile_is_not_strong_measured_evidence(tmp_path):
    ledger = SqliteLedger(db_path=tmp_path / "ledger.db")
    ledger.record_run_start("run", "thread", "unknown profile")
    ledger.record_attempt(
        "run",
        "sg-1",
        "mechanical",
        "passed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=EvidenceLevel.COUNTED,
            language="python",
        ),
    )

    totals = ledger.phase11_metrics_report()["totals"]

    assert totals["measured_rate_all"] == {"numerator": 0, "denominator": 1, "value": 0.0}
    assert totals["measured_rate_eligible"] == {"numerator": 0, "denominator": 0, "value": None}
    assert totals["tooling_uncertainty_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
