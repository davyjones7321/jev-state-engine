"""Tests for engine token usage recording, ledger migration, and reporting."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from jev.ledger import SqliteLedger
from jev.usage import UsageRecorder
import scripts.ledger_report as lr


# ---------------------------------------------------------------------------
# 1. Fake LLM with usage_metadata gives the expected totals
# ---------------------------------------------------------------------------
def test_fake_llm_with_usage_metadata_accumulates_expected_totals():
    recorder = UsageRecorder()

    msg1 = AIMessage(
        content="plan response",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 45,
            "total_tokens": 165,
            "input_token_details": {"cache_read": 30},
        },
    )
    msg2 = AIMessage(
        content="worker response",
        usage_metadata={
            "input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
            "input_token_details": {"cache_read": 10},
        },
    )

    fake_llm = GenericFakeChatModel(messages=iter([msg1, msg2]), callbacks=[recorder])

    res1 = fake_llm.invoke("investigate and plan")
    assert res1.content == "plan response"

    res2 = fake_llm.invoke("implement subgoal")
    assert res2.content == "worker response"

    assert recorder.llm_calls == 2
    assert recorder.calls == 2
    assert recorder.llm_tokens_in == 200
    assert recorder.tokens_in == 200
    assert recorder.llm_tokens_out == 65
    assert recorder.tokens_out == 65
    assert recorder.llm_cache_read_tokens == 40
    assert recorder.cache_read_tokens == 40


# ---------------------------------------------------------------------------
# 2. Exception inside the recorder does not break the call
# ---------------------------------------------------------------------------
def test_exception_inside_recorder_does_not_break_call():
    class CrashingRecorder(UsageRecorder):
        def _accumulate_usage(self, usage):
            raise RuntimeError("Forced internal explosion inside recorder!")

    crashing_recorder = CrashingRecorder()

    msg = AIMessage(
        content="safe output",
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 60,
            "input_token_details": {"cache_read": 5},
        },
    )

    fake_llm = GenericFakeChatModel(messages=iter([msg]), callbacks=[crashing_recorder])

    # Invoke must succeed despite the exception in the recorder
    res = fake_llm.invoke("do something risky")
    assert res.content == "safe output"
    assert crashing_recorder.calls == 1

    # Direct invocation with poison object also must never raise
    poison_recorder = UsageRecorder()
    poison_recorder.on_llm_end(object())
    poison_recorder.on_llm_end(None)
    assert poison_recorder.calls == 2


# ---------------------------------------------------------------------------
# 3. Migration works on an old DB
# ---------------------------------------------------------------------------
def test_migration_works_on_old_db(tmp_path):
    db_path = tmp_path / "old_ledger.db"

    # Create an old schema runs table without the 4 new columns
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE runs (
              run_id TEXT PRIMARY KEY,
              thread_id TEXT,
              ticket TEXT,
              base_commit TEXT,
              final_commit TEXT,
              engine_commit TEXT,
              worker_model TEXT,
              test_policy TEXT,
              retry_intel TEXT,
              repo_profile TEXT,
              profile_chars INTEGER,
              evidence_level TEXT,
              status TEXT,
              started_at TEXT,
              ended_at TEXT,
              notes_chars INTEGER,
              dependency_delta TEXT,
              dep_delta_jev TEXT DEFAULT NULL,
              provenance TEXT,
              model_version TEXT,
              failure_evidence TEXT,
              retry_metadata TEXT,
              tool_evidence TEXT,
              read_evidence TEXT,
              self_check_evidence TEXT,
              hypothesis_change TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO runs (run_id, thread_id, ticket, status)
            VALUES ('run-legacy', 'th-legacy', 'legacy ticket', 'completed');
            """
        )
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        assert "llm_tokens_in" not in old_cols
        assert "llm_tokens_out" not in old_cols
        assert "llm_cache_read_tokens" not in old_cols
        assert "llm_calls" not in old_cols

    # Opening with SqliteLedger triggers guarded migration
    ledger = SqliteLedger(db_path=db_path)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        migrated_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        assert "llm_tokens_in" in migrated_cols
        assert "llm_tokens_out" in migrated_cols
        assert "llm_cache_read_tokens" in migrated_cols
        assert "llm_calls" in migrated_cols

        row = conn.execute("SELECT * FROM runs WHERE run_id = 'run-legacy'").fetchone()
        assert row["status"] == "completed"
        assert row["ticket"] == "legacy ticket"
        assert row["llm_tokens_in"] is None
        assert row["llm_tokens_out"] is None
        assert row["llm_cache_read_tokens"] is None
        assert row["llm_calls"] is None

    # Writing usage to migrated row succeeds
    ledger.record_run_llm_usage(
        run_id="run-legacy",
        llm_tokens_in=500,
        llm_tokens_out=150,
        llm_cache_read_tokens=50,
        llm_calls=3,
    )

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id = 'run-legacy'").fetchone()
        assert row["llm_tokens_in"] == 500
        assert row["llm_tokens_out"] == 150
        assert row["llm_cache_read_tokens"] == 50
        assert row["llm_calls"] == 3


# ---------------------------------------------------------------------------
# 4. ledger_report prints the new lines
# ---------------------------------------------------------------------------
def test_ledger_report_prints_new_lines(tmp_path, capsys):
    db_path = tmp_path / "report_ledger.db"
    ledger = SqliteLedger(db_path=db_path)

    run_id = "run-report-usage"
    ledger.record_run_start(
        run_id=run_id,
        thread_id="th-report",
        ticket="Inspect token report",
        base_commit="abc1234",
        engine_commit="def5678",
        worker_model="gemini-flash",
        test_policy="auto",
    )
    # Add attempt with worker tokens (300 in + 100 out = 400 worker tokens)
    ledger.record_attempt(
        run_id=run_id,
        subgoal_id="sg-1",
        tier="worker",
        outcome="passed",
        tokens_in=300,
        tokens_out=100,
    )
    # Total LLM call usage: 1000 in, 250 out, 100 cache-read, 4 calls (total = 1250)
    # Unattributed = 1250 - 400 = 850
    ledger.record_run_llm_usage(
        run_id=run_id,
        llm_tokens_in=1000,
        llm_tokens_out=250,
        llm_cache_read_tokens=100,
        llm_calls=4,
    )
    ledger.record_run_end(run_id=run_id, final_status="completed")

    exit_code = lr.main(["--db-path", str(db_path), "--run-id", run_id])
    assert exit_code == 0

    captured = capsys.readouterr().out
    assert "LLM Tokens In:      1000" in captured
    assert "LLM Tokens Out:     250" in captured
    assert "LLM Cache Read:     100" in captured
    assert "LLM Calls:          4" in captured
    assert "LLM Total:          1250" in captured
    assert "Worker Tokens:      400" in captured
    assert (
        "unattributed (planner, investigation, other) = llm total minus sum of per-attempt worker tokens: 850"
        in captured
    )


# ---------------------------------------------------------------------------
# 5. A test fails if the recorder is not attached in main.py
# ---------------------------------------------------------------------------
def test_main_attaches_recorder_to_chat_model(tmp_path, monkeypatch):
    from main import main

    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-google-api-key")
    monkeypatch.setenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
    monkeypatch.setenv("JEV_API_KEY", "fake-jev-api-key")

    fake_chat_instance = MagicMock()
    mock_engine_instance = MagicMock()
    mock_engine_instance.execute.return_value = {
        "status": "completed",
        "run_id": "test-run-123",
    }

    with patch(
        "langchain_google_genai.ChatGoogleGenerativeAI",
        return_value=fake_chat_instance,
    ) as mock_chat_cls, patch("main.JevEngine", return_value=mock_engine_instance), patch("main.Gatekeeper"):
        exit_code = main(["Implement something", "--no-ledger"])
        assert exit_code == 0

        # Assert ChatGoogleGenerativeAI was initialized
        assert mock_chat_cls.called, "ChatGoogleGenerativeAI was not instantiated"
        call_kwargs = mock_chat_cls.call_args.kwargs

        # Mechanical enforcement: callbacks must be present and contain UsageRecorder
        callbacks = call_kwargs.get("callbacks")
        assert callbacks is not None, "callbacks argument was omitted"
        assert any(
            isinstance(cb, UsageRecorder) for cb in callbacks
        ), f"UsageRecorder was not attached in callbacks: {callbacks}"


def test_fails_if_recorder_not_attached():
    """Verify that if a chat constructor call lacks UsageRecorder, assertion fails."""
    call_kwargs_missing_callbacks = {"model": "gemini-test"}
    call_kwargs_empty_callbacks = {"model": "gemini-test", "callbacks": []}

    with pytest.raises(AssertionError):
        callbacks = call_kwargs_missing_callbacks.get("callbacks")
        assert callbacks is not None, "callbacks argument was omitted"
        assert any(isinstance(cb, UsageRecorder) for cb in callbacks)

    with pytest.raises(AssertionError):
        callbacks = call_kwargs_empty_callbacks.get("callbacks")
        assert callbacks is not None, "callbacks argument was omitted"
        assert any(isinstance(cb, UsageRecorder) for cb in callbacks)
