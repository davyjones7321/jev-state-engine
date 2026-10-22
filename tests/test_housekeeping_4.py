import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

import main
from jev.dependency_delta import compute_dependency_delta, format_dependency_summary
from jev.engine import JevEngine, node_investigate
from jev.ledger import SqliteLedger
from jev.models import State, Subgoal, TestOutcome, ValidationVerdict
from jev.workspace import Workspace, _run_test_subprocess


class ScriptedChatModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.invocations = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = tools
        return self

    def invoke(self, messages, **kwargs):
        self.invocations.append(messages)
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("No more scripted responses.")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    f = repo / "README.md"
    f.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# Part 1: Run-level dependency delta when nothing changed
# ==============================================================================

def test_completed_run_identical_commits_stores_empty_delta_and_no_changes_summary(clean_repo, tmp_path):
    """When a run completes and final_commit == base_commit, store empty delta JSON and summary 'Dependencies: no changes'."""
    db_file = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_file)
    ws = Workspace(repo_dir=clean_repo)
    head_commit = ws.base_commit

    engine = JevEngine(
        workspace=ws,
        recorder=ledger,
        db_path=tmp_path / "checkpoints.db",
    )

    # Mock the compiled LangGraph app to simulate a completed run where no commit was made (final_commit == base_commit)
    mock_app = MagicMock()
    mock_app.invoke.return_value = {
        "status": "completed",
        "gate_status": "verified",
        "investigation_notes": "All good.",
    }
    engine.app = mock_app

    final_state = engine.execute(ticket="No-op ticket", thread_id="thread-noop")
    run_id = final_state.get("run_id", "thread-noop")

    with sqlite3.connect(str(db_file)) as conn:
        row = conn.execute("SELECT status, dependency_delta FROM runs WHERE thread_id = 'thread-noop'").fetchone()
    assert row is not None
    status, delta_json = row
    assert status == "completed"

    expected_empty_delta = compute_dependency_delta({}).to_json()
    assert delta_json == expected_empty_delta
    assert format_dependency_summary(delta_json) == "Dependencies: no changes"


def test_escalated_run_stores_null_delta_and_not_computed_summary(clean_repo, tmp_path):
    """When a run escalates, store NULL delta and summary 'Dependencies: not computed'."""
    db_file = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_file)
    ws = Workspace(repo_dir=clean_repo)

    engine = JevEngine(
        workspace=ws,
        recorder=ledger,
        db_path=tmp_path / "checkpoints.db",
    )

    mock_app = MagicMock()
    mock_app.invoke.return_value = {
        "status": "escalated",
        "gate_status": "mechanical_failure",
    }
    engine.app = mock_app

    final_state = engine.execute(ticket="Escalated ticket", thread_id="thread-esc")

    with sqlite3.connect(str(db_file)) as conn:
        row = conn.execute("SELECT status, dependency_delta FROM runs WHERE thread_id = 'thread-esc'").fetchone()
    assert row is not None
    status, delta_json = row
    assert status == "escalated"
    assert delta_json is None
    assert format_dependency_summary(delta_json) == "Dependencies: not computed"


# ==============================================================================
# Part 2: Remove test-double coupling in node_investigate
# ==============================================================================

def test_engine_honors_max_investigation_turns_parameter(clean_repo, tmp_path):
    """JevEngine accepts max_investigation_turns and passes it to node_investigate."""
    # Test with custom N = 3
    endless_responses_3 = [
        AIMessage(
            content=f"Turn {i}",
            tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": f"c_{i}"}],
        )
        for i in range(10)
    ]
    fake_llm_3 = ScriptedChatModel(responses=endless_responses_3)
    ws = Workspace(repo_dir=clean_repo)

    engine_3 = JevEngine(
        workspace=ws,
        llm=fake_llm_3,
        db_path=tmp_path / "cp3.db",
        max_investigation_turns=3,
    )
    assert engine_3.max_investigation_turns == 3

    state_3: State = {"ticket": "Investigate ticket", "trajectory": []}
    out_state_3 = engine_3._node_investigate(state_3)
    assert len(fake_llm_3.invocations) == 3


def test_engine_default_max_investigation_turns_is_20(clean_repo, tmp_path):
    """JevEngine defaults max_investigation_turns to 20."""
    ws = Workspace(repo_dir=clean_repo)
    engine_default = JevEngine(
        workspace=ws,
        db_path=tmp_path / "cp_default.db",
    )
    assert engine_default.max_investigation_turns == 20

    # Test with 25 responses, should cap at 20
    endless_responses_25 = [
        AIMessage(
            content=f"Turn {i}",
            tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": f"c_{i}"}],
        )
        for i in range(25)
    ]
    fake_llm_25 = ScriptedChatModel(responses=endless_responses_25)
    engine_default.llm = fake_llm_25

    state: State = {"ticket": "Investigate ticket", "trajectory": []}
    out_state = engine_default._node_investigate(state)
    assert len(fake_llm_25.invocations) == 20


# ==============================================================================
# Part 3: Pin test-output decoding
# ==============================================================================

def test_run_test_subprocess_decodes_replacement_character_on_invalid_utf8(tmp_path):
    """_run_test_subprocess replaces invalid UTF-8 bytes with U+FFFD in stdout and stderr without exception."""
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe bad\\n'); sys.stderr.buffer.write(b'err \\xc3\\x28\\n')",
    ]
    res = _run_test_subprocess(cmd, cwd=tmp_path, timeout=10)
    assert res.returncode == 0
    assert "\ufffd" in res.stdout
    assert "\ufffd" in res.stderr
    assert "ok " in res.stdout and " bad" in res.stdout
    assert "err " in res.stderr


# ==============================================================================
# Part 4: Worker model configuration in src/main.py
# ==============================================================================

def test_worker_model_default_equals_hardcoded_value():
    """Default worker model equals gemini-3.5-flash-lite."""
    assert main.DEFAULT_WORKER_MODEL == "gemini-3.5-flash-lite"
    with patch.dict(os.environ, {}, clear=True):
        assert main.resolve_worker_model(None) == "gemini-3.5-flash-lite"


def test_worker_model_precedence_cli_beats_env_beats_default():
    """Worker model resolution precedence: CLI > JEV_WORKER_MODEL > DEFAULT_WORKER_MODEL."""
    # 1. Default (no CLI, no env)
    with patch.dict(os.environ, {}, clear=True):
        assert main.resolve_worker_model(None) == main.DEFAULT_WORKER_MODEL

    # 2. Env beats default
    with patch.dict(os.environ, {"JEV_WORKER_MODEL": "gemini-1.5-pro"}):
        assert main.resolve_worker_model(None) == "gemini-1.5-pro"

    # 3. CLI beats env
    with patch.dict(os.environ, {"JEV_WORKER_MODEL": "gemini-1.5-pro"}):
        assert main.resolve_worker_model("gemini-custom-cli") == "gemini-custom-cli"

    # 4. CLI beats default without env
    with patch.dict(os.environ, {}, clear=True):
        assert main.resolve_worker_model("gemini-custom-cli") == "gemini-custom-cli"


def test_worker_model_empty_or_whitespace_rejected_with_argparse_error():
    """Empty or whitespace-only model value is rejected with an argparse error."""
    parser = main.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["ticket", "--model", ""])

    with pytest.raises(SystemExit):
        parser.parse_args(["ticket", "--model", "   "])

    with patch.dict(os.environ, {"JEV_WORKER_MODEL": ""}):
        with pytest.raises(SystemExit):
            main.main(["ticket"])

    with patch.dict(os.environ, {"JEV_WORKER_MODEL": "   "}):
        with pytest.raises(SystemExit):
            main.main(["ticket"])


def test_worker_model_reaches_chat_google_generative_ai_constructor(clean_repo):
    """The resolved model name reaches the ChatGoogleGenerativeAI constructor."""
    parser = main.build_parser()
    with patch("langchain_google_genai.ChatGoogleGenerativeAI") as mock_chat_cls, \
         patch("main.JevEngine") as mock_engine_cls, \
         patch("main.Gatekeeper") as mock_gatekeeper_cls:
        mock_engine_instance = MagicMock()
        mock_engine_instance.execute.return_value = {"status": "completed"}
        mock_engine_cls.return_value = mock_engine_instance

        # Decouple from developer environment: JEV_API_URL and JEV_API_KEY are unset
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key", "JEV_WORKER_MODEL": "model-from-env"}):
            os.environ.pop("JEV_API_URL", None)
            os.environ.pop("JEV_API_KEY", None)

            # 1. Env model
            main.main(["ticket-test", "--repo-dir", str(clean_repo), "--no-ledger"])
            mock_chat_cls.assert_called_with(
                model="model-from-env",
                google_api_key="fake-key",
            )

            # 2. CLI model overrides env model
            mock_chat_cls.reset_mock()
            main.main(["ticket-test", "--repo-dir", str(clean_repo), "--no-ledger", "--model", "model-from-cli"])
            mock_chat_cls.assert_called_with(
                model="model-from-cli",
                google_api_key="fake-key",
            )
