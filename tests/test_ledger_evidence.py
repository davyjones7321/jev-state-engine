import sqlite3

from jev.ledger import MAX_EVIDENCE_CHARS, NullRecorder, SqliteLedger


def test_evidence_fields_are_recorded_and_bounded(tmp_path):
    ledger = SqliteLedger(db_path=tmp_path / "ledger.db")
    ledger.record_run_start(
        "run-1", "thread-1", "ticket", worker_model="worker",
        provenance="deterministic", model_version="worker-v2",
        failure_evidence="run failure", retry_metadata={"retry": 1},
        tool_evidence=[{"name": "read_file", "path": "src/app.py"}],
        read_evidence=["src/app.py"], self_check_evidence={"passed": False},
        hypothesis_change={"changed": True},
    )
    ledger.record_attempt(
        "run-1", "sg-1", "mechanical", "mechanical_failure",
        failure_evidence={"stderr": "syntax error"}, provenance="deterministic",
        model_version="worker-v2", retry_number=2, evidence_added=True,
        hypothesis_changed=True, hypothesis_change={"from": "A", "to": "B"},
        tool_evidence=[{"name": "self_check"}], read_evidence=["tests/test_app.py"],
        self_check_evidence={"command": "pytest", "exit_code": 1},
        retry_metadata={"reason": "new evidence"},
    )
    ledger.record_attempt(
        "run-1", "sg-1", "mechanical", "mechanical_failure",
        failure_evidence="x" * (MAX_EVIDENCE_CHARS + 100),
    )

    with sqlite3.connect(ledger.db_path) as conn:
        run = conn.execute(
            "SELECT provenance, model_version, failure_evidence, retry_metadata, tool_evidence, read_evidence, self_check_evidence, hypothesis_change FROM runs"
        ).fetchone()
        attempt = conn.execute(
            "SELECT provenance, model_version, retry_number, evidence_added, hypothesis_changed, hypothesis_change, failure_evidence, tool_evidence, read_evidence, self_check_evidence, retry_metadata FROM attempts WHERE attempt_no=1"
        ).fetchone()
        bounded = conn.execute("SELECT failure_evidence FROM attempts WHERE attempt_no=2").fetchone()[0]

    assert run[0:2] == ("deterministic", "worker-v2")
    assert '"retry": 1' in run[3]
    assert attempt[:2] == ("deterministic", "worker-v2")
    assert attempt[2:5] == (2, 1, 1)
    assert '"from": "A"' in attempt[5]
    assert len(bounded) <= MAX_EVIDENCE_CHARS + len("\n[truncated]")


def test_evidence_columns_migrate_existing_database(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ticket TEXT)")
        conn.execute("CREATE TABLE attempts (run_id TEXT, subgoal_id TEXT, attempt_no INTEGER, tier TEXT, outcome TEXT, PRIMARY KEY(run_id, subgoal_id, attempt_no))")

    SqliteLedger(db_path=db)
    with sqlite3.connect(db) as conn:
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
    assert {"provenance", "model_version", "failure_evidence", "tool_evidence", "hypothesis_change"} <= run_columns
    assert {"provenance", "model_version", "retry_number", "evidence_added", "hypothesis_changed", "self_check_evidence"} <= attempt_columns


def test_null_recorder_accepts_evidence_without_side_effects():
    recorder = NullRecorder()
    recorder.record_run_start("r", "t", "ticket", provenance="inferred", tool_evidence=["read"])
    recorder.record_attempt("r", "s", "tier0", "failed", failure_evidence={"error": "x"}, hypothesis_changed=True)
    assert recorder.has_run("r") is False
