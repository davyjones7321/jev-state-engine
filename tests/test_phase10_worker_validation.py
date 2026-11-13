"""tests/test_phase10_worker_validation.py

Comprehensive tests for Phase 10: Worker validation across profiles.
Requirements verified:
- Accept only engine-selected registry or config commands (no shell).
- Use central runner (run_command) with bounded budgets (calls <= 4, bounded timeouts).
- Discard generated compiler artifacts, leaving workspace clean.
- Preserve worker staged files intact.
- Return bounded diagnostics with evidence level as advisory feedback only:
  [evidence: EXIT_ONLY], [evidence: NAMED], [evidence: NONE].
- For profiles without a build/typecheck command (e.g. Ruby, PHP), return a clear
  skipped message; never pretend validation ran.
- Source-location hints included as non-authoritative.
- Cannot change gate status or strikes (advisory to worker only).
- Tests across profiles: TypeScript/Bun, Go, Rust, Java, .NET, CMake, Ruby, PHP,
  SwiftPM, Elixir where toolchains exist (skip with reason otherwise).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from jev.engine import MAX_SELF_CHECK_OUTPUT_CHARS, node_gate, node_implement
from jev.models import State, Subgoal
from jev.process_runner import run_command
from jev.runner_registry import (
    RunnerRegistry,
    load_registry,
    resolve_build_command,
)
from jev.workspace import (
    ALL_SOURCE_EXTENSIONS,
    CMakeCompileHandler,
    CompileOutcome,
    DotnetCompileHandler,
    ECOSYSTEM_COMPILE_HANDLERS,
    ElixirCompileHandler,
    JsTsCompileHandler,
    MavenCompileHandler,
    PhpCompileHandler,
    RubyCompileHandler,
    SwiftPMCompileHandler,
    Workspace,
)


@pytest.fixture
def clean_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, check=True)
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


class ScriptedLLM:
    def __init__(self, responses: List[AIMessage]):
        self.responses = list(responses)
        self.invocations: List[Any] = []

    def bind_tools(self, tools: Any) -> ScriptedLLM:
        return self

    def invoke(self, messages: Any) -> AIMessage:
        self.invocations.append(messages)
        if self.responses:
            return self.responses.pop(0)
        return AIMessage(content="done", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_done"}])


def _init_implement_state(ws: Workspace, subgoal: Subgoal) -> State:
    return {
        "ticket": "Validation across profiles",
        "plan_queue": [subgoal],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "unmeasured_retry_count": 0,
        "trajectory": [],
        "failed_attempts": [],
        "status": "in_progress",
        "gate_status": "none",
        "current_worktree_path": str(ws.worktree_dir),
        "base_commit": ws.base_commit,
    }


# ==============================================================================
# 1. Profile Detection and Engine-Selected Commands (No Shell)
# ==============================================================================

def test_resolve_build_command_engine_selected_no_shell(tmp_path: Path):
    """Engine-selected commands are list of args (no shell) for profiles with build commands."""
    # Dotnet
    p_dotnet = tmp_path / "dotnet_proj"
    p_dotnet.mkdir()
    (p_dotnet / "app.csproj").write_text("<Project/>", encoding="utf-8")
    cmd_dotnet = resolve_build_command(p_dotnet)
    assert cmd_dotnet.runner_profile_id == "dotnet-test"
    assert cmd_dotnet.command == ["dotnet", "build"]
    assert isinstance(cmd_dotnet.command, list)

    # CMake
    p_cmake = tmp_path / "cmake_proj"
    p_cmake.mkdir()
    (p_cmake / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8")
    cmd_cmake = resolve_build_command(p_cmake)
    assert cmd_cmake.runner_profile_id == "cmake-ctest"
    assert cmd_cmake.command == ["cmake", "--build", "build"]
    assert isinstance(cmd_cmake.command, list)

    # SwiftPM
    p_swift = tmp_path / "swift_proj"
    p_swift.mkdir()
    (p_swift / "Package.swift").write_text("// swift-tools-version:5.5\n", encoding="utf-8")
    cmd_swift = resolve_build_command(p_swift)
    assert cmd_swift.runner_profile_id == "swiftpm-test"
    assert cmd_swift.command == ["swift", "build"]

    # Elixir
    p_elixir = tmp_path / "elixir_proj"
    p_elixir.mkdir()
    (p_elixir / "mix.exs").write_text("defmodule App.MixProject do\nend\n", encoding="utf-8")
    cmd_elixir = resolve_build_command(p_elixir)
    assert cmd_elixir.runner_profile_id == "mix-test"
    assert cmd_elixir.command == ["mix", "compile"]


# ==============================================================================
# 2. Profiles Without Build Command: Ruby & PHP Return Clear Skipped Message
# ==============================================================================

def test_ruby_profile_returns_skipped_message_never_pretends_validation_ran(clean_git_repo: Path):
    """Ruby profile has no build/typecheck command: returns clear skipped message with [evidence: NONE]."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-rb", description="Ruby task", scope=["app.rb"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "Rakefile").write_text("task :default\n", encoding="utf-8")
    ws.stage_file_mutation("app.rb", "puts 'hello'\n")

    res = ws.validate_changes()
    assert res["outcome"] == "skipped"
    assert res["evidence_level"] == "none"
    assert "ruby-rake" in res["detail"]
    assert "no build or typecheck command configured" in res["detail"]
    assert "validation skipped" in res["detail"]

    # In engine
    state = _init_implement_state(ws, subgoal)
    responses = [
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
        AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
    ]
    out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))
    vc_call = next(c for c in out_state["trajectory"][-1]["tool_calls"] if c["name"] == "validate_changes")
    assert "Self-check: skipped" in vc_call["result"]
    assert "[evidence: NONE]" in vc_call["result"]
    assert "ruby-rake" in vc_call["result"]
    assert "no build or typecheck command configured" in vc_call["result"]


def test_php_profile_returns_skipped_message_never_pretends_validation_ran(clean_git_repo: Path):
    """PHP profile has no build/typecheck command: returns clear skipped message with [evidence: NONE]."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-php", description="PHP task", scope=["src/index.php"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "phpunit.xml").write_text("<phpunit/>\n", encoding="utf-8")
    (wt / "src").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/index.php", "<?php echo 'hello';\n")

    res = ws.validate_changes()
    assert res["outcome"] == "skipped"
    assert res["evidence_level"] == "none"
    assert "phpunit" in res["detail"]
    assert "no build or typecheck command configured" in res["detail"]
    assert "validation skipped" in res["detail"]

    # In engine
    state = _init_implement_state(ws, subgoal)
    responses = [
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
        AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
    ]
    out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))
    vc_call = next(c for c in out_state["trajectory"][-1]["tool_calls"] if c["name"] == "validate_changes")
    assert "Self-check: skipped" in vc_call["result"]
    assert "[evidence: NONE]" in vc_call["result"]
    assert "phpunit" in vc_call["result"]
    assert "no build or typecheck command configured" in vc_call["result"]


# ==============================================================================
# 3. TypeScript / Bun Validation
# ==============================================================================

def test_typescript_bun_validation(clean_git_repo: Path):
    """TypeScript / Bun profile validation with central runner and advisory diagnostics."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-bun", description="Bun TS task", scope=["src/app.ts"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "bunfig.toml").write_text("# bun config\n", encoding="utf-8")
    (wt / "package.json").write_text('{"name": "test-bun", "scripts": {"typecheck": "tsc --noEmit"}}\n', encoding="utf-8")
    (wt / "src").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/app.ts", "export const value: number = 42;\n")

    # If bun is installed locally, check real or simulated command
    bun_bin = shutil.which("bun")
    if not bun_bin:
        pytest.skip("bun toolchain not installed")

    res = ws.validate_changes()
    assert res["command"] == ["bun", "run", "typecheck"]
    # Handled via bun runner without crash
    assert res["outcome"] in ("passed", "failed", "skipped")
    assert res["staged_preserved"] is True


def test_typescript_bun_error_diagnostics_and_evidence(clean_git_repo: Path):
    """TypeScript/Bun error output returns bounded diagnostics with [evidence: NAMED] and hints."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-bun-err", description="Bun error task", scope=["src/app.ts"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "bunfig.toml").write_text("# bun config\n", encoding="utf-8")
    (wt / "tsconfig.json").write_text("{}", encoding="utf-8")
    (wt / "node_modules").mkdir(parents=True, exist_ok=True)
    (wt / "src").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/app.ts", "export const broken: number = 'string';\n")

    error_output = "src/app.ts:1:14 - error TS2322: Type 'string' is not assignable to type 'number'."
    def fake_compile_run(cmd, cwd, touched, is_base=False, **kwargs):
        if is_base:
            return 0, "", ""
        return 1, "", error_output

    with patch.object(
        JsTsCompileHandler,
        "run_command",
        side_effect=fake_compile_run,
    ):
        state = _init_implement_state(ws, subgoal)
        responses = [
            AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
            AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
        ]
        out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))

    vc_call = next(c for c in out_state["trajectory"][-1]["tool_calls"] if c["name"] == "validate_changes")
    res_text = vc_call["result"]
    assert "Self-check: failed" in res_text
    assert "[evidence: NAMED]" in res_text
    assert "src/app.ts" in res_text
    assert "Source location hints (non-authoritative):" in res_text
    assert "src/app.ts:1:14: [TS2322]" in res_text


# ==============================================================================
# 4. Rust, Go, Java, .NET, CMake, SwiftPM, Elixir Profiles
# ==============================================================================

def test_rust_cargo_validation(clean_git_repo: Path):
    """Rust profile: cargo check executed via central runner."""
    cargo_bin = shutil.which("cargo")
    if not cargo_bin:
        pytest.skip("cargo toolchain not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-rs", description="Rust task", scope=["src/lib.rs"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "Cargo.toml").write_text(
        '[package]\nname = "test_pkg"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (wt / "src").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/lib.rs", "pub fn answer() -> i32 { 42 }\n")

    res = ws.validate_changes()
    assert res["command"] == ["cargo", "check"]
    assert res["outcome"] == "passed"
    assert res["evidence_level"] == "exit_only"
    assert res["staged_preserved"] is True


def test_go_validation(clean_git_repo: Path):
    """Go profile: go build ./... executed where toolchain exists; skip with reason otherwise."""
    go_bin = shutil.which("go")
    if not go_bin:
        pytest.skip("Go toolchain (go) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-go", description="Go task", scope=["main.go"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "go.mod").write_text("module example.com/test\n\ngo 1.20\n", encoding="utf-8")
    ws.stage_file_mutation("main.go", "package main\n\nfunc main() {}\n")

    res = ws.validate_changes()
    assert res["command"] == ["go", "build", "./..."]
    assert res["outcome"] == "passed"
    assert res["evidence_level"] == "exit_only"


def test_java_maven_validation(clean_git_repo: Path):
    """Java Maven profile: mvn compile executed where toolchain exists; skip with reason otherwise."""
    mvn_bin = shutil.which("mvn")
    if not mvn_bin:
        pytest.skip("Maven toolchain (mvn) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-mvn", description="Java task", scope=["src/main/java/App.java"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    (wt / "src" / "main" / "java").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/main/java/App.java", "public class App {}\n")

    res = ws.validate_changes()
    assert res["command"] is not None
    assert "mvn" in res["command"][0]


def test_dotnet_validation(clean_git_repo: Path):
    """.NET profile: dotnet build executed where toolchain exists; skip with reason otherwise."""
    dotnet_bin = shutil.which("dotnet")
    if not dotnet_bin:
        pytest.skip(".NET SDK (dotnet) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-dotnet", description=".NET task", scope=["Class1.cs"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "TestApp.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n',
        encoding="utf-8",
    )
    ws.stage_file_mutation("Class1.cs", "public class Class1 {}\n")

    res = ws.validate_changes()
    assert res["command"] == ["dotnet", "build"]
    assert res["staged_preserved"] is True


def test_cmake_validation(clean_git_repo: Path):
    """CMake profile: cmake build executed where toolchain exists; skip with reason otherwise."""
    cmake_bin = shutil.which("cmake")
    if not cmake_bin:
        pytest.skip("CMake toolchain (cmake) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-cmake", description="CMake task", scope=["main.c"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\nproject(TestApp C)\nadd_executable(test_app main.c)\n",
        encoding="utf-8",
    )
    ws.stage_file_mutation("main.c", "int main(void) { return 0; }\n")

    res = ws.validate_changes()
    assert res["command"] is not None
    assert "cmake" in res["command"][0]


def test_swiftpm_validation(clean_git_repo: Path):
    """SwiftPM profile: swift build executed where toolchain exists; skip with reason otherwise."""
    swift_bin = shutil.which("swift")
    if not swift_bin:
        pytest.skip("Swift toolchain (swift) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-swift", description="Swift task", scope=["Sources/App/main.swift"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "Package.swift").write_text("// swift-tools-version:5.5\n", encoding="utf-8")
    (wt / "Sources" / "App").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("Sources/App/main.swift", 'print("hello")\n')

    res = ws.validate_changes()
    assert res["command"] == ["swift", "build"]


def test_elixir_validation(clean_git_repo: Path):
    """Elixir profile: mix compile executed where toolchain exists; skip with reason otherwise."""
    mix_bin = shutil.which("mix")
    if not mix_bin:
        pytest.skip("Elixir toolchain (mix) not installed")

    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-ex", description="Elixir task", scope=["lib/app.ex"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "mix.exs").write_text("defmodule App.MixProject do\n  use Mix.Project\nend\n", encoding="utf-8")
    (wt / "lib").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("lib/app.ex", "defmodule App do\nend\n")

    res = ws.validate_changes()
    assert res["command"] == ["mix", "compile"]


# ==============================================================================
# 5. Missing Tool Handling: Clean Skipped / Env Not Ready (No Crash, No Strike)
# ==============================================================================

def test_missing_tool_returns_env_not_ready_without_crash_or_strike(clean_git_repo: Path):
    """Missing toolchain produces environment-not-ready skipped message with 0 strikes."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-missing-tool", description="Missing tool task", scope=["Class1.cs"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "App.csproj").write_text("<Project/>\n", encoding="utf-8")
    ws.stage_file_mutation("Class1.cs", "public class Class1 {}\n")

    # Simulate dotnet not in PATH
    with patch("shutil.which", return_value=None):
        res = ws.validate_changes()
        assert res["outcome"] == "skipped"
        assert res["evidence_level"] == "none"
        assert "Environment not ready" in res["detail"] or "not found" in res["detail"]

        # Run in engine and verify strikes remain 0
        state = _init_implement_state(ws, subgoal)
        responses = [
            AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
            AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
        ]
        out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))
        assert out_state["mechanical_strike_count"] == 0
        assert out_state["semantic_strike_count"] == 0
        vc_call = next(c for c in out_state["trajectory"][-1]["tool_calls"] if c["name"] == "validate_changes")
        assert "Self-check: skipped" in vc_call["result"]
        assert "[evidence: NONE]" in vc_call["result"]


# ==============================================================================
# 6. Artifact Cleanup: Generated Files Discarded, Workspace Clean
# ==============================================================================

def test_generated_artifacts_discarded_workspace_clean(clean_git_repo: Path):
    """Compiler-generated files are discarded leaving worktree clean."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-art-clean", description="Artifact discard", scope=["src/main.rs"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "Cargo.toml").write_text('[package]\nname = "test_clean"\nversion = "0.1.0"\n', encoding="utf-8")
    (wt / "src").mkdir(parents=True, exist_ok=True)
    ws.stage_file_mutation("src/main.rs", "fn main() {}\n")

    # Simulate compilation that creates compiler artifacts
    def fake_run_command(cmd, cwd, touched, is_base=False):
        target_dir = wt / "target" / "debug"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "app.exe").write_text("binary", encoding="utf-8")
        (target_dir / "app.pdb").write_text("symbols", encoding="utf-8")
        (wt / "obj_temp.o").write_text("obj", encoding="utf-8")
        return 0, "Finished dev [unoptimized + debuginfo] target(s) in 0.10s", ""

    with patch.object(ws, "check_compile") as mock_comp:
        # Instead mock the handler directly
        with patch.object(
            ECOSYSTEM_COMPILE_HANDLERS[2],  # RustCompileHandler
            "run_command",
            side_effect=fake_run_command,
        ):
            res = ws.validate_changes()

    # Generated files must be cleaned up
    assert not (wt / "target" / "debug" / "app.exe").exists()
    assert not (wt / "obj_temp.o").exists()
    # Workspace should not have stray target directory
    assert not (wt / "target" / "debug").exists()


# ==============================================================================
# 7. Staged Files Preserved Intact
# ==============================================================================

def test_staged_files_preserved_intact(clean_git_repo: Path):
    """Worker-staged files remain completely intact after validation passes or fails."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-staged-intact", description="Staged intact", scope=["src/mod.py", "config.json"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "src").mkdir(parents=True, exist_ok=True)
    py_content = "def calculate(a: int) -> int:\n    return a * 2\n"
    cfg_content = '{"key": "value"}\n'

    ws.stage_file_mutation("src/mod.py", py_content)
    ws.stage_file_mutation("config.json", cfg_content)

    res = ws.validate_changes()
    assert res["staged_preserved"] is True
    assert (wt / "src" / "mod.py").read_text(encoding="utf-8") == py_content
    assert (wt / "config.json").read_text(encoding="utf-8") == cfg_content
    assert "src/mod.py" in ws.worker_staged_paths
    assert "config.json" in ws.worker_staged_paths


# ==============================================================================
# 8. Invariant: Cannot Change Gate Status or Strikes
# ==============================================================================

def test_validate_changes_cannot_change_gate_status_or_strikes(clean_git_repo: Path):
    """validate_changes is advisory only: failure or timeout never increments strikes or changes gate_status."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-no-strike", description="No strike invariant", scope=["src/calc.py"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    (wt / "src").mkdir(parents=True, exist_ok=True)
    # Broken syntax
    ws.stage_file_mutation("src/calc.py", "def broken(\n")

    state = _init_implement_state(ws, subgoal)
    state["mechanical_strike_count"] = 0
    state["semantic_strike_count"] = 0
    state["gate_status"] = "none"

    responses = [
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_2"}]),
        AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
    ]

    out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))

    # Assert zero strike change
    assert out_state["mechanical_strike_count"] == 0
    assert out_state["semantic_strike_count"] == 0
    assert out_state["gate_status"] == "none"

    # Trajectory shows both calls failed advisory check
    traj = out_state["trajectory"][-1]
    vc_calls = [c for c in traj["tool_calls"] if c["name"] == "validate_changes"]
    assert len(vc_calls) == 2
    for call in vc_calls:
        assert "Self-check: failed" in call["result"]
        assert "(advisory feedback only; authoritative Tier 0 gating runs in node_gate)" in call["result"]


# ==============================================================================
# 9. Budgets: Call Limit & Diagnostics Output Cap
# ==============================================================================

def test_call_budget_and_diagnostics_capped(clean_git_repo: Path):
    """Exceeding 4 validation calls returns skipped (limit reached) and diagnostics are bounded."""
    ws = Workspace(repo_dir=clean_git_repo, strict_provenance=True)
    subgoal = Subgoal(id="sg-budget", description="Budget task", scope=["main.py"], expects_tests=False)
    wt = ws.create_subgoal_worktree(subgoal.id)

    ws.stage_file_mutation("main.py", "x = 1\n")
    state = _init_implement_state(ws, subgoal)

    responses = [
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_1"}]),
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_2"}]),
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_3"}]),
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_4"}]),
        AIMessage(content="", tool_calls=[{"name": "validate_changes", "args": {}, "id": "vc_5"}]),
        AIMessage(content="", tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "sub_1"}]),
    ]

    out_state = node_implement(state, workspace=ws, llm=ScriptedLLM(responses))
    traj = out_state["trajectory"][-1]
    vc_calls = [c for c in traj["tool_calls"] if c["name"] == "validate_changes"]
    assert len(vc_calls) == 5

    # 5th call is skipped
    assert "Self-check: skipped (limit reached)" in vc_calls[4]["result"]
    assert "[evidence: NONE]" in vc_calls[4]["result"]
    assert "validation call budget exceeded" in vc_calls[4]["result"]

    # All outputs are bounded
    for call in vc_calls:
        assert len(call["result"]) <= MAX_SELF_CHECK_OUTPUT_CHARS + 200
