import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from jev.engine import _attempt_evidence, _build_source_hints, node_gate, node_implement
from jev.models import CompileOutcome, MechanicalCheckResult, State, Subgoal
from jev.workspace import Workspace


@pytest.fixture
def temp_git_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

    # Initial commit
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    return repo_dir


def test_validate_changes_ts_repo_returns_error_and_preserves_state(temp_git_repo):
    """validate_changes on a TS-style repo with a scripted typecheck returns error and preserves state."""
    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-ts", description="Add TS module", scope=["src/a.ts"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id)

    # Setup TS structure
    src_dir = wt_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (wt_path / "package.json").write_text(
        '{"name": "test-pkg", "scripts": {"typecheck": "tsc --noEmit"}}',
        encoding="utf-8",
    )
    (wt_path / "node_modules").mkdir(exist_ok=True)

    # Stage invalid TS code
    ws.stage_file_mutation("src/a.ts", "import { Foo } from './b';\nexport const x = Foo();\n")

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": ws.base_commit,
    }

    error_line = "src/a.ts(3,5): error TS2305: Module '\"./b\"' has no exported member 'Foo'."

    # Scripted typecheck returning the compile error
    fake_compile_res = {
        "ecosystem": "js_ts",
        "command": ["npm", "run", "typecheck"],
        "exit_code": 1,
        "stdout_tail": "",
        "stderr_tail": error_line,
        "output_tail": error_line,
        "new_errors": [error_line],
        "base_errors": [],
        "outcome": CompileOutcome.FAILED.value,
        "detail": f"Compile failed with 1 new error(s): {error_line}",
    }

    class CapturingLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if len(messages) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}],
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}],
            )

    with patch.object(ws, "check_compile", return_value=fake_compile_res):
        state = node_implement(state, workspace=ws, llm=CapturingLLM())

    assert state["mechanical_strike_count"] == 0
    assert state.get("gate_status") == "none"

    # Check trajectory has validate_changes output containing the error
    traj = state["trajectory"][-1]
    assert traj["node"] == "implement"
    tool_calls = traj.get("tool_calls", [])
    vc_call = next(tc for tc in tool_calls if tc["name"] == "validate_changes")
    assert error_line in vc_call["result"]
    assert "Self-check: failed" in vc_call["result"]


def test_validate_changes_cap_fifth_call_skipped_and_in_trajectory(temp_git_repo):
    """Cap of 4 calls per implement attempt; 5th returns skipped (limit reached) and recorded in trajectory."""
    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-cap", description="Cap test", scope=["src/a.py"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id)

    src_dir = wt_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/a.py", "x = 1\n")

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": ws.base_commit,
    }

    call_index = 0

    class MultiCallLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            nonlocal call_index
            call_index += 1
            if call_index <= 5:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "validate_changes", "args": {}, "id": f"call_{call_index}"}],
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_done"}],
            )

    state = node_implement(state, workspace=ws, llm=MultiCallLLM())

    traj = state["trajectory"][-1]
    assert traj["self_check_calls"] == 5
    assert len(traj["self_check_outcomes"]) == 5
    assert traj["self_check_outcomes"][-1] == "skipped (limit reached)"

    tool_calls = traj.get("tool_calls", [])
    vc_calls = [tc for tc in tool_calls if tc["name"] == "validate_changes"]
    assert len(vc_calls) == 5
    assert "Self-check: skipped (limit reached)" in vc_calls[4]["result"]


def test_validate_changes_no_artifacts_left_and_worker_staged_intact(temp_git_repo):
    """No engine artifacts left after validate_changes; worker_staged_paths remains intact."""
    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-art", description="Artifact test", scope=["src/a.ts"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id)

    src_dir = wt_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/a.ts", "export const msg = 'hello';\n")

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": ws.base_commit,
    }

    # Simulate compile check that produces an untracked artifact
    def fake_check_compile(**kwargs):
        artifact = wt_path / "dist" / "temp_build.js"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("// temp artifact\n", encoding="utf-8")
        return {
            "ecosystem": "js_ts",
            "command": ["npm", "run", "typecheck"],
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "output_tail": "",
            "new_errors": [],
            "base_errors": [],
            "outcome": CompileOutcome.PASSED.value,
            "detail": "Compile passed.",
        }

    class LLMOnce:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if len(messages) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}],
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}],
            )

    with patch.object(ws, "check_compile", side_effect=fake_check_compile):
        state = node_implement(state, workspace=ws, llm=LLMOnce())

    # Artifact must be discarded
    assert not (wt_path / "dist" / "temp_build.js").exists()
    # Worker staged path intact
    assert "src/a.ts" in ws.worker_staged_paths
    assert (wt_path / "src" / "a.ts").read_text(encoding="utf-8") == "export const msg = 'hello';\n"


def test_validate_changes_python_file_still_gets_syntax_check(temp_git_repo):
    """Python files still get syntax check and report SyntaxError."""
    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-py", description="Python test", scope=["src/mod.py"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id)

    src_dir = wt_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/mod.py", "def broken_syntax(\n")

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": ws.base_commit,
    }

    class LLMOnce:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if len(messages) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}],
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}],
            )

    state = node_implement(state, workspace=ws, llm=LLMOnce())

    traj = state["trajectory"][-1]
    vc_call = next(tc for tc in traj["tool_calls"] if tc["name"] == "validate_changes")
    assert "Self-check: failed" in vc_call["result"]
    assert "SyntaxError" in vc_call["result"]
    assert state["mechanical_strike_count"] == 0


def test_validate_changes_baseline_errors_not_reported_as_new(temp_git_repo):
    """Baseline errors present in base_commit are not reported as new by validate_changes."""
    # Commit a baseline file with syntax error in base commit
    bad_py = temp_git_repo / "legacy.py"
    bad_py.write_text("def broken(\n", encoding="utf-8")
    subprocess.run(["git", "add", "legacy.py"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Commit with baseline syntax error"], cwd=temp_git_repo, check=True, capture_output=True)

    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_git_repo, check=True, capture_output=True, text=True).stdout.strip()

    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-base", description="New feature", scope=["legacy.py"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id, base_ref=base_sha)

    # Worker stages unchanged legacy.py with the same baseline error
    ws.stage_file_mutation("legacy.py", "def broken(\n")

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": base_sha,
        "subgoal_base_commit": base_sha,
    }

    class LLMOnce:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if len(messages) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}],
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}],
            )

    state = node_implement(state, workspace=ws, llm=LLMOnce())

    traj = state["trajectory"][-1]
    vc_call = next(tc for tc in traj["tool_calls"] if tc["name"] == "validate_changes")
    # Not reported as failed because it is a baseline error
    assert "Self-check: failed" not in vc_call["result"]


def test_source_hints_location_and_referenced_file(temp_git_repo):
    """source_hints: location surrounding lines, referenced file relative to erroring file, secret excluded, caps."""
    # Create candidate structure
    src_dir = temp_git_repo / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    a_ts = src_dir / "a.ts"
    a_lines = [f"// line {i}" for i in range(1, 31)]
    a_lines[14] = "import { helper } from './util';"
    a_ts.write_text("\n".join(a_lines) + "\n", encoding="utf-8")

    util_ts = src_dir / "util.ts"
    util_lines = [f"export const util_{i} = {i};" for i in range(1, 50)]
    util_ts.write_text("\n".join(util_lines) + "\n", encoding="utf-8")

    # Create secret file
    secret_file = temp_git_repo / ".env"
    secret_file.write_text("SECRET=12345\n", encoding="utf-8")

    # Error referencing a.ts at line 15, referencing ./util, unresolvable file, and secret file
    error_text = (
        "src/a.ts(15,10): error TS2307: Cannot find module './util' or its corresponding type declarations.\n"
        "File \".env\", line 1, in secret_loader\n"
        "nonexistent/missing/file.ts:1:1: error: cannot find\n"
    )

    hints = _build_source_hints(error_text, temp_git_repo)

    # 1. Location hint for src/a.ts around line 15
    loc_hint = next((h for h in hints if h["file"] == "src/a.ts"), None)
    assert loc_hint is not None
    assert loc_hint["type"] == "location"
    assert loc_hint["line"] == 15
    assert loc_hint["confidence"] == "hint, may be incomplete"
    assert "15: import { helper } from './util';" in loc_hint["snippet"]
    # Surrounding lines (+- 10 lines)
    assert "5: // line 5" in loc_hint["snippet"]
    assert "25: // line 25" in loc_hint["snippet"]

    # 2. Referenced file resolves relative to src/a.ts -> src/util.ts
    ref_hint = next((h for h in hints if h["file"] == "src/util.ts"), None)
    assert ref_hint is not None
    assert ref_hint["type"] == "referenced_file"
    assert ref_hint["confidence"] == "hint, may be incomplete"
    # Capped at 40 lines
    snippet_lines = ref_hint["snippet"].splitlines()
    assert len(snippet_lines) <= 40

    # 3. Secret files (.env) never included
    assert not any(h["file"] == ".env" for h in hints)

    # 4. Unresolvable token ignored
    assert not any("nonexistent" in h["file"] for h in hints)

    # 5. Caps: max 3 files, 6000 chars total
    assert len(hints) <= 3
    total_chars = sum(len(h["snippet"]) for h in hints)
    assert total_chars <= 6000


def test_source_hints_python_traceback_and_go_style(temp_git_repo):
    """source_hints works for Python traceback (File 'x', line N) and Go-style (file:line:col)."""
    pkg_dir = temp_git_repo / "pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    py_file = pkg_dir / "worker.py"
    py_lines = [f"# py line {i}" for i in range(1, 30)]
    py_lines[9] = "raise ValueError('something went wrong')"
    py_file.write_text("\n".join(py_lines) + "\n", encoding="utf-8")

    go_file = pkg_dir / "main.go"
    go_lines = [f"// go line {i}" for i in range(1, 30)]
    go_lines[19] = "func Execute() {}"
    go_file.write_text("\n".join(go_lines) + "\n", encoding="utf-8")

    traceback_err = (
        'Traceback (most recent call last):\n'
        '  File "pkg/worker.py", line 10, in run\n'
        '    raise ValueError("something went wrong")\n'
        'pkg/main.go:20:5: undefined: Execute\n'
    )

    hints = _build_source_hints(traceback_err, temp_git_repo)

    py_hint = next((h for h in hints if h["file"] == "pkg/worker.py"), None)
    assert py_hint is not None
    assert py_hint["line"] == 10
    assert "10: raise ValueError('something went wrong')" in py_hint["snippet"]

    go_hint = next((h for h in hints if h["file"] == "pkg/main.go"), None)
    assert go_hint is not None
    assert go_hint["line"] == 20
    assert "20: func Execute() {}" in go_hint["snippet"]


def test_engine_level_compile_failure_retry_feedback_contains_hint(temp_git_repo):
    """Engine-level test: after a compile failure the retry feedback contains the hint for the file named in error."""
    ws = Workspace(repo_dir=temp_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-engine", description="Add component", scope=["src/comp.ts"], expects_tests=False)
    wt_path = ws.create_subgoal_worktree(subgoal.id)

    src_dir = wt_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    comp_file = src_dir / "comp.ts"
    comp_lines = [f"// code line {i}" for i in range(1, 25)]
    comp_lines[4] = "const x: number = 'invalid string';"
    comp_file.write_text("\n".join(comp_lines) + "\n", encoding="utf-8")
    ws.stage_file_mutation("src/comp.ts", "\n".join(comp_lines) + "\n")

    error_msg = "src/comp.ts(5,7): error TS2322: Type 'string' is not assignable to type 'number'."

    compile_telemetry = {
        "ecosystem": "js_ts",
        "command": ["npm", "run", "typecheck"],
        "exit_code": 1,
        "stdout_tail": "",
        "stderr_tail": error_msg,
        "output_tail": error_msg,
        "new_errors": [error_msg],
        "base_errors": [],
        "outcome": CompileOutcome.FAILED.value,
        "detail": f"Compile failed: {error_msg}",
    }

    mech_result = MechanicalCheckResult(
        passed=False,
        failed_check="compile",
        detail=f"Compile failed: {error_msg}",
        checks_run=["compile"],
        checks={"compile": {"ran": True, "passed": False, "detail": error_msg}},
        compile_outcome=compile_telemetry,
        test_runner_outcome={"ran": False},
    )

    state: State = {
        "status": "in_progress",
        "gate_status": "none",
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "current_subgoal": subgoal,
        "plan_queue": [subgoal],
        "trajectory": [],
        "failed_attempts": [],
        "current_worktree_path": str(wt_path),
        "base_commit": ws.base_commit,
    }

    with patch.object(ws, "run_mechanical_checks", return_value=mech_result):
        res_state = node_gate(state, workspace=ws)

    # 1. State incremented strike for mechanical failure
    assert res_state["mechanical_strike_count"] == 1
    assert res_state["gate_status"] == "mechanical_failure"

    # 2. Last feedback contains the hint
    last_feedback = res_state.get("last_feedback", "")
    assert "src/comp.ts" in last_feedback
    assert "read src/comp.ts before retrying" in last_feedback

    # 3. Trajectory failure evidence contains source_hints
    traj_gate = res_state["trajectory"][-1]
    fail_ev = traj_gate.get("failure_evidence") or {}
    source_hints = fail_ev.get("source_hints") or []
    assert len(source_hints) > 0
    hint_item = source_hints[0]
    assert hint_item["file"] == "src/comp.ts"
    assert hint_item["confidence"] == "hint, may be incomplete"
    assert "5: const x: number = 'invalid string';" in hint_item["snippet"]
