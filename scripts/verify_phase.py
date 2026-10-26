#!/usr/bin/env python3
"""scripts/verify_phase.py

Verification script for phase transitions as specified in session-1-v2.md (Phase 0B):
Prints branch/commit, diff stat vs a base ref, added/modified/deleted tests,
collected counts for new test files, greps for key patterns, and the last lines of the offline suite.
"""

import os
import re
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, check=True):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command {' '.join(cmd)} failed (code {res.returncode}):\n{res.stderr}")
    return res


def main():
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    repo_root = Path(__file__).resolve().parent.parent

    print(f"=== Verification Report for base: {base_ref} ===")

    # 1. Branch & Commit
    branch_res = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    commit_res = run_cmd(["git", "rev-parse", "HEAD"])
    branch = branch_res.stdout.strip()
    commit = commit_res.stdout.strip()
    print(f"Branch: {branch}")
    print(f"Commit: {commit}\n")

    # 2. Diff stat vs base_ref
    print(f"--- Diff stat vs {base_ref} ---")
    diff_stat_res = run_cmd(["git", "diff", "--stat", base_ref])
    print(diff_stat_res.stdout.strip() or "(no diff)")
    print()

    # 3. Added/modified/deleted tests
    print("--- Test Changes ---")
    diff_tests_res = run_cmd(["git", "diff", base_ref, "--", "tests/"])
    added_tests = []
    for line in diff_tests_res.stdout.splitlines():
        if line.startswith("+def test_") or line.startswith("+    def test_"):
            m = re.search(r"def\s+(test_[a-zA-Z0-9_]+)", line)
            if m:
                added_tests.append(m.group(1))
    if added_tests:
        print("Added tests:")
        for t in added_tests:
            print(f"  + {t}")
    else:
        print("(no new test functions added)")
    print()

    # 4. Collected counts for new/modified test files
    print("--- Collected Test Counts ---")
    changed_files_res = run_cmd(["git", "diff", "--name-only", base_ref, "--", "tests/"])
    changed_test_files = [f.strip() for f in changed_files_res.stdout.splitlines() if f.strip().endswith(".py")]
    for tf in changed_test_files:
        if (repo_root / tf).exists():
            c_res = run_cmd(["python", "-m", "pytest", tf, "--collect-only", "-q"])
            summary_lines = [l for l in c_res.stdout.splitlines() if "collected" in l or "tests" in l]
            last_line = summary_lines[-1] if summary_lines else c_res.stdout.strip()
            print(f"  {tf}: {last_line}")
    print()

    # 5. Greps for key patterns
    print("--- Grep Checks ---")
    # git add -A
    grep_git_add = run_cmd(["git", "grep", "-n", "git add -A", "--", "src/jev/"], check=False)
    print("Pattern 'git add -A' in src/jev/:")
    print("  " + (grep_git_add.stdout.strip() or "None found (clean)"))

    # run_baseline_tests(
    grep_baseline = run_cmd(["git", "grep", "-n", "run_baseline_tests(", "--", "src/jev/engine.py"], check=False)
    print("Pattern 'run_baseline_tests(' in src/jev/engine.py:")
    print("  " + (grep_baseline.stdout.strip() or "None found in engine.py (clean)"))

    # FULL_SUITE_PASSED
    grep_fsp = run_cmd(["git", "grep", "-n", "FULL_SUITE_PASSED", "--", "src/jev/workspace.py", "src/jev/engine.py"], check=False)
    print("Occurrences of 'FULL_SUITE_PASSED' in workspace.py and engine.py:")
    for line in grep_fsp.stdout.splitlines():
        print(f"  {line}")

    # worktree_dir == self.repo_dir (shows guard unchanged)
    grep_guard = run_cmd(["git", "grep", "-n", "worktree_dir == self.repo_dir", "--", "src/jev/workspace.py"], check=False)
    print("Occurrences of 'worktree_dir == self.repo_dir' in workspace.py (guard check):")
    for line in grep_guard.stdout.splitlines():
        print(f"  {line}")
    print()

    # 6. Last lines of offline test suite
    print("--- Pytest Offline Suite Summary ---")
    env = os.environ.copy()
    env.pop("ANTIGRAVITY_SOURCE_METADATA", None)
    pytest_res = subprocess.run(
        ["python", "-m", "pytest", "-q", "-m", "not live"],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    lines = [l for l in pytest_res.stdout.splitlines() if l.strip()]
    if lines:
        for l in lines[-5:]:
            print(l)
    else:
        print(pytest_res.stderr.strip() or "(no pytest output)")


if __name__ == "__main__":
    main()
