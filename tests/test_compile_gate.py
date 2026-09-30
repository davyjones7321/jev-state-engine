import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.engine import node_gate, node_verify
from jev.models import (
    CompileOutcome,
    State,
    Subgoal,
    ValidationVerdict,
)
from jev.workspace import Workspace


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    return ws


# Scenario 1: Pass
def test_compile_pass(git_repo):
    ws = git_repo
    subgoal = Subgoal(description="Update app.py", scope=["src/app.py"], expects_tests=False)

    (ws.repo_dir / "src" / "app.py").write_text("def hello():\n    return 'hello universe'\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Update app",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "passed"

    entry = [e for e in out["trajectory"] if e.get("node") == "gate"][-1]
    assert "compile" in entry
    comp = entry["compile"]
    assert comp is not None
    assert comp["outcome"] == CompileOutcome.PASSED.value
    assert comp["ecosystem"] == "python"
    assert comp["exit_code"] == 0
    assert comp["new_errors"] == []
    assert comp["base_errors"] == []


# Scenario 2: New-error fail
def test_compile_new_error_fail(git_repo):
    ws = git_repo
    subgoal = Subgoal(description="Break app.py syntax", scope=["src/app.py"], expects_tests=False)

    # Note: check_build also does AST, but let's test check_compile with a syntax error directly
    # and via node_gate
    (ws.repo_dir / "src" / "app.py").write_text("def invalid_syntax(\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.FAILED.value
    assert len(compile_res["new_errors"]) > 0
    assert compile_res["base_errors"] == []
    assert "SyntaxError" in compile_res["new_errors"][0]


# Scenario 3: Pre-existing-error pass
def test_compile_pre_existing_error_pass(tmp_path):
    repo = tmp_path / "repo_preexisting"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "preexisting-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # check.js simulates a typechecker that always outputs an existing error
    check_js = (
        "const fs = require('fs');\n"
        "console.error('src/old.ts:1:1: error: pre-existing type mismatch');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "old.ts").write_text("const x: number = 'old';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit with pre-existing error"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    # Now modify another file
    (repo / "src" / "new_file.ts").write_text("export const y = 42;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/new_file.ts"], cwd=repo, check=True, capture_output=True)

    compile_res = ws.check_compile()
    # Should PASS because the only error in worktree was already in base commit!
    assert compile_res["outcome"] == CompileOutcome.PASSED.value
    assert len(compile_res["base_errors"]) > 0
    assert len(compile_res["new_errors"]) == 0
    assert "pre-existing" in compile_res["detail"].lower()


# Scenario 4: NO_COMPILE_COMMAND escalation
def test_compile_no_compile_command_escalation(tmp_path):
    repo = tmp_path / "repo_no_cmd"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    # TypeScript source with package.json (so JS/TS ecosystem is detected), but no typecheck/build/tsconfig
    pkg_json = {"name": "app", "scripts": {"lint": "eslint ."}}
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("console.log('init');\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    (repo / "src" / "index.ts").write_text("console.log('modified');\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)

    subgoal = Subgoal(description="Modify index.ts", scope=["src/index.ts"], expects_tests=False)
    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Ticket",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["status"] == "escalated"
    assert out["gate_status"] == "no_compile_command"
    assert gk.escalate_deadlock.called
    assert gk.escalate_deadlock.call_args[1]["triggering_tier"] == "mechanical"

    entry = [e for e in out["trajectory"] if e.get("node") == "gate"][-1]
    assert entry["compile"]["outcome"] == CompileOutcome.NO_COMPILE_COMMAND.value


# Scenario 5: ENV_NOT_READY
def test_compile_env_not_ready(tmp_path):
    repo = tmp_path / "repo_env_not_ready"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    # JS/TS project with package.json and tsconfig.json, but node_modules is MISSING
    pkg_json = {"name": "app", "scripts": {"typecheck": "tsc --noEmit"}}
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "index.ts").write_text("const a: number = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)

    (repo / "index.ts").write_text("const a: number = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "index.ts"], cwd=repo, check=True, capture_output=True)

    subgoal = Subgoal(description="Update index.ts", scope=["index.ts"], expects_tests=False)
    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Ticket",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["status"] == "escalated"
    assert out["gate_status"] == "env_not_ready"
    assert "node_modules is missing" in out["last_feedback"]
    assert gk.escalate_deadlock.called

    entry = [e for e in out["trajectory"] if e.get("node") == "gate"][-1]
    assert entry["compile"]["outcome"] == CompileOutcome.ENV_NOT_READY.value


# Scenario 6: Docs-only exempt
def test_compile_docs_only_exempt(git_repo):
    ws = git_repo
    subgoal = Subgoal(description="Add docstring", scope=["src/app.py"], expects_tests=False)

    (ws.repo_dir / "src" / "app.py").write_text(
        '"""App module docstring."""\n\ndef hello():\n    return \'world\'\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.EXEMPT.value
    assert "docs-only" in compile_res["detail"].lower()


# Scenario 7: CSS-only exempt
def test_compile_css_only_exempt(git_repo):
    ws = git_repo
    subgoal = Subgoal(description="Update styles", scope=["styles.css"], expects_tests=False)

    (ws.repo_dir / "styles.css").write_text("body { color: red; }\n", encoding="utf-8")
    subprocess.run(["git", "add", "styles.css"], cwd=ws.repo_dir, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.EXEMPT.value
    assert "no compiled source files" in compile_res["detail"].lower()


# Scenario 8: Infra-as-code exempt
def test_compile_infra_as_code_exempt(git_repo):
    ws = git_repo
    subgoal = Subgoal(description="Update terraform", scope=["main.tf"], expects_tests=False)

    (ws.repo_dir / "main.tf").write_text('resource "null_resource" "test" {}\n', encoding="utf-8")
    subprocess.run(["git", "add", "main.tf"], cwd=ws.repo_dir, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.EXEMPT.value
    assert "no compiled source files" in compile_res["detail"].lower()


# Scenario 9: Junction/link cleanup never touches target
def test_junction_cleanup_never_touches_target(git_repo):
    ws = git_repo
    real_nm = ws.repo_dir / "node_modules"
    real_nm.mkdir(parents=True, exist_ok=True)
    target_marker = real_nm / "my-package.json"
    target_marker.write_text('{"name": "vital-dependency"}', encoding="utf-8")

    # Create worktree
    wt_path = ws.create_subgoal_worktree("test-cleanup")
    wt_nm = wt_path / "node_modules"
    assert wt_nm.exists(), "Worktree node_modules junction/symlink should exist"
    assert (wt_nm / "my-package.json").exists(), "Package marker should be accessible through link"

    # Merge or discard worktree
    ws.discard_subgoal_worktree(wt_path, "jev-subgoal-test-cleanup")

    # Assert worktree is cleaned up, but real node_modules in repo is untouched!
    assert not wt_path.exists(), "Worktree path should be removed"
    assert real_nm.exists(), "Repository node_modules MUST NOT be deleted"
    assert target_marker.exists(), "File inside repository node_modules MUST NOT be deleted"
    assert target_marker.read_text(encoding="utf-8") == '{"name": "vital-dependency"}'


# Scenario 10: Verify-stage compile failure
def test_verify_stage_compile_failure(git_repo):
    ws = git_repo
    gk = FakeGatekeeper()

    # Introduce a new compile failure on the main merged tree
    (ws.repo_dir / "src" / "app.py").write_text("def broken_syntax(\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/app.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    state: State = {
        "ticket": "Verify ticket",
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": "passed",
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_verify(state, workspace=ws, gatekeeper=gk)
    assert out["status"] == "verification_failed"
    assert out["gate_status"] == "verification_failed"
    assert "Final compile check failed" in out["last_feedback"]
    assert gk.escalate_deadlock.called
    assert gk.escalate_deadlock.call_args[1]["triggering_tier"] == "mechanical"

    verify_entry = [e for e in out["trajectory"] if e.get("node") == "verify"][-1]
    assert "compile" in verify_entry
    assert verify_entry["compile"]["outcome"] == CompileOutcome.FAILED.value
    assert len(verify_entry["compile"]["new_errors"]) > 0


# Scenario 11: Nonzero exit with unparsable output -> FAILED
def test_compile_nonzero_exit_unparsable_output_fails(tmp_path):
    repo = tmp_path / "repo_unparsable"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "unparsable-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # Script exits non-zero but outputs text with no error/fail keywords or line numbers
    check_js = (
        "console.log('Build runner started');\n"
        "console.log('Process terminated with status 137');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    (repo / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=repo, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.FAILED.value
    assert "no parsable errors were found" in compile_res["detail"]
    assert compile_res["exit_code"] == 1
    assert compile_res["new_errors"] == []
    assert compile_res["base_errors"] == []


# Scenario 12: Missing executable -> ENV_NOT_READY
def test_compile_missing_executable_env_not_ready(tmp_path):
    repo = tmp_path / "repo_missing_exec"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "missing-exec-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    (repo / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=repo, check=True, capture_output=True)

    from jev.workspace import ECOSYSTEM_COMPILE_HANDLERS
    js_handler = next(h for h in ECOSYSTEM_COMPILE_HANDLERS if h.name == "js_ts")
    with patch.object(js_handler, "run_command", side_effect=FileNotFoundError("No such file or directory: 'npm'")):
        compile_res = ws.check_compile()
        assert compile_res["outcome"] == CompileOutcome.ENV_NOT_READY.value
        assert "Environment not ready" in compile_res["detail"]
        assert "FileNotFoundError" in compile_res["detail"]


# Scenario 13: Timeout -> ENV_NOT_READY
def test_compile_timeout_env_not_ready(tmp_path):
    repo = tmp_path / "repo_timeout"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "timeout-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    (repo / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=repo, check=True, capture_output=True)

    from jev.workspace import ECOSYSTEM_COMPILE_HANDLERS
    js_handler = next(h for h in ECOSYSTEM_COMPILE_HANDLERS if h.name == "js_ts")
    with patch.object(js_handler, "run_command", side_effect=subprocess.TimeoutExpired(cmd=["npm", "run", "typecheck"], timeout=5)):
        compile_res = ws.check_compile()
        assert compile_res["outcome"] == CompileOutcome.ENV_NOT_READY.value
        assert "Environment not ready" in compile_res["detail"]
        assert "TimeoutExpired" in compile_res["detail"]


# Scenario 14: Pre-existing errors only -> PASSED
def test_compile_pre_existing_errors_only_passed(tmp_path):
    repo = tmp_path / "repo_preexisting_only"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "preexisting-only-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    check_js = (
        "console.error('src/old.ts:1:1: error: pre-existing type mismatch');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "old.ts").write_text("const x: number = 'old';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit with pre-existing error"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    (repo / "src" / "new.ts").write_text("export const z = 99;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/new.ts"], cwd=repo, check=True, capture_output=True)

    compile_res = ws.check_compile()
    assert compile_res["outcome"] == CompileOutcome.PASSED.value
    assert len(compile_res["base_errors"]) == 1
    assert "src/old.ts:1:1: error: pre-existing type mismatch" in compile_res["base_errors"][0]
    assert len(compile_res["new_errors"]) == 0
    assert "pre-existing" in compile_res["detail"].lower()


# Scenario 15: One new error among pre-existing -> FAILED
def test_compile_one_new_error_among_preexisting_fails(tmp_path):
    repo = tmp_path / "repo_one_new_err"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "one-new-err-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # Base commit check.js has only the old error
    check_js_base = (
        "console.error('src/old.ts:1:1: error: pre-existing type mismatch');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js_base, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "old.ts").write_text("const x: number = 'old';\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Base with old error"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    wt_path = ws.create_subgoal_worktree("one-new-err")
    ws_wt = Workspace(repo_dir=repo, worktree_dir=wt_path)

    # In worktree, check.js produces the old error AND a new error
    check_js_wt = (
        "console.error('src/old.ts:1:1: error: pre-existing type mismatch');\n"
        "console.error('src/new.ts:2:2: error: brand new syntax error');\n"
        "process.exit(1);\n"
    )
    (wt_path / "check.js").write_text(check_js_wt, encoding="utf-8")
    (wt_path / "src" / "new.ts").write_text("export const z = 100;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/new.ts", "check.js"], cwd=wt_path, check=True, capture_output=True)

    compile_res = ws_wt.check_compile()
    try:
        assert compile_res["outcome"] == CompileOutcome.FAILED.value
        assert len(compile_res["base_errors"]) == 1
        assert "src/old.ts:1:1: error: pre-existing type mismatch" in compile_res["base_errors"][0]
        assert len(compile_res["new_errors"]) == 1
        assert "src/new.ts:2:2: error: brand new syntax error" in compile_res["new_errors"][0]
        assert "1 new error(s)" in compile_res["detail"]
    finally:
        ws.discard_subgoal_worktree(wt_path, "jev-subgoal-one-new-err")


# Scenario 16: Parser fixture from a real run & both TypeScript diagnostic formats
def test_tsc_parser_real_run_fixture():
    fixture = """src/app/layout.tsx:5:8 - error TS2613: Module '"C:/Users/DavyJ/Desktop/jp-bat/src/components/Footer"' has no default export. Did you mean to use 'import { Footer } from "C:/Users/DavyJ/Desktop/jp-bat/src/components/Footer"' instead?

5 import Footer from "@/components/Footer";
         ~~~~~~

src/components/Footer.tsx:3:16 - error TS2305: Module '"lucide-react"' has no exported member 'Github'.

3 import { Mail, Github, Twitter, Linkedin, Heart } from 'lucide-react';
                 ~~~~~~

src/components/Footer.tsx:3:24 - error TS2305: Module '"lucide-react"' has no exported member 'Twitter'.

src/components/Footer.tsx:3:33 - error TS2305: Module '"lucide-react"' has no exported member 'Linkedin'.

src/components/Navigation.tsx:2:22 - error TS2307: Cannot find module 'react-router-dom' or its corresponding type declarations.

Found 5 errors in 3 files."""

    errors = Workspace.parse_diagnostics(fixture)
    assert len(errors) == 5

    # 1. layout.tsx TS2613
    assert errors[0].file == "src/app/layout.tsx"
    assert errors[0].code == "TS2613"
    assert errors[0].line == 5
    assert errors[0].column == 8
    assert "Footer" in errors[0].message

    # 2. Footer.tsx TS2305 x3
    footer_errs = [e for e in errors if e.file == "src/components/Footer.tsx"]
    assert len(footer_errs) == 3
    for fe in footer_errs:
        assert fe.code == "TS2305"
        assert "lucide-react" in fe.message
    assert any("Github" in fe.message for fe in footer_errs)
    assert any("Twitter" in fe.message for fe in footer_errs)
    assert any("Linkedin" in fe.message for fe in footer_errs)

    # 3. Navigation.tsx TS2307
    nav_errs = [e for e in errors if e.file == "src/components/Navigation.tsx"]
    assert len(nav_errs) == 1
    assert nav_errs[0].code == "TS2307"
    assert "react-router-dom" in nav_errs[0].message

    # Also test second TS format: "src/a.ts(5,8): error TS2613: msg"
    alt_fmt = "src/a.ts(12,34): error TS2613: Alternate format message"
    alt_errs = Workspace.parse_diagnostics(alt_fmt)
    assert len(alt_errs) == 1
    assert alt_errs[0].file == "src/a.ts"
    assert alt_errs[0].code == "TS2613"
    assert alt_errs[0].line == 12
    assert alt_errs[0].column == 34
    assert alt_errs[0].message == "Alternate format message"


# Scenario 17: (a) Same errors on shifted lines are NOT new
def test_compile_same_errors_shifted_lines_not_new(tmp_path):
    repo = tmp_path / "repo_shifted_lines"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "shifted-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # Base commit check.js reports error on line 5
    check_js_base = (
        "console.error('src/foo.ts:5:10 - error TS2305: Module has no member foo');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js_base, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "foo.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Base commit with error on line 5"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    wt_path = ws.create_subgoal_worktree("shifted")
    ws_wt = Workspace(repo_dir=repo, worktree_dir=wt_path)

    # In worktree, lines shifted due to added comments/lines above, so check.js reports same error on line 25
    check_js_wt = (
        "console.error('src/foo.ts:25:10 - error TS2305: Module has no member foo');\n"
        "process.exit(1);\n"
    )
    (wt_path / "check.js").write_text(check_js_wt, encoding="utf-8")
    (wt_path / "src" / "foo.ts").write_text("// new header\n// more lines\nexport const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)

    try:
        res = ws_wt.check_compile()
        assert res["outcome"] == CompileOutcome.PASSED.value
        assert len(res["base_errors"]) == 1
        assert len(res["new_errors"]) == 0
        assert "pre-existing" in res["detail"].lower()
    finally:
        ws.discard_subgoal_worktree(wt_path, "jev-subgoal-shifted")


# Scenario 18: (b) Extra duplicate of an existing error IS new
def test_compile_extra_duplicate_of_existing_error_is_new(tmp_path):
    repo = tmp_path / "repo_extra_duplicate"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "extra-dup-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # Base commit check.js reports 1 occurrence of TS2305
    check_js_base = (
        "console.error('src/foo.ts:5:10 - error TS2305: Module has no member foo');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js_base, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "foo.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Base with 1 occurrence of error"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    wt_path = ws.create_subgoal_worktree("extra-dup")
    ws_wt = Workspace(repo_dir=repo, worktree_dir=wt_path)

    # In worktree, there are 2 occurrences of TS2305 with the same message (e.g. imported again on line 12)
    check_js_wt = (
        "console.error('src/foo.ts:5:10 - error TS2305: Module has no member foo');\n"
        "console.error('src/foo.ts:12:10 - error TS2305: Module has no member foo');\n"
        "process.exit(1);\n"
    )
    (wt_path / "check.js").write_text(check_js_wt, encoding="utf-8")
    (wt_path / "src" / "foo.ts").write_text("export const x = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True, capture_output=True)

    try:
        res = ws_wt.check_compile()
        assert res["outcome"] == CompileOutcome.FAILED.value
        assert len(res["base_errors"]) == 1
        assert len(res["new_errors"]) == 1
        assert "1 new error(s)" in res["detail"]
    finally:
        ws.discard_subgoal_worktree(wt_path, "jev-subgoal-extra-dup")


# Scenario 19: (c) In verify stage baseline comes from a different tree than merged tree
def test_compile_verify_stage_baseline_different_tree_fails_on_merge_error(tmp_path):
    repo = tmp_path / "repo_verify_stage"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "verify-merge-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    # Base commit check.js passes (exit 0)
    check_js_clean = (
        "console.log('clean compile');\n"
        "process.exit(0);\n"
    )
    (repo / "check.js").write_text(check_js_clean, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Clean base commit"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    # ws.base_commit points to "Clean base commit"

    # Now subgoals were executed and merged into repo_dir (the merged tree).
    # In repo_dir, a compile error is introduced (e.g. check.js now fails with a TS error)
    check_js_fail = (
        "console.error('src/index.ts:1:1: error TS9999: Merge conflict or breakage');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js_fail, encoding="utf-8")
    (repo / "src" / "index.ts").write_text("export const a = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Merged subgoals commit with breakage"], cwd=repo, check=True, capture_output=True)

    # In node_verify, worktree_dir == repo_dir
    ws.worktree_dir = ws.repo_dir

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Implement feature",
        "plan_queue": [],
        "current_subgoal": None,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": "passed",
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_verify(state, workspace=ws, gatekeeper=gk)

    # Assert verification FAILED because baseline in detached worktree at base_commit was clean,
    # so the error in repo_dir is new!
    assert out["status"] == "verification_failed"
    assert out["gate_status"] == "verification_failed"
    assert "Final compile check failed" in out["last_feedback"]
    assert gk.escalate_deadlock.called

    verify_entry = [e for e in out["trajectory"] if e.get("node") == "verify"][-1]
    comp = verify_entry["compile"]
    assert comp["outcome"] == CompileOutcome.FAILED.value
    assert len(comp["new_errors"]) == 1
    assert "TS9999" in comp["new_errors"][0]


# Scenario 20: (d) Temp baseline worktree and node_modules link removed, real node_modules untouched
def test_compile_temp_baseline_worktree_and_link_cleaned_up_real_nm_untouched(tmp_path):
    repo = tmp_path / "repo_cleanup_nm"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "cleanup-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    real_nm = repo / "node_modules"
    real_nm.mkdir()
    marker = real_nm / "secret-dep.txt"
    marker.write_text("vital-data", encoding="utf-8")

    check_js = (
        "console.error('src/index.ts:1:1: error TS1000: Some error');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    wt_path = ws.create_subgoal_worktree("cleanup-check")
    ws_wt = Workspace(repo_dir=repo, worktree_dir=wt_path)

    try:
        (wt_path / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/index.ts"], cwd=wt_path, check=True, capture_output=True)

        res = ws_wt.check_compile()
        # Compile ran baseline comparison
        assert res["outcome"] in (CompileOutcome.PASSED.value, CompileOutcome.FAILED.value)

        # Verify no baseline worktrees remain in .jev-worktrees
        worktrees_dir = repo / ".jev-worktrees"
        baseline_dirs = [d for d in worktrees_dir.glob("baseline-*") if d.is_dir()]
        assert len(baseline_dirs) == 0, f"Found leftover baseline worktrees: {baseline_dirs}"

        # Verify real node_modules in repo is untouched
        assert real_nm.exists(), "Real node_modules directory was deleted!"
        assert marker.exists(), "File inside real node_modules was deleted!"
        assert marker.read_text(encoding="utf-8") == "vital-data"
    finally:
        ws.discard_subgoal_worktree(wt_path, "jev-subgoal-cleanup-check")


# Scenario 21: (e) Worktree creation failure returns ENV_NOT_READY
def test_compile_baseline_worktree_creation_failure_env_not_ready(tmp_path):
    repo = tmp_path / "repo_wt_fail"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    pkg_json = {
        "name": "wt-fail-test",
        "scripts": {"typecheck": "node check.js"}
    }
    (repo / "package.json").write_text(json.dumps(pkg_json), encoding="utf-8")
    (repo / "node_modules").mkdir()

    check_js = (
        "console.error('src/index.ts:1:1: error TS1000: Some error');\n"
        "process.exit(1);\n"
    )
    (repo / "check.js").write_text(check_js, encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo, check=True, capture_output=True)

    ws = Workspace(repo_dir=repo)
    (repo / "src" / "index.ts").write_text("export const x = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/index.ts"], cwd=repo, check=True, capture_output=True)

    original_run_repo_git = ws._run_repo_git

    def fake_run_repo_git(args, check=False):
        if len(args) >= 2 and args[0] == "worktree" and args[1] == "add":
            return subprocess.CompletedProcess(
                args=["git"] + args,
                returncode=1,
                stdout="",
                stderr="fatal: unable to create worktree",
            )
        return original_run_repo_git(args, check=check)

    with patch.object(ws, "_run_repo_git", side_effect=fake_run_repo_git):
        res = ws.check_compile()
        assert res["outcome"] == CompileOutcome.ENV_NOT_READY.value
        assert "Environment not ready" in res["detail"]
        assert "failed to create temporary baseline worktree" in res["detail"]

