from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
import pytest

from jev.mech_evidence import (
    BaseStatus,
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
)
from jev.ledger import NullRecorder, SqliteLedger


# ---------------------------------------------------------------------------
# 1. Invariants a-e: Valid and invalid cases
# ---------------------------------------------------------------------------

def test_invariant_a_unmeasured_requires_reason():
    # Valid: UNMEASURED with reason
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TIMEOUT,
    )
    assert ev.mech_status == MechStatus.UNMEASURED
    assert ev.reason == MechReason.TIMEOUT

    # Invalid: UNMEASURED without reason
    with pytest.raises(ValueError, match="reason"):
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED)


def test_invariant_b_pass_and_fail_must_not_carry_reason():
    # Valid: PASS without reason
    ev_pass = MechanicalEvidence(mech_status=MechStatus.PASS)
    assert ev_pass.reason is None

    # Valid: FAIL without reason
    ev_fail = MechanicalEvidence(mech_status=MechStatus.FAIL)
    assert ev_fail.reason is None

    # Invalid: PASS with reason
    with pytest.raises(ValueError, match="reason"):
        MechanicalEvidence(mech_status=MechStatus.PASS, reason=MechReason.TIMEOUT)

    # Invalid: FAIL with reason
    with pytest.raises(ValueError, match="reason"):
        MechanicalEvidence(mech_status=MechStatus.FAIL, reason=MechReason.TOOLING_ERROR)


def test_invariant_c_unmeasured_forces_mechanically_unverified():
    # Valid: omitted/None is forced to True
    ev = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.TOOLING_ERROR,
    )
    assert ev.mechanically_unverified is True

    # Valid: explicitly True
    ev2 = MechanicalEvidence(
        mech_status=MechStatus.UNMEASURED,
        reason=MechReason.FLAKY,
        mechanically_unverified=True,
    )
    assert ev2.mechanically_unverified is True

    # Invalid: explicitly False
    with pytest.raises(ValueError, match="mechanically_unverified"):
        MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.TIMEOUT,
            mechanically_unverified=False,
        )


def test_invariant_d_evidence_level_exit_only_and_none():
    # When evidence_level is exit_only or none:
    for lvl in (EvidenceLevel.EXIT_ONLY, EvidenceLevel.NONE):
        # Forces full_suite_verified=False when omitted
        ev = MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=lvl,
        )
        assert ev.full_suite_verified is False
        assert ev.mechanically_unverified is True

        # Invalid: full_suite_verified=True
        with pytest.raises(ValueError, match="full_suite_verified"):
            MechanicalEvidence(
                mech_status=MechStatus.PASS,
                evidence_level=lvl,
                full_suite_verified=True,
            )

        # Invalid: mechanically_unverified=False when status is PASS
        with pytest.raises(ValueError, match="mechanically_unverified"):
            MechanicalEvidence(
                mech_status=MechStatus.PASS,
                evidence_level=lvl,
                mechanically_unverified=False,
            )

    # When evidence_level is counted or named, PASS can have full_suite_verified=True and mechanically_unverified=False
    ev_counted = MechanicalEvidence(
        mech_status=MechStatus.PASS,
        evidence_level=EvidenceLevel.COUNTED,
        full_suite_verified=True,
        mechanically_unverified=False,
    )
    assert ev_counted.full_suite_verified is True
    assert ev_counted.mechanically_unverified is False


def test_invariant_e_reason_values_outside_enum_rejected():
    # Random invalid string
    with pytest.raises(ValueError):
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason="NOT_A_VALID_REASON")

    # NOT_APPLICABLE is a BaseStatus, never a reason
    with pytest.raises(ValueError):
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason="NOT_APPLICABLE")

    with pytest.raises(ValueError):
        MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=BaseStatus.NOT_APPLICABLE)

    # Valid: all enum values
    for reason in MechReason:
        ev = MechanicalEvidence(mech_status=MechStatus.UNMEASURED, reason=reason)
        assert ev.reason == reason


# ---------------------------------------------------------------------------
# 2. Round trip test: write and read back identical
# ---------------------------------------------------------------------------

def test_round_trip_pass_fail_and_all_unmeasured_reasons(tmp_path):
    db_path = tmp_path / "round_trip.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "rt-run-1"
    ledger.record_run_start(run_id=run_id, thread_id="t1", ticket="RT test")

    test_cases = [
        MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=EvidenceLevel.NAMED,
            base_status=BaseStatus.PASS,
            comparison=Comparison.SAME,
            command_identity={"cmd": "pytest", "args": ["-q"]},
            target_scope={"kind": "file", "paths": ["src/app.py"], "new_target": False},
            target_rule="rule_1",
            target_confidence=0.95,
            runner_profile_id="pytest",
            registry_version="1.0.0",
            language="python",
            worker_staged_paths=["src/app.py"],
            engine_touched_paths=["tests/test_app.py"],
            test_authorship="user",
            test_surface_changed=False,
            verifier_modified=False,
            approval_valid=True,
            comparison_valid=True,
            mechanically_unverified=False,
            verification_inconclusive=False,
        ),
        MechanicalEvidence(
            mech_status=MechStatus.FAIL,
            evidence_level=EvidenceLevel.COUNTED,
            base_status=BaseStatus.PASS,
            comparison=Comparison.CANDIDATE_ONLY,
            language="python",
            runner_profile_id="pytest",
        ),
    ]

    for reason in MechReason:
        test_cases.append(
            MechanicalEvidence(
                mech_status=MechStatus.UNMEASURED,
                reason=reason,
                base_status=BaseStatus.NOT_APPLICABLE,
                language="python",
                runner_profile_id="pytest",
            )
        )

    for i, ev in enumerate(test_cases, start=1):
        subgoal_id = f"sg-{i}"
        ledger.record_attempt(
            run_id=run_id,
            subgoal_id=subgoal_id,
            tier="mechanical",
            outcome="passed" if ev.mech_status == MechStatus.PASS else "failed",
            mech_evidence=ev,
        )

        read_ev = ledger.get_attempt_mech_evidence(run_id, subgoal_id, 1)
        assert read_ev is not None
        assert read_ev == ev

        # UNMEASURED must never read back as PASS
        if ev.mech_status == MechStatus.UNMEASURED:
            assert read_ev.mech_status != MechStatus.PASS
            assert read_ev.mech_status == MechStatus.UNMEASURED


# ---------------------------------------------------------------------------
# 3. Old ledger migration test
# ---------------------------------------------------------------------------

def test_old_ledger_migration_and_null_mech_fields(tmp_path):
    sql_file = Path("tests/fixtures/ledger_pre_step1.sql")
    assert sql_file.exists(), "tests/fixtures/ledger_pre_step1.sql must exist"

    db_path = tmp_path / "old_ledger.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.close()

    # Open with new SqliteLedger -> runs migration
    ledger = SqliteLedger(db_path=db_path)

    # Verify old row exists unchanged
    old_ev = ledger.get_attempt_mech_evidence("run-pre-1", "sg-1", 1)
    assert old_ev is None, "Old rows must have NULL mech fields and return None"

    # Raw query confirms mech columns exist and are NULL
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM attempts WHERE run_id = 'run-pre-1'").fetchone()
    conn.close()

    assert row["diff"] == "+line"
    assert row["test_outcome"] == "PASSED"
    assert row["evidence_level"] == "tests-only"
    assert row["mech_status"] is None
    assert row["mech_reason"] is None
    assert row["mech_schema_version"] is None


# ---------------------------------------------------------------------------
# 4. Legacy callers without mech_evidence
# ---------------------------------------------------------------------------

def test_legacy_caller_without_mech_evidence(tmp_path):
    db_path = tmp_path / "legacy.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "leg-run"
    ledger.record_run_start(run_id=run_id, thread_id="t1", ticket="legacy test")

    ledger.record_attempt(
        run_id=run_id,
        subgoal_id="sg-leg",
        tier="mechanical",
        outcome="passed",
        diff="+diff",
        tests_ran=True,
        test_outcome="PASSED",
    )

    ev = ledger.get_attempt_mech_evidence(run_id, "sg-leg", 1)
    assert ev is None

    null_rec = NullRecorder()
    null_rec.record_attempt(
        run_id=run_id,
        subgoal_id="sg-leg",
        tier="mechanical",
        outcome="passed",
        mech_evidence=MechanicalEvidence(mech_status=MechStatus.PASS),
    )


# ---------------------------------------------------------------------------
# 5. measured_rate_report test
# ---------------------------------------------------------------------------

def test_measured_rate_report(tmp_path):
    db_path = tmp_path / "metrics.db"
    ledger = SqliteLedger(db_path=db_path)
    run_id = "metrics-run"
    ledger.record_run_start(run_id=run_id, thread_id="t1", ticket="metrics test")

    # 1. PASS (measured, eligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s1", tier="mechanical", outcome="passed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.PASS,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 2. PASS exit_only (measured, eligible, exit_only_pass)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s2", tier="mechanical", outcome="passed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.PASS,
            evidence_level=EvidenceLevel.EXIT_ONLY,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 3. FAIL (measured, eligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s3", tier="mechanical", outcome="failed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.FAIL,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 4. UNMEASURED with TIMEOUT (unmeasured, eligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s4", tier="mechanical", outcome="failed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.TIMEOUT,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 5. UNMEASURED with MISSING_TOOL (unmeasured, ineligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s5", tier="mechanical", outcome="failed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.MISSING_TOOL,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 6. UNMEASURED with NO_COMMAND (unmeasured, ineligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s6", tier="mechanical", outcome="failed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.NO_COMMAND,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 7. UNMEASURED with ENVIRONMENT_UNAVAILABLE (unmeasured, ineligible)
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s7", tier="mechanical", outcome="failed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=MechReason.ENVIRONMENT_UNAVAILABLE,
            language="python",
            runner_profile_id="pytest",
        ),
    )

    # 8. Another language/runner: rust / cargo
    ledger.record_attempt(
        run_id=run_id, subgoal_id="s8", tier="mechanical", outcome="passed",
        mech_evidence=MechanicalEvidence(
            mech_status=MechStatus.PASS,
            language="rust",
            runner_profile_id="cargo",
        ),
    )

    report = ledger.measured_rate_report(since_days=90)

    py_key = ("python", "pytest")
    assert py_key in report
    py_row = report[py_key]

    assert py_row["attempts_all"] == 7
    assert py_row["measured"] == 3
    assert py_row["unmeasured"] == 4
    # Eligible excludes MISSING_TOOL, NO_COMMAND, ENVIRONMENT_UNAVAILABLE (3 excluded -> 4 eligible)
    assert py_row["attempts_eligible"] == 4
    assert py_row["exit_only_pass_count"] == 1
    assert abs(py_row["measured_rate_all"] - (3 / 7)) < 1e-6
    assert abs(py_row["measured_rate_eligible"] - (3 / 4)) < 1e-6

    # Test NullRecorder returns empty report
    null_rec = NullRecorder()
    assert null_rec.measured_rate_report() == {}


def test_measured_rate_report_zero_division():
    # Helper to test division by zero gives None
    from jev.ledger import _compute_rates
    rate_all, rate_eligible = _compute_rates(0, 0, 0)
    assert rate_all is None
    assert rate_eligible is None


# ---------------------------------------------------------------------------
# 6. Idempotent migration
# ---------------------------------------------------------------------------

def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.db"
    # Open once
    ledger1 = SqliteLedger(db_path=db_path)
    # Open twice
    ledger2 = SqliteLedger(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()]
    conn.close()

    assert "mech_status" in cols
    # Column appears exactly once
    assert cols.count("mech_status") == 1
