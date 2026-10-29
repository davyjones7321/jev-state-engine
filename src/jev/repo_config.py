from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field

PROTECTED_PREFIXES = (
    ".git/",
    ".jev/",
    "node_modules/",
)

PROTECTED_FILES = (
    "runner_profiles.json",
    "runner_registry.py",
    "repo_config.py",
    "process_runner.py",
)

BUILD_TEST_CONFIG_FILES = (
    "pytest.ini",
    ".pytest.ini",
    "setup.cfg",
    "tox.ini",
    "jest.config.js",
    "jest.config.ts",
    "jest.config.json",
    "jest.config.mjs",
    "jest.config.cjs",
    "vitest.config.ts",
    "vitest.config.js",
    "vitest.config.mjs",
    ".mocharc.json",
    ".mocharc.js",
    ".mocharc.yaml",
    ".mocharc.yml",
    "tsconfig.json",
    "Makefile",
    "GNUmakefile",
    "makefile",
    "Taskfile.yml",
    "Taskfile.yaml",
    "Taskfile",
    "Justfile",
    "justfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Cargo.toml",
    "CMakeLists.txt",
    "Rakefile",
    "phpunit.xml",
    "phpunit.xml.dist",
    "mix.exs",
    ".vscode/tasks.json",
)

LOCKFILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Gemfile.lock",
    "mix.lock",
    "go.sum",
)


def normalize_command_argv(cmd: Union[str, Sequence[str]]) -> List[str]:
    """Normalizes a command string or sequence into a list of arguments."""
    if isinstance(cmd, str):
        return shlex.split(cmd, posix=(sys.platform != "win32"))
    return list(cmd)


def is_protected_path(path: Union[str, Path]) -> bool:
    """Returns True if the path is protected and cannot be staged or touched by workers."""
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    norm = posixpath.normpath(raw)
    if norm.startswith("./"):
        norm = norm[2:]
    norm_lower = norm.lower()

    # Prefix checks
    for prefix in (".git", ".jev", "node_modules"):
        if norm_lower == prefix or norm_lower.startswith(f"{prefix}/"):
            return True

    # Protected file name checks (exact or trailing component)
    parts = norm_lower.split("/")
    filename = parts[-1]
    for pf in PROTECTED_FILES:
        pf_lower = pf.lower()
        if filename == pf_lower or norm_lower == pf_lower or norm_lower.endswith(f"/{pf_lower}"):
            return True

    return False


def is_build_or_test_config(path: Union[str, Path], diff_text: Optional[str] = None) -> bool:
    """Returns True if path is a repository build/test configuration file."""
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    norm = posixpath.normpath(raw)
    if norm.startswith("./"):
        norm = norm[2:]
    parts = norm.split("/")
    filename = parts[-1]

    if filename in BUILD_TEST_CONFIG_FILES or norm in BUILD_TEST_CONFIG_FILES:
        return True

    # Prefixes for dynamic test config names
    for pattern in ("jest.config.", "vitest.config.", ".mocharc.", "tsconfig."):
        if filename.startswith(pattern):
            return True

    # package.json scripts or config
    if filename == "package.json":
        if diff_text:
            if '"scripts"' in diff_text or "'scripts'" in diff_text or '"test"' in diff_text:
                return True
        return True

    # pyproject.toml test/build config
    if filename == "pyproject.toml":
        if diff_text:
            if "tool.pytest" in diff_text or "scripts" in diff_text or "tool.poetry.scripts" in diff_text:
                return True
        return True

    return False


def is_lockfile(path: Union[str, Path]) -> bool:
    """Returns True if path is a dependency lockfile."""
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    norm = posixpath.normpath(raw)
    if norm.startswith("./"):
        norm = norm[2:]
    parts = norm.split("/")
    filename = parts[-1]
    return filename in LOCKFILES


def compute_repo_id(repo_dir: Union[str, Path]) -> str:
    """Derives deterministic repository identity.

    Uses initial git commit SHA if in a git repository; falls back to SHA256 of canonical path.
    """
    path = Path(repo_dir).resolve()
    try:
        res = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.splitlines()[0].strip()
    except Exception:
        pass

    normalized = str(path).replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_approval_hash(
    repo_id: str,
    profile_id: Optional[str],
    registry_version: Optional[str],
    exact_argv: Sequence[str],
    cwd: str,
    executable_path: Optional[str],
    executable_version: Optional[str],
    platform: str,
) -> str:
    """Computes a deterministic SHA-256 hash across bound trust parameters."""
    payload = {
        "repo_id": str(repo_id),
        "profile_id": str(profile_id) if profile_id else "",
        "registry_version": str(registry_version) if registry_version else "",
        "exact_argv": list(exact_argv),
        "cwd": str(cwd or "."),
        "executable_path": str(executable_path or ""),
        "executable_version": str(executable_version or ""),
        "platform": str(platform),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TrustBinding(BaseModel):
    repo_id: str
    profile_id: Optional[str] = None
    registry_version: Optional[str] = None
    exact_argv: List[str]
    cwd: str = "."
    executable_path: Optional[str] = None
    executable_version: Optional[str] = None
    platform: str
    approval_hash: str
    recorded_at: Optional[str] = None
    recorded_by: Optional[str] = "owner"


class RepoConfig(BaseModel):
    runner_profile_id: Optional[str] = None
    build_command: Optional[Union[str, List[str]]] = None
    test_command: Optional[Union[str, List[str]]] = None
    cwd: Optional[str] = None
    target_rule: Optional[str] = None
    generated_paths: List[str] = Field(default_factory=list)
    expected_tests_policy: Optional[str] = None
    trust: Optional[TrustBinding] = None


def record_trust(
    config: RepoConfig,
    repo_dir: Union[str, Path],
    executable_path: Optional[str] = None,
    executable_version: Optional[str] = None,
    registry_version: Optional[str] = None,
    recorded_by: str = "owner",
) -> RepoConfig:
    """Records one-time owner trust binding for the repository configuration."""
    import datetime

    repo_id = compute_repo_id(repo_dir)
    exact_argv = normalize_command_argv(config.test_command) if config.test_command else []
    cwd = config.cwd or "."
    platform = sys.platform

    if not registry_version:
        try:
            from jev.runner_registry import load_registry
            reg = load_registry()
            registry_version = reg.registry_version
        except Exception:
            registry_version = "1.0.0"

    approval_hash = compute_approval_hash(
        repo_id=repo_id,
        profile_id=config.runner_profile_id,
        registry_version=registry_version,
        exact_argv=exact_argv,
        cwd=cwd,
        executable_path=executable_path,
        executable_version=executable_version,
        platform=platform,
    )

    config.trust = TrustBinding(
        repo_id=repo_id,
        profile_id=config.runner_profile_id,
        registry_version=registry_version,
        exact_argv=exact_argv,
        cwd=cwd,
        executable_path=executable_path,
        executable_version=executable_version,
        platform=platform,
        approval_hash=approval_hash,
        recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        recorded_by=recorded_by,
    )
    return config


def validate_trust(
    config: RepoConfig,
    repo_dir: Union[str, Path],
    executable_path: Optional[str] = None,
    executable_version: Optional[str] = None,
    platform: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Validates the trust binding against repository identity, argv, platform, and executable."""
    if not config.trust:
        return False, "missing trust binding"

    tb = config.trust
    expected_repo_id = compute_repo_id(repo_dir)
    if tb.repo_id != expected_repo_id:
        return False, f"repo_id mismatch: {tb.repo_id} != {expected_repo_id}"

    curr_platform = platform or sys.platform
    if tb.platform != curr_platform:
        return False, f"platform mismatch: {tb.platform} != {curr_platform}"

    if config.runner_profile_id and tb.profile_id != config.runner_profile_id:
        return False, f"profile mismatch: {tb.profile_id} != {config.runner_profile_id}"

    config_argv = normalize_command_argv(config.test_command) if config.test_command else []
    if tb.exact_argv != config_argv:
        return False, "command changed: approval invalidated"

    config_cwd = config.cwd or "."
    if tb.cwd != config_cwd:
        return False, f"cwd mismatch: {tb.cwd} != {config_cwd}"

    if executable_path and tb.executable_path and tb.executable_path != executable_path:
        return False, f"executable path mismatch: {tb.executable_path} != {executable_path}"

    if executable_version and tb.executable_version and tb.executable_version != executable_version:
        return False, f"executable version mismatch: {tb.executable_version} != {executable_version}"

    expected_hash = compute_approval_hash(
        repo_id=tb.repo_id,
        profile_id=tb.profile_id,
        registry_version=tb.registry_version,
        exact_argv=tb.exact_argv,
        cwd=tb.cwd,
        executable_path=tb.executable_path,
        executable_version=tb.executable_version,
        platform=tb.platform,
    )
    if tb.approval_hash != expected_hash:
        return False, "approval hash mismatch: tampered trust binding"

    return True, None


def load_repo_config(repo_dir: Union[str, Path]) -> Optional[RepoConfig]:
    """Loads and validates .jev/config.json if present in repo_dir or parent worktree repo."""
    p = Path(repo_dir).resolve()
    cfg_file = p / ".jev" / "config.json"
    if not cfg_file.exists() or not cfg_file.is_file():
        curr = p
        while curr != curr.parent:
            candidate = curr / ".jev" / "config.json"
            if candidate.exists() and candidate.is_file():
                cfg_file = candidate
                break
            git_file = curr / ".git"
            if git_file.exists() and git_file.is_file():
                try:
                    gitdir_content = git_file.read_text(encoding="utf-8").strip()
                    if gitdir_content.startswith("gitdir:"):
                        gitdir_path = Path(gitdir_content[7:].strip())
                        if not gitdir_path.is_absolute():
                            gitdir_path = (curr / gitdir_path).resolve()
                        main_repo = gitdir_path.parent.parent.parent
                        candidate = main_repo / ".jev" / "config.json"
                        if candidate.exists() and candidate.is_file():
                            cfg_file = candidate
                            break
                except Exception:
                    pass
            curr = curr.parent
        else:
            return None

    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        return RepoConfig.model_validate(data)
    except Exception:
        return None


def save_repo_config(config: RepoConfig, repo_dir: Union[str, Path]) -> Path:
    """Serializes RepoConfig to .jev/config.json in repo_dir."""
    p = Path(repo_dir)
    jev_dir = p / ".jev"
    jev_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = jev_dir / "config.json"
    raw = config.model_dump(exclude_none=True)
    cfg_file.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return cfg_file
