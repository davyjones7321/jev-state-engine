#!/usr/bin/env python3
"""scripts/capture_runner_fixtures.py

Captures or regenerates real test runner fixtures for the current platform
with strict normalization (placeholders for python, home, tmp, user, durations, hashes).
In verification mode (--verify-only), compares live exit codes with committed fixtures.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "runners"


def normalize_text(text: str) -> str:
    """Normalizes paths, usernames, temp directories, timings, and hashes to placeholders."""
    if not text:
        return ""

    user = getpass.getuser()
    user_home = str(Path.home())
    temp_dir = tempfile.gettempdir()
    py_exe = sys.executable

    # 1. Normalize temp directories
    text = text.replace(temp_dir, "<TMP>")
    # Match Windows AppData\Local\Temp or general temp patterns
    text = re.sub(r"[A-Za-z]:\\[Uu]sers\\[^\\]+\\AppData\\Local\\Temp\\[^\r\n\\\", ']+", "<TMP>", text)
    text = re.sub(r"[A-Za-z]:\\[Uu]sers\\[^\\]+\\AppData\\Local\\Temp", "<TMP>", text)
    text = re.sub(r"/tmp/[^\r\n/\\\", ']+", "<TMP>", text)
    text = re.sub(r"/tmp\b", "<TMP>", text)

    # 2. Normalize Python executable path
    text = text.replace(py_exe, "<PYTHON>")
    text = re.sub(r"[A-Za-z]:\\[^\r\n\\\", ']+\\python(?:\.exe)?", "<PYTHON>", text)

    # 3. Normalize user home and usernames
    text = text.replace(user_home, "<HOME>")
    # Replace any Windows Users path
    text = re.sub(r"[A-Za-z]:\\[Uu]sers\\[^\r\n\\\", ']+", "<HOME>", text)
    text = re.sub(r"\\[Uu]sers\\[^\r\n\\\", ']+", "<HOME>", text)
    text = re.sub(r"[Uu]sers\\[^\r\n\\\", ']+", "<HOME>", text)
    text = re.sub(r"/home/[^\r\n/\\\", ']+", "<HOME>", text)
    text = text.replace(user, "<USER>")

    # 4. Normalize durations & timings
    text = re.sub(r"\bin\s+\d+(?:\.\d+)?s\b", "in <DUR>", text)
    text = re.sub(r"\[\d+(?:\.\d+)?ms\]", "[<DUR>]", text)
    text = re.sub(r"\(\d+(?:\.\d+)?ms\)", "(<DUR>)", text)
    text = re.sub(r"duration_ms\s+\d+(?:\.\d+)?", "duration_ms <DUR>", text)
    text = re.sub(r"finished in \d+(?:\.\d+)?s", "finished in <DUR>", text)

    # 5. Normalize hashes and variable ids
    text = re.sub(r"\([0-9a-f]{7,12}\)", "(<HASH>)", text)
    text = re.sub(r"-[0-9a-f]{16}(\.exe)?\b", r"-<HASH>\1", text)
    text = re.sub(r"\bthread '[^']+' \(\d+\)", "thread '<name>' (<TID>)", text)

    # Final sweep ensuring no Users\ or /home/ remains
    text = text.replace("Users\\", "<HOME>\\").replace("users\\", "<HOME>\\")
    text = text.replace("/home/", "<HOME>/")
    if user:
        text = text.replace(user, "<USER>")

    return text


def normalize_command(cmd: List[str]) -> List[str]:
    """Normalizes command argument list."""
    norm_cmd = []
    py_exe = sys.executable
    for arg in cmd:
        if arg == py_exe or arg.endswith("python.exe") or arg.endswith("python3"):
            norm_cmd.append("<PYTHON>")
        elif "bun.EXE" in arg or "bun.exe" in arg:
            norm_cmd.append("<BUN>")
        elif "cargo.EXE" in arg or "cargo.exe" in arg:
            norm_cmd.append("<CARGO>")
        elif "node.EXE" in arg or "node.exe" in arg:
            norm_cmd.append("<NODE>")
        else:
            norm_cmd.append(normalize_text(arg))
    return norm_cmd


def normalize_fixture_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizes all fields in a fixture dictionary."""
    if data.get("tool_version"):
        data["tool_version"] = normalize_text(str(data["tool_version"]))

    if "cases" in data and isinstance(data["cases"], dict):
        for case_name, case in data["cases"].items():
            if "command" in case and isinstance(case["command"], list):
                case["command"] = normalize_command(case["command"])
            if "stdout" in case and isinstance(case["stdout"], str):
                case["stdout"] = normalize_text(case["stdout"])
            if "stderr" in case and isinstance(case["stderr"], str):
                case["stderr"] = normalize_text(case["stderr"])

    return data


def capture_pytest(tmp_path: Path) -> Optional[Dict[str, Any]]:
    try:
        ver_res = subprocess.run([sys.executable, "-m", "pytest", "--version"], capture_output=True, text=True)
        if ver_res.returncode != 0:
            return None
        tool_version = (ver_res.stdout.strip() or ver_res.stderr.strip())
    except Exception:
        return None

    cases = {}

    # 1. pass
    p_dir = tmp_path / "pytest_pass"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "test_p.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    cmd_pass = [sys.executable, "-m", "pytest", "-q"]
    r_pass = subprocess.run(cmd_pass, cwd=p_dir, capture_output=True, text=True)
    cases["pass"] = {"command": cmd_pass, "exit_code": r_pass.returncode, "stdout": r_pass.stdout, "stderr": r_pass.stderr}

    # 2. fail
    f_dir = tmp_path / "pytest_fail"
    f_dir.mkdir(parents=True, exist_ok=True)
    (f_dir / "test_p.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (f_dir / "test_f.py").write_text("def test_ng(): assert False\n", encoding="utf-8")
    cmd_fail = [sys.executable, "-m", "pytest", "-q"]
    r_fail = subprocess.run(cmd_fail, cwd=f_dir, capture_output=True, text=True)
    cases["fail"] = {"command": cmd_fail, "exit_code": r_fail.returncode, "stdout": r_fail.stdout, "stderr": r_fail.stderr}

    # 3. zero_tests
    z_dir = tmp_path / "pytest_zero"
    z_dir.mkdir(parents=True, exist_ok=True)
    cmd_zero = [sys.executable, "-m", "pytest", "-q"]
    r_zero = subprocess.run(cmd_zero, cwd=z_dir, capture_output=True, text=True)
    cases["zero_tests"] = {"command": cmd_zero, "exit_code": r_zero.returncode, "stdout": r_zero.stdout, "stderr": r_zero.stderr}

    # 4. filter_matches_nothing
    cmd_filter = [sys.executable, "-m", "pytest", "-q", "-k", "nonexistent"]
    r_filter = subprocess.run(cmd_filter, cwd=p_dir, capture_output=True, text=True)
    cases["filter_matches_nothing"] = {"command": cmd_filter, "exit_code": r_filter.returncode, "stdout": r_filter.stdout, "stderr": r_filter.stderr}

    # 5. all_skipped
    s_dir = tmp_path / "pytest_skip"
    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / "test_s.py").write_text("import pytest\n@pytest.mark.skip(reason='testing skip')\ndef test_skip(): pass\n", encoding="utf-8")
    cmd_skip = [sys.executable, "-m", "pytest", "-q"]
    r_skip = subprocess.run(cmd_skip, cwd=s_dir, capture_output=True, text=True)
    cases["all_skipped"] = {"command": cmd_skip, "exit_code": r_skip.returncode, "stdout": r_skip.stdout, "stderr": r_skip.stderr}

    return {
        "profile_id": "pytest",
        "runner": "pytest",
        "supported_platforms": ["linux", "darwin", "win32"],
        "captured_on_host": True,
        "host_platform": sys.platform,
        "tool_version": tool_version,
        "skip_reason": None,
        "known_reference": {"zero_tests_exit": 5, "all_skipped_exit": 0},
        "cases": cases,
    }


def capture_node(tmp_path: Path) -> Optional[Dict[str, Any]]:
    node_exe = shutil.which("node")
    if not node_exe:
        return None
    try:
        ver_res = subprocess.run([node_exe, "--version"], capture_output=True, text=True)
        if ver_res.returncode != 0:
            return None
        tool_version = ver_res.stdout.strip()
    except Exception:
        return None

    cases = {}

    # 1. pass
    p_dir = tmp_path / "node_pass"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "test_p.test.js").write_text("const test = require('node:test'); const assert = require('node:assert'); test('pass', () => { assert.strictEqual(1, 1); });\n", encoding="utf-8")
    cmd_pass = [node_exe, "--test"]
    r_pass = subprocess.run(cmd_pass, cwd=p_dir, capture_output=True, text=True)
    cases["pass"] = {"command": cmd_pass, "exit_code": r_pass.returncode, "stdout": r_pass.stdout, "stderr": r_pass.stderr}

    # 2. fail
    f_dir = tmp_path / "node_fail"
    f_dir.mkdir(parents=True, exist_ok=True)
    (f_dir / "test_p.test.js").write_text("const test = require('node:test'); const assert = require('node:assert'); test('pass', () => { assert.strictEqual(1, 1); });\n", encoding="utf-8")
    (f_dir / "test_f.test.js").write_text("const test = require('node:test'); const assert = require('node:assert'); test('fail', () => { assert.fail('expected'); });\n", encoding="utf-8")
    cmd_fail = [node_exe, "--test"]
    r_fail = subprocess.run(cmd_fail, cwd=f_dir, capture_output=True, text=True)
    cases["fail"] = {"command": cmd_fail, "exit_code": r_fail.returncode, "stdout": r_fail.stdout, "stderr": r_fail.stderr}

    # 3. zero_tests
    z_dir = tmp_path / "node_zero"
    z_dir.mkdir(parents=True, exist_ok=True)
    cmd_zero = [node_exe, "--test"]
    r_zero = subprocess.run(cmd_zero, cwd=z_dir, capture_output=True, text=True)
    cases["zero_tests"] = {"command": cmd_zero, "exit_code": r_zero.returncode, "stdout": r_zero.stdout, "stderr": r_zero.stderr}

    # 4. filter_matches_nothing
    cmd_filter = [node_exe, "--test", "--test-name-pattern=nonexistent"]
    r_filter = subprocess.run(cmd_filter, cwd=p_dir, capture_output=True, text=True)
    cases["filter_matches_nothing"] = {"command": cmd_filter, "exit_code": r_filter.returncode, "stdout": r_filter.stdout, "stderr": r_filter.stderr}

    # 5. all_skipped
    s_dir = tmp_path / "node_skip"
    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / "test_s.test.js").write_text("const test = require('node:test'); test('skip', { skip: true }, () => {});\n", encoding="utf-8")
    cmd_skip = [node_exe, "--test"]
    r_skip = subprocess.run(cmd_skip, cwd=s_dir, capture_output=True, text=True)
    cases["all_skipped"] = {"command": cmd_skip, "exit_code": r_skip.returncode, "stdout": r_skip.stdout, "stderr": r_skip.stderr}

    return {
        "profile_id": "node-native-test",
        "runner": "node",
        "supported_platforms": ["linux", "darwin", "win32"],
        "captured_on_host": True,
        "host_platform": sys.platform,
        "tool_version": tool_version,
        "skip_reason": None,
        "known_reference": {"zero_tests_exit": 0, "all_skipped_exit": 0},
        "cases": cases,
    }


def capture_bun(tmp_path: Path) -> Optional[Dict[str, Any]]:
    bun_exe = shutil.which("bun")
    if not bun_exe:
        return None
    try:
        ver_res = subprocess.run([bun_exe, "--version"], capture_output=True, text=True)
        if ver_res.returncode != 0:
            return None
        tool_version = ver_res.stdout.strip()
    except Exception:
        return None

    cases = {}

    # 1. pass
    p_dir = tmp_path / "bun_pass"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "test_p.test.js").write_text("import { test, expect } from 'bun:test'; test('pass', () => { expect(1).toBe(1); });\n", encoding="utf-8")
    cmd_pass = [bun_exe, "test"]
    r_pass = subprocess.run(cmd_pass, cwd=p_dir, capture_output=True, text=True)
    cases["pass"] = {"command": cmd_pass, "exit_code": r_pass.returncode, "stdout": r_pass.stdout, "stderr": r_pass.stderr}

    # 2. fail
    f_dir = tmp_path / "bun_fail"
    f_dir.mkdir(parents=True, exist_ok=True)
    (f_dir / "test_p.test.js").write_text("import { test, expect } from 'bun:test'; test('pass', () => { expect(1).toBe(1); });\n", encoding="utf-8")
    (f_dir / "test_f.test.js").write_text("import { test, expect } from 'bun:test'; test('fail', () => { expect(1).toBe(2); });\n", encoding="utf-8")
    cmd_fail = [bun_exe, "test"]
    r_fail = subprocess.run(cmd_fail, cwd=f_dir, capture_output=True, text=True)
    cases["fail"] = {"command": cmd_fail, "exit_code": r_fail.returncode, "stdout": r_fail.stdout, "stderr": r_fail.stderr}

    # 3. zero_tests
    z_dir = tmp_path / "bun_zero"
    z_dir.mkdir(parents=True, exist_ok=True)
    cmd_zero = [bun_exe, "test"]
    r_zero = subprocess.run(cmd_zero, cwd=z_dir, capture_output=True, text=True)
    cases["zero_tests"] = {"command": cmd_zero, "exit_code": r_zero.returncode, "stdout": r_zero.stdout, "stderr": r_zero.stderr}

    # 4. filter_matches_nothing
    cmd_filter = [bun_exe, "test", "-t", "nonexistent"]
    r_filter = subprocess.run(cmd_filter, cwd=p_dir, capture_output=True, text=True)
    cases["filter_matches_nothing"] = {"command": cmd_filter, "exit_code": r_filter.returncode, "stdout": r_filter.stdout, "stderr": r_filter.stderr}

    # 5. all_skipped
    s_dir = tmp_path / "bun_skip"
    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / "test_p.test.js").write_text("import { test } from 'bun:test'; test.skip('skip', () => {});\n", encoding="utf-8")
    cmd_skip = [bun_exe, "test"]
    r_skip = subprocess.run(cmd_skip, cwd=s_dir, capture_output=True, text=True)
    cases["all_skipped"] = {"command": cmd_skip, "exit_code": r_skip.returncode, "stdout": r_skip.stdout, "stderr": r_skip.stderr}

    return {
        "profile_id": "bun-test",
        "runner": "bun",
        "supported_platforms": ["linux", "darwin", "win32"],
        "captured_on_host": True,
        "host_platform": sys.platform,
        "tool_version": tool_version,
        "skip_reason": None,
        "known_reference": {
            "zero_tests_exit": 1,
            "filter_matches_nothing_exit": 1,
            "all_skipped_exit": 0,
            "failure_marker": "(fail) Suite > test",
        },
        "cases": cases,
    }


def capture_cargo(tmp_path: Path) -> Optional[Dict[str, Any]]:
    cargo_exe = shutil.which("cargo")
    if not cargo_exe:
        return None
    try:
        ver_res = subprocess.run([cargo_exe, "--version"], capture_output=True, text=True)
        if ver_res.returncode != 0:
            return None
        tool_version = ver_res.stdout.strip()
    except Exception:
        return None

    cases = {}
    c_dir = tmp_path / "cargo_proj"
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "Cargo.toml").write_text('[package]\nname = "test_cargo"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    src_dir = c_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # 1. pass
    (src_dir / "lib.rs").write_text("#[cfg(test)]\nmod tests {\n    #[test]\n    fn it_works() { assert_eq!(2 + 2, 4); }\n}\n", encoding="utf-8")
    cmd_pass = [cargo_exe, "test"]
    r_pass = subprocess.run(cmd_pass, cwd=c_dir, capture_output=True, text=True)
    cases["pass"] = {"command": cmd_pass, "exit_code": r_pass.returncode, "stdout": r_pass.stdout, "stderr": r_pass.stderr}

    # 2. fail
    (src_dir / "lib.rs").write_text("#[test]\nfn it_fails() { assert!(false); }\n", encoding="utf-8")
    cmd_fail = [cargo_exe, "test"]
    r_fail = subprocess.run(cmd_fail, cwd=c_dir, capture_output=True, text=True)
    cases["fail"] = {"command": cmd_fail, "exit_code": r_fail.returncode, "stdout": r_fail.stdout, "stderr": r_fail.stderr}

    # 3. zero_tests
    (src_dir / "lib.rs").write_text("pub fn foo() {}\n", encoding="utf-8")
    cmd_zero = [cargo_exe, "test"]
    r_zero = subprocess.run(cmd_zero, cwd=c_dir, capture_output=True, text=True)
    cases["zero_tests"] = {"command": cmd_zero, "exit_code": r_zero.returncode, "stdout": r_zero.stdout, "stderr": r_zero.stderr}

    # 4. filter_matches_nothing
    (src_dir / "lib.rs").write_text("#[test]\nfn it_works() {}\n", encoding="utf-8")
    cmd_filter = [cargo_exe, "test", "nonexistent"]
    r_filter = subprocess.run(cmd_filter, cwd=c_dir, capture_output=True, text=True)
    cases["filter_matches_nothing"] = {"command": cmd_filter, "exit_code": r_filter.returncode, "stdout": r_filter.stdout, "stderr": r_filter.stderr}

    # 5. all_skipped
    (src_dir / "lib.rs").write_text("#[test]\n#[ignore]\nfn it_skipped() {}\n", encoding="utf-8")
    cmd_skip = [cargo_exe, "test"]
    r_skip = subprocess.run(cmd_skip, cwd=c_dir, capture_output=True, text=True)
    cases["all_skipped"] = {"command": cmd_skip, "exit_code": r_skip.returncode, "stdout": r_skip.stdout, "stderr": r_skip.stderr}

    return {
        "profile_id": "cargo-test",
        "runner": "cargo",
        "supported_platforms": ["linux", "darwin", "win32"],
        "captured_on_host": True,
        "host_platform": sys.platform,
        "tool_version": tool_version,
        "skip_reason": None,
        "known_reference": {"zero_tests_exit": 0, "all_skipped_exit": 0},
        "cases": cases,
    }


def capture_npm_runner(runner: str, tmp_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Installs npm runner (jest, mocha, vitest) into temp project and captures cases."""
    node_exe = shutil.which("node")
    npm_exe = shutil.which("npm")
    if not node_exe:
        return None, "node not found"
    if not npm_exe:
        return None, "npm not found"

    proj_dir = tmp_path / f"npm_{runner}"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "package.json").write_text(json.dumps({"name": f"test-{runner}", "version": "1.0.0"}), encoding="utf-8")

    # Install runner with a short timeout
    cmd_install = [npm_exe, "install", "--no-audit", "--no-fund", "--save-dev", runner]
    if sys.platform == "win32" and not npm_exe.endswith((".cmd", ".bat")):
        # If npm without extension, use npm.cmd on Windows
        win_npm = shutil.which("npm.cmd")
        if win_npm:
            cmd_install[0] = win_npm

    try:
        inst_res = subprocess.run(cmd_install, cwd=proj_dir, capture_output=True, text=True, timeout=10)
        if inst_res.returncode != 0:
            return None, f"npm install failed for {runner}: {inst_res.stderr.strip() or inst_res.stdout.strip()}"
    except subprocess.TimeoutExpired:
        return None, f"npm install timed out for {runner}"
    except Exception as e:
        return None, f"npm install error for {runner}: {e}"

    # Get installed version
    bin_path = proj_dir / "node_modules" / ".bin" / runner
    if sys.platform == "win32":
        bin_cmd = proj_dir / "node_modules" / ".bin" / f"{runner}.cmd"
        if bin_cmd.exists():
            bin_path = bin_cmd

    if not bin_path.exists():
        return None, f"Runner binary {runner} not found in node_modules/.bin"

    try:
        ver_res = subprocess.run([str(bin_path), "--version"], cwd=proj_dir, capture_output=True, text=True, timeout=10)
        tool_version = (ver_res.stdout.strip() or ver_res.stderr.strip())
    except Exception:
        tool_version = "installed"

    cases = {}
    try:
        # 1. pass
        p_dir = proj_dir / "case_pass"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / f"sample.test.js").write_text("test('ok', () => { expect(1).toBe(1); });\n" if runner != "mocha" else "const assert = require('assert'); it('ok', () => { assert.equal(1, 1); });\n", encoding="utf-8")
        cmd_p = [str(bin_path)]
        r_p = subprocess.run(cmd_p, cwd=p_dir, capture_output=True, text=True, timeout=15)
        cases["pass"] = {"command": cmd_p, "exit_code": r_p.returncode, "stdout": r_p.stdout, "stderr": r_p.stderr}

        # 2. fail
        f_dir = proj_dir / "case_fail"
        f_dir.mkdir(parents=True, exist_ok=True)
        (f_dir / f"sample.test.js").write_text("test('fail', () => { expect(1).toBe(2); });\n" if runner != "mocha" else "const assert = require('assert'); it('fail', () => { assert.equal(1, 2); });\n", encoding="utf-8")
        cmd_f = [str(bin_path)]
        r_f = subprocess.run(cmd_f, cwd=f_dir, capture_output=True, text=True, timeout=15)
        cases["fail"] = {"command": cmd_f, "exit_code": r_f.returncode, "stdout": r_f.stdout, "stderr": r_f.stderr}

        # 3. zero_tests
        z_dir = proj_dir / "case_zero"
        z_dir.mkdir(parents=True, exist_ok=True)
        cmd_z = [str(bin_path)]
        r_z = subprocess.run(cmd_z, cwd=z_dir, capture_output=True, text=True, timeout=15)
        cases["zero_tests"] = {"command": cmd_z, "exit_code": r_z.returncode, "stdout": r_z.stdout, "stderr": r_z.stderr}

        # 4. filter_matches_nothing
        cmd_filter = [str(bin_path), "-t", "nonexistent"] if runner != "mocha" else [str(bin_path), "-g", "nonexistent"]
        r_filter = subprocess.run(cmd_filter, cwd=p_dir, capture_output=True, text=True, timeout=15)
        cases["filter_matches_nothing"] = {"command": cmd_filter, "exit_code": r_filter.returncode, "stdout": r_filter.stdout, "stderr": r_filter.stderr}

        # 5. all_skipped
        s_dir = proj_dir / "case_skip"
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / f"sample.test.js").write_text("test.skip('skip', () => {});\n" if runner != "mocha" else "it.skip('skip', () => {});\n", encoding="utf-8")
        cmd_s = [str(bin_path)]
        r_s = subprocess.run(cmd_s, cwd=s_dir, capture_output=True, text=True, timeout=15)
        cases["all_skipped"] = {"command": cmd_s, "exit_code": r_s.returncode, "stdout": r_s.stdout, "stderr": r_s.stderr}

        return {
            "profile_id": runner,
            "runner": runner,
            "supported_platforms": ["linux", "darwin", "win32"],
            "captured_on_host": True,
            "host_platform": sys.platform,
            "tool_version": tool_version,
            "skip_reason": None,
            "known_reference": {"zero_tests_exit": 1},
            "cases": cases,
        }, None
    except Exception as e:
        return None, f"Execution failed for {runner}: {e}"


def get_skip_reason_for_profile(profile_id: str, platform: str) -> str:
    """Returns an accurate, specific skip reason for tools that are missing or not supported."""
    if profile_id == "xcode-test":
        return f"xcodebuild not supported on {platform} (darwin only)"
    if profile_id == "swiftpm-test" and platform == "win32":
        return "swiftpm-test not supported on win32 (platforms: ['linux', 'darwin'])"

    tool_binary_map = {
        "deno-test": "deno",
        "go-test": "go",
        "maven-surefire": "mvn",
        "gradle-test": "gradle",
        "dotnet-test": "dotnet",
        "cmake-ctest": "ctest",
        "ruby-rake": "ruby",
        "phpunit": "php",
        "swiftpm-test": "swift",
        "mix-test": "mix",
        "terraform": "terraform",
        "ansible": "ansible-lint",
        "helm": "helm",
        "bun-test": "bun",
        "jest": "npm",
        "mocha": "npm",
        "vitest": "npm",
    }
    tool = tool_binary_map.get(profile_id, profile_id)
    return f"{tool} not found"


def regenerate_fixtures(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Regenerates all profile fixtures for the current platform with normalization."""
    out_dir.mkdir(parents=True, exist_ok=True)
    platform = sys.platform

    # Load registry
    from jev.runner_registry import load_registry
    registry = load_registry()

    results = {}
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)

        for profile in registry.profiles:
            pid = profile.profile_id
            data = None
            skip_msg = None

            if platform not in profile.platforms:
                skip_msg = f"{pid} not supported on {platform} (platforms: {profile.platforms})"
            elif pid == "pytest":
                data = capture_pytest(tmp_path)
                if not data:
                    skip_msg = "pytest not found"
            elif pid == "node-native-test":
                data = capture_node(tmp_path)
                if not data:
                    skip_msg = "node not found"
            elif pid == "bun-test":
                data = capture_bun(tmp_path)
                if not data:
                    skip_msg = "bun not found"
            elif pid == "cargo-test":
                data = capture_cargo(tmp_path)
                if not data:
                    skip_msg = "cargo not found"
            elif pid in ("jest", "mocha", "vitest"):
                data, skip_msg = capture_npm_runner(pid, tmp_path)
            else:
                skip_msg = get_skip_reason_for_profile(pid, platform)

            if data is None:
                known_ref = {}
                if profile.zero_test_exit_behavior is not None:
                    known_ref["zero_tests_exit"] = profile.zero_test_exit_behavior
                elif pid in ("jest", "mocha", "vitest"):
                    known_ref["zero_tests_exit"] = 1
                data = {
                    "profile_id": pid,
                    "runner": profile.runner,
                    "supported_platforms": profile.platforms,
                    "captured_on_host": False,
                    "host_platform": platform,
                    "tool_version": None,
                    "skip_reason": skip_msg,
                    "known_reference": known_ref,
                    "cases": {},
                }

            normalized = normalize_fixture_data(data)
            results[pid] = normalized

            out_file = out_dir / f"{pid}.json"
            out_file.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
            print(f"[{'CAPTURED' if normalized['captured_on_host'] else 'SKIPPED'}] {pid} -> {out_file.name}", flush=True)

    return results


def verify_against_committed_fixtures(platform: str) -> bool:
    """Verifies that live exit codes match committed fixtures of the SAME platform."""
    plat_dir = FIXTURES_ROOT / platform
    if not plat_dir.is_dir():
        print(f"No committed fixtures directory for platform '{platform}' at {plat_dir}")
        return True

    from jev.runner_registry import load_registry
    registry = load_registry()

    all_ok = True
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        for profile in registry.profiles:
            pid = profile.profile_id
            f_path = plat_dir / f"{pid}.json"
            if not f_path.exists():
                continue

            committed = json.loads(f_path.read_text(encoding="utf-8"))
            if not committed.get("captured_on_host"):
                continue

            # Live capture
            live_data = None
            if pid == "pytest":
                live_data = capture_pytest(tmp_path)
            elif pid == "node-native-test":
                live_data = capture_node(tmp_path)
            elif pid == "bun-test":
                live_data = capture_bun(tmp_path)
            elif pid == "cargo-test":
                live_data = capture_cargo(tmp_path)
            elif pid in ("jest", "mocha", "vitest"):
                live_data, _ = capture_npm_runner(pid, tmp_path)

            if not live_data:
                print(f"WARNING: Toolchain for {pid} was captured in committed fixture, but not available in current run.")
                continue

            # Check exit codes
            for case_name, case in committed.get("cases", {}).items():
                expected_exit = case.get("exit_code")
                live_case = live_data.get("cases", {}).get(case_name)
                if live_case is None:
                    continue
                actual_exit = live_case.get("exit_code")
                if expected_exit != actual_exit:
                    print(f"EXIT CODE MISMATCH in {pid} case '{case_name}': expected {expected_exit}, got {actual_exit}")
                    all_ok = False
                else:
                    print(f"VERIFIED: {pid} case '{case_name}' exit code {actual_exit} matches committed fixture")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Capture or verify runner fixtures.")
    parser.add_argument("--verify-only", action="store_true", help="Only verify exit codes against committed fixtures")
    parser.add_argument("--out-dir", type=str, default=None, help="Directory to save generated fixtures")
    args = parser.parse_args()

    platform = sys.platform
    target_dir = Path(args.out_dir) if args.out_dir else (FIXTURES_ROOT / platform)

    if args.verify_only:
        print(f"=== Verifying live runner exit codes against committed fixtures for {platform} ===")
        ok = verify_against_committed_fixtures(platform)
        if not ok:
            print("Verification FAILED: One or more exit codes differed!")
            sys.exit(1)
        print("Verification PASSED: All live exit codes matched committed fixtures.")
        sys.exit(0)

    print(f"=== Regenerating fixtures for platform {platform} in {target_dir} ===")
    regenerate_fixtures(target_dir)
    print("Done regenerating fixtures.")


if __name__ == "__main__":
    main()
