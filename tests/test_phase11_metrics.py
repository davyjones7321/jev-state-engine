"""Phase 11 metrics use strong evidence and independently assessed outcomes."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jev.ledger import SqliteLedger
from jev.mech_evidence import MechanicalEvidence


def record(ledger, name, status="PASS", level="counted", reason=None, **kwargs):
    ledger.record_attempt(
        "run", name, "mechanical", kwargs.pop("outcome", "passed"),
        created_at="2026-11-02T12:00:00+00:00",
        mech_evidence=MechanicalEvidence(
            mech_status=status, reason=reason, evidence_level=level,
            language="python", runner_profile_id="pytest", registry_version="1",
            **kwargs,
        ),
    )


@pytest.fixture
def ledger(tmp_path):
    result = SqliteLedger(db_path=tmp_path / "ledger.db")
    result.record_run_start("run", "thread", "metrics")
    return result


def test_strong_rates_exclude_weak_unmeasured_and_outside_profile(ledger):
    record(ledger, "counted")
    record(ledger, "named", level="named")
    record(ledger, "weak", level="exit_only")
    record(ledger, "outside", command_identity={"preflight_status": "OUTSIDE_PROFILE"})
    record(ledger, "timeout", status="UNMEASURED", reason="TIMEOUT", level="none")
    record(ledger, "missing", status="UNMEASURED", reason="MISSING_TOOL", level="none")
    report = ledger.phase11_metrics_report()
    total = report["totals"]
    assert total["measured_rate_all"] == {"numerator": 2, "denominator": 6, "value": 2 / 6}
    assert total["measured_rate_eligible"] == {"numerator": 2, "denominator": 4, "value": .5}
    assert total["exit_only_success_count"] == 1
    assert total["tooling_uncertainty_rate"]["numerator"] == 3
    assert report["groups"][0]["platform"] == sys.platform


@pytest.mark.parametrize("reason", ["TIMEOUT", "TOOLING_ERROR", "FLAKY", "NO_COMMAND", "TEST_SURFACE_CHANGED", "COMMAND_IDENTITY_CHANGED"])
def test_every_unmeasured_reason_stays_out_of_strong_numerator(ledger, reason):
    record(ledger, "unmeasured", status="UNMEASURED", reason=reason, level="none")
    assert ledger.phase11_metrics_report()["totals"]["measured_rate_all"]["numerator"] == 0


def test_assessed_errors_and_safety_counts_are_not_inferred_from_jev(ledger):
    record(ledger, "reject", status="FAIL", outcome="mechanical_failure")
    record(ledger, "accept", verifier_modified=True)
    ledger.record_attempt_assessment(
        "run", "reject", 1, worker_should_accept=True, unsafe_commit_count=0, process_leak_count=2,
    )
    ledger.record_attempt_assessment(
        "run", "accept", 1, mechanical_should_accept=False, unsafe_commit_count=1, process_leak_count=0,
    )
    total = ledger.phase11_metrics_report()["totals"]
    assert total["false_worker_rejection_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert total["false_mechanical_acceptance_rate"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert total["unsafe_commit_count"] == 1
    assert total["process_leak_count"] == 2
    assert total["safety_assessed_attempts"] == 2
    assert total["verifier_modified_rate"]["numerator"] == 1


def test_unknown_assessments_remain_unknown_and_zero_denominators_safe(ledger):
    empty = ledger.phase11_metrics_report()["totals"]
    assert empty["measured_rate_all"] == {"numerator": 0, "denominator": 0, "value": None}
    record(ledger, "unknown")
    total = ledger.phase11_metrics_report()["totals"]
    assert total["false_worker_rejection_rate"]["value"] is None
    assert total["false_mechanical_acceptance_rate"]["value"] is None
    assert total["unsafe_commit_count"] is None
    assert total["process_leak_count"] is None


def test_group_dimensions_time_window_and_identity_fallback(ledger):
    record(ledger, "one")
    record(ledger, "two")
    ledger.record_attempt_assessment("run", "two", 1, platform="linux")
    ledger.record_attempt(
        "run", "other", "mechanical", "passed", created_at="2026-11-01T12:00:00Z",
        mech_evidence=MechanicalEvidence(
            mech_status="PASS", evidence_level="named", language="rust",
            command_identity={"profile_id": "cargo-test", "registry_version": "2", "platform": "linux"},
        ),
    )
    report = ledger.phase11_metrics_report(since="2026-11-02T00:00:00Z", until="2026-11-03T00:00:00Z")
    assert report["totals"]["attempts_all"] == 2
    assert len(report["groups"]) == (1 if sys.platform == "linux" else 2)
    other = next(g for g in ledger.phase11_metrics_report()["groups"] if g["language"] == "rust")
    assert (other["runner_profile"], other["registry_version"], other["platform"]) == ("cargo-test", "2", "linux")
    with pytest.raises(ValueError):
        ledger.phase11_metrics_report(since="not-a-date")
    with pytest.raises(ValueError):
        ledger.phase11_metrics_report(since="2026-11-03", until="2026-11-02")


def test_legacy_database_metrics_cli_is_read_only(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(Path("tests/fixtures/ledger_pre_step1.sql").read_text())
    before = db.read_bytes()
    result = subprocess.run(
        [sys.executable, "scripts/ledger_report.py", "--db-path", str(db), "--metrics"],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout)
    assert report["totals"]["attempts_all"] == 1
    assert report["totals"]["measured_rate_all"]["numerator"] == 0
    assert report["groups"][0]["platform"] is None
    assert db.read_bytes() == before


def test_assessments_validate_identity_and_values(ledger):
    with pytest.raises(ValueError):
        ledger.record_attempt_assessment("run", "absent", 1, process_leak_count=0)
    record(ledger, "one")
    with pytest.raises(ValueError):
        ledger.record_attempt_assessment("run", "one", 1, process_leak_count=-1)
    with pytest.raises(ValueError):
        ledger.record_attempt_assessment("run", "one", 1, worker_should_accept="yes")


def test_error_rate_denominators_and_partial_safety_coverage(ledger):
    record(ledger, "false-reject", status="FAIL", outcome="mechanical_failure")
    record(ledger, "true-reject", status="FAIL", outcome="semantic_failure")
    record(ledger, "false-accept")
    record(ledger, "true-accept")
    record(ledger, "unreviewed", outcome="mechanical_failure", status="FAIL")
    for name, correct in (("false-reject", True), ("true-reject", False)):
        ledger.record_attempt_assessment("run", name, 1, worker_should_accept=correct)
    for name, correct in (("false-accept", False), ("true-accept", True)):
        ledger.record_attempt_assessment("run", name, 1, mechanical_should_accept=correct)
    ledger.record_attempt_assessment("run", "true-accept", 1, unsafe_commit_count=0)
    total = ledger.phase11_metrics_report()["totals"]
    assert total["false_worker_rejection_rate"] == {"numerator": 1, "denominator": 2, "value": .5}
    assert total["false_mechanical_acceptance_rate"] == {"numerator": 1, "denominator": 2, "value": .5}
    assert total["unsafe_commit_count"] == 0
    assert total["unsafe_commit_assessed_attempts"] == 1
    assert total["process_leak_count"] is None
    assert total["safety_assessed_attempts"] == 0


def test_no_eligible_attempts_and_window_boundaries(ledger):
    record(ledger, "missing", status="UNMEASURED", reason="NO_COMMAND", level="none")
    total = ledger.phase11_metrics_report()["totals"]
    assert total["measured_rate_eligible"] == {"numerator": 0, "denominator": 0, "value": None}
    assert ledger.phase11_metrics_report(until="2026-11-02T12:00:00Z")["totals"]["attempts_all"] == 0
    assert ledger.phase11_metrics_report(since="2026-11-02T12:00:00Z")["totals"]["attempts_all"] == 1


def test_invalid_timestamps_and_malformed_identity_are_visible(ledger):
    record(ledger, "bad")
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE attempts SET created_at='invalid', mech_command_identity_json='invalid'")
    assert ledger.phase11_metrics_report()["totals"]["attempts_all"] == 1
    report = ledger.phase11_metrics_report(since="2026-11-01")
    assert report["excluded_invalid_timestamp_count"] == 1
    assert report["totals"]["attempts_all"] == 0


def test_metrics_cli_current_schema_and_invalid_window(ledger):
    record(ledger, "one")
    command = [sys.executable, "scripts/ledger_report.py", "--db-path", str(ledger.db_path), "--metrics"]
    result = subprocess.run(command + ["--since", "2026-11-02", "--until", "2026-11-03"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["totals"]["measured_rate_all"]["value"] == 1.0
    invalid = subprocess.run(command + ["--since", "bad"], capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "Error:" in invalid.stderr
