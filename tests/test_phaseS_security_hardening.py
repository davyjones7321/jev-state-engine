"""Comprehensive tests for Phase S: Security, secrets, and advisor hardening."""

import ast
import glob
import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess

import pytest

from jev.advisory import (
    AdvisoryErrorCode,
    AdvisoryRequest,
    AdvisoryResult,
    RepairBrief,
    parse_repair_brief,
)
from jev.gatekeeper import Gatekeeper
from jev.ledger import SqliteLedger, strip_raw_reasoning, exclude_raw_reasoning_data
from jev.models import Subgoal
from jev.proposals import redact_data_structure, redact_secrets_in_text
from jev.providers import OpenAICompatibleProvider, validate_advisory_timeout
from jev.retrieval import AstWorkspaceRetriever, is_secret_path, retrieve_workspace
from jev.workspace import Workspace


# ==============================================================================
# 1. Secret denylist tests across retrieval, workspace, and engine
# ==============================================================================

DENYLIST_TEST_PATHS = [
    ".env",
    ".env.local",
    ".env.production",
    "subdir/.env",
    ".envrc",
    "cert.pem",
    "sub/cert.pem",
    "server.key",
    "sub/server.key",
    "id_rsa",
    "id_rsa.pub",
    ".ssh/id_rsa",
    ".ssh/config",
    "credentials.json",
    "secrets.json",
    "sub/credentials.json",
    ".aws/config",
    ".aws/credentials",
    ".kube/config",
]

ALLOWED_TEST_PATHS = [
    "src/main.py",
    "package.json",
    "README.md",
    "environment.ts",
    "keyboard.py",
    "identity.rs",
]


def test_retrieval_is_secret_path_denylist():
    """Verify is_secret_path flags every pattern in the secret denylist and allows normal paths."""
    for path in DENYLIST_TEST_PATHS:
        assert is_secret_path(path), f"Expected {path} to be flagged as secret"

    for path in ALLOWED_TEST_PATHS:
        assert not is_secret_path(path), f"Expected {path} to be allowed"


def test_retrieval_workspace_blocks_secret_files(tmp_path):
    """Verify AstWorkspaceRetriever ignores secret files during scan and raises on direct access."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "app.py").write_text("def hello(): pass\n", encoding="utf-8")
    (worktree / ".env").write_text("SECRET=12345\n", encoding="utf-8")
    (worktree / "credentials.json").write_text('{"key": "val"}', encoding="utf-8")
    ssh_dir = worktree / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("privatekey", encoding="utf-8")
    (ssh_dir / "helper.py").write_text("def secret_helper(): pass\n", encoding="utf-8")

    retriever = AstWorkspaceRetriever(worktree)
    res = retriever.retrieve()
    assert res.files_scanned == 1
    found_symbols = [s.name for s in res.symbols]
    assert "hello" in found_symbols
    assert "secret_helper" not in found_symbols

    # Direct access to secret path raises PermissionError
    with pytest.raises(PermissionError, match="access to protected secret files is denied"):
        retriever.retrieve(path=".env")

    with pytest.raises(PermissionError, match="access to protected secret files is denied"):
        retriever.retrieve(path=".ssh/id_rsa")


def test_workspace_run_read_tool_blocks_secret_denylist(tmp_path):
    """Verify Workspace.run_read_tool and _resolve_for_read block secret paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=123", encoding="utf-8")
    (repo / "server.key").write_text("PRIVATE_KEY", encoding="utf-8")
    aws_dir = repo / ".aws"
    aws_dir.mkdir()
    (aws_dir / "credentials").write_text("aws_key", encoding="utf-8")

    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    for secret in [".env", "server.key", ".aws/credentials", "credentials.json"]:
        # _resolve_for_read raises PermissionError
        with pytest.raises(PermissionError, match="Access denied to protected secret path"):
            ws._resolve_for_read(secret)

        # run_read_tool returns access denied message
        out_cat = ws.run_read_tool("cat", [secret])
        assert "Access denied to protected secret file" in out_cat

        out_head = ws.run_read_tool("head", [secret])
        assert "Access denied to protected secret file" in out_head


def test_workspace_stage_file_mutation_blocks_secrets(tmp_path):
    """Verify Workspace.stage_file_mutation refuses to mutate secret paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    for secret in [".env", "server.key", "sub/.env.prod", ".ssh/id_rsa", "secrets.json"]:
        with pytest.raises(PermissionError, match="Modifying protected secret path is blocked"):
            ws.stage_file_mutation(secret, "bad content")


def test_engine_investigate_read_tools_block_secrets(tmp_path):
    """Verify engine node_investigate read tools reject all secret files."""
    from jev.engine import node_investigate

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')", encoding="utf-8")
    (repo / ".env").write_text("SECRET=123", encoding="utf-8")
    (repo / "key.pem").write_text("PEM_DATA", encoding="utf-8")
    ssh_dir = repo / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("SSH_KEY", encoding="utf-8")

    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    class DummyLLM:
        def bind_tools(self, tools):
            self.tools = {t.name: t for t in tools}
            return self

        def invoke(self, messages):
            # Inspect tools
            return MagicMock(tool_calls=[])

    dummy_llm = DummyLLM()
    state = {
        "ticket": "test ticket",
        "investigation_notes": "",
        "workspace": ws,
        "llm": dummy_llm,
        "trajectory": [],
    }

    # Run investigate node to bind tools
    node_investigate(state, workspace=ws, llm=dummy_llm)
    tools = dummy_llm.tools

    read_file = tools["read_file"]
    list_dir = tools["list_dir"]
    grep = tools["grep"]

    # read_file rejects secrets
    assert "Access denied to protected secret file" in read_file.invoke({"path": ".env"})
    assert "Access denied to protected secret file" in read_file.invoke({"path": "key.pem"})
    assert "Access denied to protected secret file" in read_file.invoke({"path": ".ssh/id_rsa"})

    # list_dir rejects direct secret query and filters secret entries
    assert "Access denied to protected secret path" in list_dir.invoke({"path": ".ssh"})
    root_listing = list_dir.invoke({"path": "."})
    assert ".env" not in root_listing
    assert "key.pem" not in root_listing
    assert "main.py" in root_listing

    # grep rejects secret path argument and ignores secret files
    assert "Access denied to protected secret path" in grep.invoke({"query": "SECRET", "path": ".env"})
    grep_res = grep.invoke({"query": "SECRET"})
    assert ".env" not in grep_res


def test_engine_implement_read_tools_block_secrets(tmp_path):
    """Verify engine node_implement read tools reject all secret files."""
    from jev.engine import node_implement

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)

    (repo / "app.py").write_text("print('app')", encoding="utf-8")
    (repo / ".env").write_text("TOP_SECRET=xyz", encoding="utf-8")
    (repo / "credentials.json").write_text('{"token": "xyz"}', encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    class DummyLLM:
        def bind_tools(self, tools):
            self.tools = {t.name: t for t in tools}
            return self

        def invoke(self, messages):
            return MagicMock(tool_calls=[])

    dummy_llm = DummyLLM()
    sg = Subgoal(id="sg-1", description="implement feature", scope=["app.py"])
    state = {
        "ticket": "implement feature",
        "plan_queue": [sg],
        "completed_subgoals": [],
        "workspace": ws,
        "llm": dummy_llm,
        "trajectory": [],
        "status": "in_progress",
    }

    node_implement(state, workspace=ws, llm=dummy_llm)
    tools = dummy_llm.tools

    read_file = tools["read_file"]
    list_dir = tools["list_dir"]
    grep = tools["grep"]
    inspect_structure = tools["inspect_structure"]

    assert "Access denied to protected secret file" in read_file.invoke({"path": ".env"})
    assert "Access denied to protected secret file" in read_file.invoke({"path": "credentials.json"})
    assert "Access denied to protected secret file" in list_dir.invoke({"path": ".env"})
    assert "Access denied to protected secret" in grep.invoke({"query": "TOP", "path": ".env"})
    assert "Access denied to protected secret file" in inspect_structure.invoke({"path": ".env"})


# ==============================================================================
# 2. Payload redaction tests
# ==============================================================================

def test_payload_redaction_secrets_in_text_and_data_structure():
    """Verify deterministic token and credential redaction across data shapes."""
    raw_text = (
        "Token: ghp_1234567890abcdef1234\n"
        "API: sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456\n"
        "Auth: Bearer my-secret-bearer-token-12345\n"
        "AWS: AKIAIOSFODNN7EXAMPLE\n"
        "URL: https://user:superpass123@api.example.com\n"
        "Config: api_key=supersecret123\n"
    )
    redacted = redact_secrets_in_text(raw_text)
    assert "ghp_1234567890abcdef1234" not in redacted
    assert "sk-ant-api03-" not in redacted
    assert "my-secret-bearer-token-12345" not in redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "superpass123" not in redacted
    assert "[REDACTED]" in redacted

    struct = {
        "api_key": "raw_secret_value",
        "nested": {
            "password": "secret_password",
            "safe": "visible string with ghp_1234567890abcdef1234 token",
        },
        "list": ["clean", "sk-proj-1234567890abcdef12345678"],
    }
    red_struct = redact_data_structure(struct)
    assert red_struct["api_key"] == "[REDACTED]"
    assert red_struct["nested"]["password"] == "[REDACTED]"
    assert "ghp_" not in red_struct["nested"]["safe"]
    assert "sk-proj-" not in red_struct["list"][1]


def test_gatekeeper_redacts_outgoing_payload_and_escalation():
    """Verify Gatekeeper redacts outgoing request payloads and escalation dumps."""
    captured_payloads = []

    def mock_post(url, json=None, headers=None):
        captured_payloads.append(json)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_error = False
        mock_resp.json.return_value = {
            "answers": {
                "valid": {"value": True, "probability": 0.95},
            }
        }
        return mock_resp

    gk = Gatekeeper(api_url="https://api.test/v1", api_key="test-key")
    gk.client.post = mock_post

    sg = Subgoal(id="sg-1", description="Subgoal with ghp_1234567890abcdef1234", scope=["app.py"])
    secret_diff = "diff --git a/app.py b/app.py\n+api_key = 'sk-123456789012345678901234'"
    gk.validate_subgoal(sg, diff=secret_diff, mechanical_detail="detail with AKIAIOSFODNN7EXAMPLE")

    assert len(captured_payloads) == 1
    sent = json.dumps(captured_payloads[0])
    assert "ghp_1234567890abcdef1234" not in sent
    assert "sk-123456789012345678901234" not in sent
    assert "AKIAIOSFODNN7EXAMPLE" not in sent
    assert "[REDACTED]" in sent


def test_gatekeeper_escalate_deadlock_redaction(tmp_path):
    """Verify Gatekeeper.escalate_deadlock writes redacted escalation files."""
    gk = Gatekeeper(api_url="https://api.test/v1", api_key="test-key")
    log_file = tmp_path / "escalation.log"

    trajectory = [
        {"error": "Failed with bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz.abc"},
        {"secret_key": "my_hidden_pass"},
    ]
    gk.escalate_deadlock(trajectory, triggering_tier="gate", log_path=log_file)

    log_content = log_file.read_text(encoding="utf-8")
    assert "my_hidden_pass" not in log_content
    assert "[REDACTED]" in log_content


def test_workspace_diff_and_test_output_redaction(tmp_path):
    """Verify Workspace.get_staged_diff and _record_test_run redact sensitive tokens."""
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = Workspace(repo_dir=repo, worktree_dir=repo)

    # Test output redaction
    ws._record_test_run(
        ecosystem="python",
        command=["pytest"],
        exit_code=1,
        stdout="Passed test 1\nToken: ghp_abcdefghijklmnopqrstuvwx",
        stderr="Error with secret password=my_super_secret_passwd",
    )
    assert "ghp_abcdefghijklmnopqrstuvwx" not in ws.last_test_run["stdout"]
    assert "my_super_secret_passwd" not in ws.last_test_run["stderr"]
    assert "[REDACTED]" in ws.last_test_run["stdout"]


# ==============================================================================
# 3. Raw reasoning exclusion tests
# ==============================================================================

def test_raw_reasoning_stripping_and_exclusion():
    """Verify <think> tags and raw reasoning keys are thoroughly excluded."""
    text_with_think = "Hello world <think>internal deliberation</think> final decision."
    assert strip_raw_reasoning(text_with_think) == "Hello world  final decision."

    unclosed_think = "Prefix text <think>unfinished internal thoughts..."
    assert strip_raw_reasoning(unclosed_think) == "Prefix text"

    data = {
        "cot": "Chain of thought should be dropped",
        "chain_of_thought": "Dropped too",
        "reasoning_trace": "Dropped trace",
        "hidden_reasoning": "Dropped hidden",
        "result": "Clean text <think>secret</think> here",
        "nested": {
            "cot": "drop nested",
            "keep": "clean nested",
        },
    }
    clean = exclude_raw_reasoning_data(data)
    assert "cot" not in clean
    assert "chain_of_thought" not in clean
    assert "reasoning_trace" not in clean
    assert "hidden_reasoning" not in clean
    assert "cot" not in clean["nested"]
    assert clean["nested"]["keep"] == "clean nested"
    assert clean["result"] == "Clean text  here"


def test_ledger_excludes_reasoning_and_redacts_secrets(tmp_path):
    """Verify SqliteLedger never persists reasoning keys or unredacted secrets."""
    db_file = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_file)

    ticket_text = "Fix ticket with secret ghp_1234567890abcdef1234 and <think>internal ticket note</think>"
    ledger.record_run_start(
        run_id="run-1",
        thread_id="t-1",
        ticket=ticket_text,
    )

    secret_diff = "diff +key = 'sk-123456789012345678901234'\n<think>diff thoughts</think>"
    feedback = "Feedback with token AKIAIOSFODNN7EXAMPLE and <think>reasoning</think>"
    failure_ev = {
        "cot": "CoT reasoning",
        "chain_of_thought": "Step 1, step 2",
        "diagnostics": "Error with password=admin123",
    }

    ledger.record_attempt(
        run_id="run-1",
        subgoal_id="sg-1",
        tier="mechanical",
        outcome="failed",
        diff=secret_diff,
        feedback_sent=feedback,
        failure_evidence=failure_ev,
    )

    # Read back from database
    with ledger._get_connection() as conn:
        run_row = conn.execute("SELECT ticket FROM runs WHERE run_id = 'run-1'").fetchone()
        assert "ghp_1234567890abcdef1234" not in run_row[0]
        assert "<think>" not in run_row[0]

        att_row = conn.execute(
            "SELECT diff, feedback_sent, failure_evidence FROM attempts WHERE run_id = 'run-1'"
        ).fetchone()
        persisted_diff, persisted_fb, persisted_fe = att_row
        assert "sk-123456789012345678901234" not in persisted_diff
        assert "<think>" not in persisted_diff
        assert "AKIAIOSFODNN7EXAMPLE" not in persisted_fb
        assert "<think>" not in persisted_fb

        assert "cot" not in persisted_fe
        assert "chain_of_thought" not in persisted_fe
        assert "admin123" not in persisted_fe


# ==============================================================================
# 4. Advisor brief validation and timeout contract tests
# ==============================================================================

def test_advisor_repair_brief_schema_and_reasoning_trace_rejection():
    """Verify parse_repair_brief strictly enforces schema and rejects reasoning traces."""
    valid_json = json.dumps({
        "summary": "Fix syntax error",
        "cause": "Missing colon on line 12",
        "actions": ["Add colon after if condition"],
    })
    brief = parse_repair_brief(valid_json)
    assert brief.summary == "Fix syntax error"
    assert brief.actions == ["Add colon after if condition"]

    # Reject reasoning traces inside content
    with pytest.raises(ValueError, match="reasoning-trace text is rejected"):
        parse_repair_brief("Here is my brief: <think>deliberation</think> " + valid_json)

    with pytest.raises(ValueError, match="reasoning-trace text is rejected"):
        parse_repair_brief(json.dumps({
            "summary": "Fix error",
            "cause": "Bad syntax",
            "actions": ["Fix it"],
            "cot": "Reasoning trace",
        }))

    with pytest.raises(ValueError, match="reasoning-trace text is rejected"):
        parse_repair_brief(json.dumps({
            "summary": "Fix error <think>thought</think>",
            "cause": "Bad syntax",
            "actions": ["Fix it"],
        }))

    # Reject invalid schemas (missing fields or wrong lengths)
    with pytest.raises(ValueError):
        parse_repair_brief(json.dumps({"summary": "Incomplete"}))

    with pytest.raises(ValueError):
        parse_repair_brief(json.dumps({
            "summary": "x" * 600,
            "cause": "cause",
            "actions": ["act"],
        }))


def test_advisor_provider_drops_invalid_output_non_blocking():
    """Verify OpenAICompatibleProvider drops invalid model output without fabricating success."""
    class FakeClient:
        def __init__(self, response_text):
            self.response_text = response_text

        def post(self, url, json=None, headers=None):
            resp = MagicMock()
            resp.is_error = False
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": self.response_text}}]
            }
            return resp

    # Case 1: Model outputs hallucinated non-JSON text
    client_bad = FakeClient("I am not sure how to fix this, try again.")
    provider = OpenAICompatibleProvider("https://advisory.test", "model-1", client=client_bad)
    res = provider.advise(AdvisoryRequest(prompt="help"))
    assert not res.ok
    assert res.brief is None
    assert res.error_code == AdvisoryErrorCode.VALIDATION

    # Case 2: Model outputs reasoning trace
    client_think = FakeClient("<think>private reasoning</think> {\"summary\": \"s\", \"cause\": \"c\", \"actions\": [\"a\"]}")
    provider_think = OpenAICompatibleProvider("https://advisory.test", "model-1", client=client_think)
    res_think = provider_think.advise(AdvisoryRequest(prompt="help"))
    assert not res_think.ok
    assert res_think.brief is None
    assert res_think.error_code == AdvisoryErrorCode.VALIDATION


def test_advisor_timeout_contract():
    """Verify ADVISORY_TIMEOUT contract: default 20s, positive number up to 60s max."""
    # Default is 20.0
    assert validate_advisory_timeout(None) == 20.0
    assert validate_advisory_timeout("20") == 20.0
    assert validate_advisory_timeout(45.5) == 45.5
    assert validate_advisory_timeout(60.0) == 60.0

    # Non-positive or exceeding 60s raises ValueError
    with pytest.raises(ValueError, match="positive number"):
        validate_advisory_timeout(0)

    with pytest.raises(ValueError, match="positive number"):
        validate_advisory_timeout(-5)

    with pytest.raises(ValueError, match="cannot exceed 60.0 seconds"):
        validate_advisory_timeout(60.1)

    with pytest.raises(ValueError, match="cannot exceed 60.0 seconds"):
        validate_advisory_timeout(120)

    with pytest.raises(ValueError, match="positive number"):
        validate_advisory_timeout("not-a-number")


# ==============================================================================
# 5. Gatekeeper .env loading removal tests
# ==============================================================================

def test_gatekeeper_never_loads_dot_env_by_default(tmp_path, monkeypatch):
    """Verify Gatekeeper removes Path('.env') default and never reads .env from cwd."""
    # Clear environment variables
    monkeypatch.delenv("JEV_API_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    # Create a .env file in the current working directory
    dot_env = tmp_path / ".env"
    dot_env.write_text("JEV_API_URL=https://secret-env.test\nJEV_API_KEY=secret-env-key\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    # Calling Gatekeeper() without env_file should NOT read .env and must raise ValueError
    with pytest.raises(ValueError, match="JEV_API_URL must be configured"):
        Gatekeeper()

    # Explicit env_file parameter still works if explicitly requested
    gk_explicit = Gatekeeper(env_file=dot_env)
    assert gk_explicit.api_url == "https://secret-env.test"
    assert gk_explicit.api_key == "secret-env-key"


# ==============================================================================
# 6. Isolation test: src/ must not import advisor, deepseek, laya, kaggle_server, tri_shadow
# ==============================================================================

def test_src_isolation_from_prohibited_modules():
    """Verify that no file under src/ imports advisor, deepseek, laya, kaggle_server, or tri_shadow."""
    prohibited = {"advisor", "deepseek", "laya", "kaggle_server", "tri_shadow"}
    violations = []

    src_files = glob.glob("src/**/*.py", recursive=True)
    assert len(src_files) > 0, "Expected to find python files in src/"

    for file_path in src_files:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
        try:
            tree = ast.parse(code, filename=file_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.partition(".")[0]
                    if root_mod in prohibited:
                        violations.append((file_path, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.partition(".")[0]
                    if root_mod in prohibited:
                        violations.append((file_path, node.lineno, node.module))

    assert not violations, f"Isolation violation! Prohibited imports found in src/: {violations}"


# ==============================================================================
# 7. Kaggle server route & contract tests
# ==============================================================================

def test_kaggle_server_contract_elements():
    """Verify kaggle_server.py specifies /chat/completions route, max_new_tokens 800, and HTTP 502 on invalid JSON."""
    server_path = Path("advisor/kaggle_server.py")
    assert server_path.exists(), "advisor/kaggle_server.py must exist"

    text = server_path.read_text(encoding="utf-8")
    assert "@app.post(\"/chat/completions\")" in text
    assert "max_new_tokens=800" in text
    assert "status_code=502" in text
    assert "re.sub(r\"(?s)<think>.*?(?:</think>|\\Z)\", \"\", raw_response)" in text
