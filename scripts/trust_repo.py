#!/usr/bin/env python3
"""scripts/trust_repo.py

Interactive approval and trust recording CLI for repository test configuration.
Trust records live outside the repository under <JEV_HOME>/trust/<repo_id>.json.
Never executes the command.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Add src to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from jev.repo_config import (
    RepoConfig,
    compute_repo_id,
    get_trust_file_path,
    load_repo_config,
    record_trust,
)


def inspect_executable(cmd: list[str]) -> tuple[str | None, str | None]:
    """Finds executable path and version if available on PATH."""
    if not cmd:
        return None, None
    exe_name = cmd[0]
    exe_path = shutil.which(exe_name)
    if not exe_path:
        return None, None

    exe_version = None
    try:
        res = subprocess.run([exe_path, "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            first_line = res.stdout.strip().splitlines()
            if first_line:
                exe_version = first_line[0][:80]
    except Exception:
        pass

    return exe_path, exe_version


def main() -> None:
    parser = argparse.ArgumentParser(description="Record trust for repository test configuration.")
    parser.add_argument("repo_dir", type=str, help="Path to repository root containing .jev/config.json")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt for non-interactive use")
    args = parser.parse_args()

    repo_path = Path(args.repo_dir).resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        print(f"Error: directory does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    cfg = load_repo_config(repo_path)
    if cfg is None:
        print(f"Error: no valid .jev/config.json found in {repo_path}", file=sys.stderr)
        sys.exit(1)

    repo_id = compute_repo_id(repo_path)
    profile = cfg.runner_profile or "(none)"
    cwd = cfg.cwd or "."
    test_cmd = cfg.test_command or []

    exe_path, exe_ver = inspect_executable(test_cmd)

    print("=== Jev Repository Trust Review ===")
    print(f"Repository: {repo_path}")
    print(f"Repo ID:    {repo_id}")
    print(f"Profile:    {profile}")
    print(f"CWD:        {cwd}")
    print(f"Executable: {exe_path or '(not found on PATH)'}" + (f" ({exe_ver})" if exe_ver else ""))
    print("Declared Test Command:")
    if test_cmd:
        for arg in test_cmd:
            print(f"  {arg}")
    else:
        print("  (none declared)")

    if cfg.build_command:
        print("Declared Build Command:")
        for arg in cfg.build_command:
            print(f"  {arg}")

    if not args.yes:
        try:
            choice = input("\nTrust this repository command? [y/N]: ").strip().lower()
        except EOFError:
            choice = "n"
        if choice not in ("y", "yes"):
            print("Aborted. Trust was not recorded.")
            sys.exit(1)

    record = record_trust(
        config=cfg,
        repo_dir=repo_path,
        approved_by=os.environ.get("USER") or os.environ.get("USERNAME") or "owner",
        executable_path=exe_path,
        executable_version=exe_ver,
    )

    trust_path = get_trust_file_path(repo_id)
    print(f"\nTrust recorded for repository {repo_id} at:")
    print(f"  {trust_path}")


if __name__ == "__main__":
    main()
