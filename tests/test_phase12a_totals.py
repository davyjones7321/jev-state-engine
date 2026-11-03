"""Step 3b: Phase 11 assessed metrics also expose their total coverage."""

import json
import subprocess
import sys

import pytest

from jev.ledger import SqliteLedger
from jev.mech_evidence import MechanicalEvidence


@pytest.fixture
def ledger(tmp_path):
    result = SqliteLedger(db_path=tmp_path / "ledger.db")
    result.record_run_start("run", "thread", "totals")
    return result


def _record(ledger, subgoal_id, *, status="PASS", outcome="passed"):
    ledger.record_attempt(
        "run",
        subgoal_id,
        "mechanical",
        outcome,
        created_at="2026-11-02T12:00:00+00:00",
        mech_evidence=MechanicalEvidence(
            mech_status=status,
            evidence_level="counted",
            language="python",
            runner_profile_id="pytest",
            registry_version="1",
        ),
    )


def test_totals_groups_and_metrics_cli_show_assessment_coverage(ledger):
    _record(ledger, "reject-one", status="FAIL", outcome="mechanical_failure")
    _record(ledger, "reject-two", status="FAIL", outcome="rejected")
    _record(ledger, "accept-one")
    _record(ledger, "accept-two")
    ledger.record_attempt_assessment("run", "reject-one", 1, worker_should_accept=True)
    ledger.record_attempt_assessment("run", "accept-one", 1, mechanical_should_accept=False)

    report = ledger.phase11_metrics_report()
    total = report["totals"]
    assert total["worker_rejections_total"] == 2
    assert total["worker_rejections_assessed"] == 1
    assert total["false_worker_rejection_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert total["mechanical_acceptances_total"] == 2
    assert total["mechanical_acceptances_assessed"] == 1
    assert total["false_mechanical_acceptance_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}

    group = report["groups"][0]
    assert group["worker_rejections_total"] == 2
    assert group["mechanical_acceptances_total"] == 2

    result = subprocess.run(
        [sys.executable, "scripts/ledger_report.py", "--db-path", str(ledger.db_path), "--metrics"],
        capture_output=True,
        text=True,
        check=True,
    )
    cli_report = json.loads(result.stdout)
    assert cli_report["totals"]["worker_rejections_total"] == 2
    assert cli_report["totals"]["mechanical_acceptances_total"] == 2
    assert cli_report["groups"][0]["worker_rejections_total"] == 2
    assert cli_report["groups"][0]["mechanical_acceptances_total"] == 2


def test_empty_ledger_has_zero_coverage_totals(ledger):
    total = ledger.phase11_metrics_report()["totals"]
    assert total["worker_rejections_total"] == 0
    assert total["mechanical_acceptances_total"] == 0
