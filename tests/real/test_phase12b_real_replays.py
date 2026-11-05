"""Phase 12B: real-repository and real-process replays.

These tests create tiny disposable repositories, execute the intended runner
when its toolchain is installed, and skip with an explicit reason otherwise.
They do not modify the repository containing this test suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jev.mech_evidence import BaseStatus, MechReason, MechStatus
from jev.models import Subgoal
from jev.process_runner import run_command
from jev.runner_registry import is_zero_test_marker_match, load_registry, parse_test_counts_summary
from jev.workspace import Workspace



# Pinned exact versions for dependency-backed templates (stable releases as of Phase 12B):
# jest: 29.7.0 (v29 stable text reporter)
# mocha: 10.8.2 (v10 stable spec reporter)
# vitest: 1.6.0 (v1 stable reporter)
NPM_RUNNER_SPECS = {
    "jest": {
        "command": lambda: [shutil.which("npx") or "npx", "jest"],
        "pass_files": {
            "package.json": json.dumps({"name": "phase12b-jest", "private": True, "devDependencies": {"jest": "29.7.0"}}, indent=2) + "\n",
            "real.test.js": "test('real repository', () => { expect(2 + 2).toBe(4); });\n",
        },
        "fail_files": {
            "package.json": json.dumps({"name": "phase12b-jest", "private": True, "devDependencies": {"jest": "29.7.0"}}, indent=2) + "\n",
            "real.test.js": "test('real repository', () => { expect(2 + 2).toBe(5); });\n",
        },
        "zero_files": {
            "package.json": json.dumps({"name": "phase12b-jest", "private": True, "devDependencies": {"jest": "29.7.0"}}, indent=2) + "\n",
        },
    },
    "mocha": {
        "command": lambda: [shutil.which("npx") or "npx", "mocha"],
        "pass_files": {
            "package.json": json.dumps({"name": "phase12b-mocha", "private": True, "devDependencies": {"mocha": "10.8.2"}}, indent=2) + "\n",
            "test/real.test.js": "const assert = require('assert'); it('real repository', () => { assert.equal(2 + 2, 4); });\n",
        },
        "fail_files": {
            "package.json": json.dumps({"name": "phase12b-mocha", "private": True, "devDependencies": {"mocha": "10.8.2"}}, indent=2) + "\n",
            "test/real.test.js": "const assert = require('assert'); it('real repository', () => { assert.equal(2 + 2, 5); });\n",
        },
        "zero_files": {
            "package.json": json.dumps({"name": "phase12b-mocha", "private": True, "devDependencies": {"mocha": "10.8.2"}}, indent=2) + "\n",
        },
    },
    "vitest": {
        "command": lambda: [shutil.which("npx") or "npx", "vitest", "run"],
        "pass_files": {
            "package.json": json.dumps({"name": "phase12b-vitest", "private": True, "devDependencies": {"vitest": "1.6.0"}}, indent=2) + "\n",
            "real.test.js": "import { test, expect } from 'vitest'; test('real repository', () => { expect(2 + 2).toBe(4); });\n",
        },
        "fail_files": {
            "package.json": json.dumps({"name": "phase12b-vitest", "private": True, "devDependencies": {"vitest": "1.6.0"}}, indent=2) + "\n",
            "real.test.js": "import { test, expect } from 'vitest'; test('real repository', () => { expect(2 + 2).toBe(5); });\n",
        },
        "zero_files": {
            "package.json": json.dumps({"name": "phase12b-vitest", "private": True, "devDependencies": {"vitest": "1.6.0"}}, indent=2) + "\n",
        },
    },
}

PROFILE_SPECS = {
    "pytest": {
        "tool": None,
        "command": lambda: [sys.executable, "-m", "pytest", "-q"],
        "files": {
            "pytest.ini": "[pytest]\n",
            "tests/test_real.py": "def test_real_repository():\n    assert 2 + 2 == 4\n",
        },
    },
    "node-native-test": {
        "tool": "node",
        "command": lambda: ["node", "--test"],
        "files": {
            "package.json": '{"name":"phase12b-node","private":true}\n',
            "real.test.js": "import test from 'node:test';\nimport assert from 'node:assert';\ntest('real repository', () => assert.equal(2 + 2, 4));\n",
        },
    },
    "bun-test": {
        "tool": "bun",
        "command": lambda: ["bun", "test"],
        "files": {
            "package.json": '{"name":"phase12b-bun","private":true}\n',
            "real.test.js": "import { test, expect } from 'bun:test';\ntest('real repository', () => expect(2 + 2).toBe(4));\n",
        },
    },
    "go-test": {
        "tool": "go",
        "command": lambda: ["go", "test", "./..."],
        "files": {
            "go.mod": "module phase12b.example\n\ngo 1.18\n",
            "real_test.go": "package phase12b\n\nimport \"testing\"\n\nfunc TestRealRepository(t *testing.T) {\n\tif 2+2 != 4 { t.Fatal(\"arithmetic\") }\n}\n",
        },
    },
    "cargo-test": {
        "tool": "cargo",
        "command": lambda: ["cargo", "test"],
        "files": {
            "Cargo.toml": "[package]\nname = \"phase12b\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
            "src/lib.rs": "#[cfg(test)]\nmod tests {\n    #[test]\n    fn real_repository() { assert_eq!(2 + 2, 4); }\n}\n",
        },
    },
}

ALL_PROFILE_TOOLS = {
    "pytest": None,
    "node-native-test": "node",
    "jest": "node",
    "mocha": "node",
    "vitest": "node",
    "bun-test": "bun",
    "deno-test": "deno",
    "go-test": "go",
    "cargo-test": "cargo",
    "maven-surefire": "mvn",
    "gradle-test": "gradle",
    "dotnet-test": "dotnet",
    "cmake-ctest": "ctest",
    "ruby-rake": "ruby",
    "phpunit": "php",
    "swiftpm-test": "swift",
    "mix-test": "mix",
    "xcode-test": "xcodebuild",
    "terraform": "terraform",
    "ansible": "ansible-lint",
    "helm": "helm",
}


def _write_repo(repo: Path, files: dict[str, str]) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Phase 12B"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "phase12b@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial real repository"], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _run(repo: Path, argv: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(argv[0]) if argv else None
    resolved_argv = [executable, *argv[1:]] if executable else argv
    return subprocess.run(
        resolved_argv,
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _npm_install(repo: Path) -> subprocess.CompletedProcess[str]:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("Toolchain 'npm' not found for npm-backed profile")
    result = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180.0,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout)[-300:].strip()
        pytest.skip(f"npm install failed for profile: {tail}")
    return result


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("profile_id", sorted(ALL_PROFILE_TOOLS))
def test_real_profile_repository_passes_or_skips_with_reason(tmp_path, profile_id):
    """Every registry profile has a real-repository replay or an explicit skip."""
    tool = ALL_PROFILE_TOOLS[profile_id]
    if tool and shutil.which(tool) is None:
        pytest.skip(f"Toolchain '{tool}' not found for profile '{profile_id}'")
    if profile_id == "pytest":
        command = [sys.executable, "-m", "pytest", "-q"]
        files = PROFILE_SPECS[profile_id]["files"]
    elif profile_id in PROFILE_SPECS:
        command = PROFILE_SPECS[profile_id]["command"]()
        files = PROFILE_SPECS[profile_id]["files"]
    elif profile_id in NPM_RUNNER_SPECS:
        command = NPM_RUNNER_SPECS[profile_id]["command"]()
        files = NPM_RUNNER_SPECS[profile_id]["pass_files"]
    else:
        pytest.skip(
            f"Profile '{profile_id}' has no dependency-free repository template; real CI fixture is deferred"
        )

    repo = tmp_path / profile_id
    _write_repo(repo, files)
    if profile_id in NPM_RUNNER_SPECS:
        _npm_install(repo)
    result = _run(repo, command)
    assert result.returncode == 0, _combined(result)

    profile = load_registry().get_profile(profile_id)
    assert profile is not None
    parsed = parse_test_counts_summary(profile, _combined(result), exit_code=result.returncode)
    assert parsed["has_positive_pass"] is True, _combined(result)
    assert parsed["passed"] == 1, (
        f"Profile '{profile_id}': expected 1 passed test, got {parsed['passed']}; parsed: {parsed}; output: {_combined(result)}"
    )


@pytest.mark.parametrize(
    ("profile_id", "command", "files", "expected_exit"),
    [
        ("pytest", [sys.executable, "-m", "pytest", "-q"], {"pytest.ini": "[pytest]\n"}, 5),
        ("node-native-test", ["node", "--test"], {"package.json": "{}\n"}, 0),
        ("bun-test", ["bun", "test"], {"package.json": "{}\n"}, 1),
    ],
)
def test_real_zero_test_exit_behavior_is_downgrade_only(
    tmp_path, profile_id, command, files, expected_exit
):
    """Real zero-test exits never become positive counted evidence."""
    tool = ALL_PROFILE_TOOLS[profile_id]
    if tool and shutil.which(tool) is None:
        pytest.skip(f"Toolchain '{tool}' not found for profile '{profile_id}'")
    repo = tmp_path / f"zero-{profile_id}"
    _write_repo(repo, files)
    result = _run(repo, command)
    output = _combined(result)
    assert result.returncode == expected_exit, output
    profile = load_registry().get_profile(profile_id)
    assert profile is not None
    parsed = parse_test_counts_summary(profile, output, exit_code=result.returncode)
    assert parsed["has_positive_pass"] is False, output
    if result.returncode != 0:
        # Bun 1.3 reports an empty discovery as "0 test files matching" rather
        # than the older "No tests found!" marker used by the fixture corpus.
        assert is_zero_test_marker_match(profile, output, result.returncode) is True or "0 test files matching" in output, output


def test_real_baseline_failure_is_not_misclassified_as_worker_regression(tmp_path):
    """A pre-existing real test failure remains a same-failure PASS comparison."""
    repo = tmp_path / "baseline-failure"
    head = _write_repo(
        repo,
        {
            "pytest.ini": "[pytest]\n",
            "tests/test_existing.py": "def test_existing():\n    assert False\n",
            "app.py": "value = 1\n",
        },
    )
    ws = Workspace(repo_dir=repo, test_timeout=30)
    ws.stage_file_mutation("app.py", "value = 2\n")
    evidence = ws.evaluate_tests(
        Subgoal(id="baseline-failure", description="unrelated change", scope=["app.py"]),
        base_commit=head,
        test_policy="always",
    )
    assert evidence is not None
    assert evidence.mech_status == MechStatus.PASS
    assert evidence.base_status == BaseStatus.FAIL
    assert evidence.reason is None


def test_clean_exit_with_unparseable_output_is_not_counted(tmp_path):
    """A clean real process with no test summary remains exit-only evidence."""
    repo = tmp_path / "unparseable"
    _write_repo(repo, {"run_tests.py": "print('deployment checks complete')\n"})
    result = _run(repo, [sys.executable, "run_tests.py"])
    assert result.returncode == 0
    pytest_profile = load_registry().get_profile("pytest")
    assert pytest_profile is not None
    parsed = parse_test_counts_summary(pytest_profile, _combined(result), exit_code=0)
    assert parsed["has_positive_pass"] is False
    assert parsed["passed"] == 0


def test_real_bun_build_output_is_discarded_from_engine_scope(tmp_path):
    """A real Bun test may create .bun-build output, which engine cleanup removes."""
    if shutil.which("bun") is None:
        pytest.skip("Toolchain 'bun' not found for generated-output replay")
    repo = tmp_path / "bun-build"
    head = _write_repo(
        repo,
        {
            "package.json": '{"name":"phase12b-generated","private":true}\n',
            "real.test.js": (
                "import { mkdirSync, writeFileSync } from 'node:fs';\n"
                "import { test, expect } from 'bun:test';\n"
                "test('generates bun output', () => {\n"
                "  mkdirSync('.bun-build', { recursive: true });\n"
                "  writeFileSync('.bun-build/generated.js', 'export const generated = true;\\n');\n"
                "  expect(true).toBe(true);\n"
                "});\n"
            ),
            "worker.txt": "preserve me\n",
        },
    )
    ws = Workspace(repo_dir=repo)
    ws.create_subgoal_worktree("bun-generated", base_ref=head)
    result = _run(ws.worktree_dir, ["bun", "test"])
    assert result.returncode == 0, _combined(result)
    generated = ws.worktree_dir / ".bun-build" / "generated.js"
    assert generated.exists()

    ws.record_engine_touched(".bun-build/generated.js")
    ws.discard_engine_touched()
    assert not generated.exists()
    assert (ws.worktree_dir / "worker.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert head


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def test_timeout_descendants_are_absent_after_twenty_real_replays(tmp_path):
    """Repeat real descendant cleanup at least twenty times on every CI OS."""
    child_script = tmp_path / "spawn_child.py"
    child_script.write_text(
        "import os, subprocess, sys, time\n"
        "pid_file = sys.argv[1]\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "open(pid_file, 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    for index in range(20):
        pid_file = tmp_path / f"child-{index}.pid"
        result = run_command(
            [sys.executable, str(child_script), str(pid_file)],
            cwd=tmp_path,
            timeout=0.5,
            env=os.environ.copy(),
        )
        assert result.timed_out is True
        assert result.descendants_remaining == 0
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_is_alive(child_pid):
            time.sleep(0.05)
        assert _pid_is_alive(child_pid) is False, f"replay {index}: child {child_pid} survived"


@pytest.mark.parametrize("profile_id", sorted(NPM_RUNNER_SPECS))
def test_real_npm_profile_failing_variant_is_not_positive(tmp_path, profile_id):
    """Failing variant of npm-backed profile is not positive."""
    tool = ALL_PROFILE_TOOLS[profile_id]
    if tool and shutil.which(tool) is None:
        pytest.skip(f"Toolchain '{tool}' not found for profile '{profile_id}'")
    spec = NPM_RUNNER_SPECS[profile_id]
    repo = tmp_path / f"fail-{profile_id}"
    _write_repo(repo, spec["fail_files"])
    _npm_install(repo)
    result = _run(repo, spec["command"]())
    output = _combined(result)
    assert result.returncode != 0, f"Expected failing exit code for {profile_id}, got 0: {output}"
    profile = load_registry().get_profile(profile_id)
    assert profile is not None
    parsed = parse_test_counts_summary(profile, output, exit_code=result.returncode)
    assert parsed["has_positive_pass"] is False, (
        f"Failing variant parsed positive for {profile_id}: {parsed}; output: {output}"
    )


@pytest.mark.parametrize("profile_id", sorted(NPM_RUNNER_SPECS))
def test_real_npm_profile_zero_test_variant_is_not_positive(tmp_path, profile_id):
    """Zero-test variant of npm-backed profile is not positive."""
    tool = ALL_PROFILE_TOOLS[profile_id]
    if tool and shutil.which(tool) is None:
        pytest.skip(f"Toolchain '{tool}' not found for profile '{profile_id}'")
    spec = NPM_RUNNER_SPECS[profile_id]
    repo = tmp_path / f"zero-{profile_id}"
    _write_repo(repo, spec["zero_files"])
    _npm_install(repo)
    result = _run(repo, spec["command"]())
    output = _combined(result)
    profile = load_registry().get_profile(profile_id)
    assert profile is not None
    parsed = parse_test_counts_summary(profile, output, exit_code=result.returncode)
    assert parsed["has_positive_pass"] is False, (
        f"Zero-test variant parsed positive for {profile_id}: {parsed}; output: {output}"
    )
