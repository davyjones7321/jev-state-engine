from __future__ import annotations

import datetime
import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from jev.mech_evidence import MechanicalEvidence

if "lockfile_changed" not in MechanicalEvidence.model_fields:
    MechanicalEvidence.model_fields["lockfile_changed"] = FieldInfo(
        annotation=Optional[bool], default=None
    )
    MechanicalEvidence.model_rebuild(force=True)

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


def get_jev_home() -> Path:
    """Returns JEV_HOME path (defaults to ~/.jev)."""
    jev_home = os.environ.get("JEV_HOME")
    if jev_home:
        return Path(jev_home).expanduser().resolve()
    return (Path.home() / ".jev").resolve()


def get_trust_dir() -> Path:
    """Returns the directory where repository trust records are stored."""
    td = get_jev_home() / "trust"
    td.mkdir(parents=True, exist_ok=True)
    return td


def get_trust_file_path(repo_id: str) -> Path:
    """Returns the external path for a repository trust record: <JEV_HOME>/trust/<repo_id>.json."""
    return get_trust_dir() / f"{repo_id}.json"


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


def is_build_or_test_config(
    path: Union[str, Path],
    base_content: Optional[str] = None,
    candidate_content: Optional[str] = None,
) -> bool:
    """Returns True if path is a repository build/test configuration file.

    For package.json and pyproject.toml, comparison is STRUCTURAL:
    - package.json: only changes in 'scripts', 'workspaces', and runner config keys
      ('jest', 'vitest', 'mocha', 'ava') count as verifier changes. Dependency, version,
      description or other edits are not.
    - pyproject.toml: only [tool.pytest*], [project.scripts], and [tool.poetry.scripts]
      count as verifier changes.
    - A file that cannot be parsed counts as changed.
    """
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    norm = posixpath.normpath(raw)
    if norm.startswith("./"):
        norm = norm[2:]
    parts = norm.split("/")
    filename = parts[-1]

    # 1. Structural check for package.json
    if filename == "package.json":
        if base_content is None and candidate_content is None:
            return True
        try:
            base_json = json.loads(base_content or "{}") if base_content else {}
            cand_json = json.loads(candidate_content or "{}") if candidate_content else {}
        except Exception:
            return True  # A file that cannot be parsed counts as changed

        # Compare scripts
        if base_json.get("scripts") != cand_json.get("scripts"):
            return True
        # Compare workspaces
        if base_json.get("workspaces") != cand_json.get("workspaces"):
            return True
        # Compare runner config keys
        for key in ("jest", "vitest", "mocha", "ava"):
            if base_json.get(key) != cand_json.get(key):
                return True
        # Dependency, version, description or other edits are not verifier changes
        return False

    # 2. Structural check for pyproject.toml
    if filename == "pyproject.toml":
        if base_content is None and candidate_content is None:
            return True
        try:
            import tomllib
            base_toml = tomllib.loads(base_content or "") if base_content else {}
            cand_toml = tomllib.loads(candidate_content or "") if candidate_content else {}
        except Exception:
            return True  # A file that cannot be parsed counts as changed

        # Check [tool.pytest*]
        base_tool = base_toml.get("tool", {}) if isinstance(base_toml, dict) else {}
        cand_tool = cand_toml.get("tool", {}) if isinstance(cand_toml, dict) else {}
        base_pytest = {k: v for k, v in base_tool.items() if k.startswith("pytest")}
        cand_pytest = {k: v for k, v in cand_tool.items() if k.startswith("pytest")}
        if base_pytest != cand_pytest:
            return True

        # Check [project.scripts]
        base_project = base_toml.get("project", {}) if isinstance(base_toml, dict) else {}
        cand_project = cand_toml.get("project", {}) if isinstance(cand_toml, dict) else {}
        if base_project.get("scripts") != cand_project.get("scripts"):
            return True

        # Check [tool.poetry.scripts]
        base_poetry = base_tool.get("poetry", {}) if isinstance(base_tool, dict) else {}
        cand_poetry = cand_tool.get("poetry", {}) if isinstance(cand_tool, dict) else {}
        if base_poetry.get("scripts") != cand_poetry.get("scripts"):
            return True

        return False

    # 3. Known build and test config filenames
    if filename in BUILD_TEST_CONFIG_FILES or norm in BUILD_TEST_CONFIG_FILES:
        return True

    # 4. Prefixes for dynamic test config names
    for pattern in ("jest.config.", "vitest.config.", ".mocharc.", "tsconfig."):
        if filename.startswith(pattern):
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


def is_shared_test_config_or_fixture(path: Union[str, Path]) -> bool:
    """Returns True if path is a shared test configuration or fixture file (e.g. conftest.py, fixtures)."""
    if not path:
        return False
    raw = str(path).replace("\\", "/").strip()
    norm = posixpath.normpath(raw)
    if norm.startswith("./"):
        norm = norm[2:]
    parts = [p.lower() for p in norm.split("/")]
    filename = parts[-1]

    if filename == "conftest.py":
        return True
    if filename in (
        "setuptests.ts",
        "setuptests.js",
        "setuptests.cjs",
        "setuptests.mjs",
        "jest.setup.js",
        "jest.setup.ts",
    ):
        return True
    if any(p in ("fixtures", "fixture", "test_fixtures", "__fixtures__") for p in parts):
        return True
    return False


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


class TrustRecord(BaseModel):
    """Schema for a repository trust record stored outside the repo under <JEV_HOME>/trust/<repo_id>.json."""
    repo_id: str
    command_hash: str
    profile_id: Optional[str] = None
    registry_version: Optional[str] = None
    argv: List[str]
    cwd: str = "."
    platform: str
    executable_path: Optional[str] = None
    executable_version: Optional[str] = None
    approved_at: str
    approved_by: str = "owner"


TrustBinding = TrustRecord


class RepoConfig(BaseModel):
    """Repository configuration declared in .jev/config.json (declarations only).

    Any embedded 'trust' field is ignored and flagged as ignored_embedded_trust.
    """
    model_config = ConfigDict(extra="ignore")

    runner_profile_id: Optional[str] = Field(default=None, alias="runner_profile")
    build_command: Optional[Union[str, List[str]]] = None
    test_command: Optional[Union[str, List[str]]] = None
    cwd: Optional[str] = None
    target_rule: Optional[str] = None
    generated_paths: List[str] = Field(default_factory=list)
    expected_tests_policy: Optional[str] = None
    ignored_embedded_trust: bool = False

    @property
    def runner_profile(self) -> Optional[str]:
        return self.runner_profile_id

    @model_validator(mode="before")
    @classmethod
    def check_embedded_trust(cls, data: Any) -> Any:
        if isinstance(data, dict) and "trust" in data:
            # Embedded trust in .jev/config.json is IGNORED
            d = dict(data)
            del d["trust"]
            d["ignored_embedded_trust"] = True
            return d
        return data


def compute_command_hash(config: RepoConfig) -> str:
    """Computes SHA-256 of the declared command fields."""
    test_argv = normalize_command_argv(config.test_command) if config.test_command else []
    build_argv = normalize_command_argv(config.build_command) if config.build_command else []
    payload = {
        "build_command": build_argv,
        "cwd": str(config.cwd or "."),
        "profile_id": str(config.runner_profile_id or ""),
        "test_command": test_argv,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        "cwd": str(cwd or "."),
        "exact_argv": list(exact_argv),
        "executable_path": str(executable_path or ""),
        "executable_version": str(executable_version or ""),
        "platform": str(platform),
        "profile_id": str(profile_id or ""),
        "registry_version": str(registry_version or ""),
        "repo_id": str(repo_id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_trust(
    config: RepoConfig,
    repo_dir: Union[str, Path],
    approved_by: str = "owner",
    executable_path: Optional[str] = None,
    executable_version: Optional[str] = None,
    registry_version: Optional[str] = None,
    platform: Optional[str] = None,
) -> TrustRecord:
    """Writes owner trust record outside the repository to <JEV_HOME>/trust/<repo_id>.json."""
    repo_id = compute_repo_id(repo_dir)
    exact_argv = normalize_command_argv(config.test_command) if config.test_command else []
    cwd = config.cwd or "."
    plat = platform or sys.platform

    if not registry_version:
        try:
            from jev.runner_registry import load_registry
            reg = load_registry()
            registry_version = reg.registry_version
        except Exception:
            registry_version = "1.0.0"

    cmd_hash = compute_command_hash(config)
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    rec = TrustRecord(
        repo_id=repo_id,
        command_hash=cmd_hash,
        profile_id=config.runner_profile_id,
        registry_version=registry_version,
        argv=exact_argv,
        cwd=cwd,
        platform=plat,
        executable_path=executable_path,
        executable_version=executable_version,
        approved_at=now_utc,
        approved_by=approved_by,
    )

    trust_path = get_trust_file_path(repo_id)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
    return rec


def validate_trust(
    config: RepoConfig,
    repo_dir: Union[str, Path],
    executable_path: Optional[str] = None,
    executable_version: Optional[str] = None,
    platform: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Validates repository configuration against the external trust record in <JEV_HOME>/trust/<repo_id>.json."""
    repo_id = compute_repo_id(repo_dir)
    trust_path = get_trust_file_path(repo_id)

    if not trust_path.exists() or not trust_path.is_file():
        return False, "untrusted_repo_config: run scripts/trust_repo.py"

    try:
        data = json.loads(trust_path.read_text(encoding="utf-8"))
        rec = TrustRecord.model_validate(data)
    except Exception as e:
        return False, f"untrusted_repo_config: run scripts/trust_repo.py ({e})"

    if rec.repo_id != repo_id:
        return False, f"repo_id mismatch: {rec.repo_id} != {repo_id}"

    curr_platform = platform or sys.platform
    if rec.platform != curr_platform:
        return False, f"platform mismatch: {rec.platform} != {curr_platform}"

    if config.runner_profile_id and rec.profile_id and rec.profile_id != config.runner_profile_id:
        return False, f"profile mismatch: {rec.profile_id} != {config.runner_profile_id}"

    # Verify command matches
    config_argv = normalize_command_argv(config.test_command) if config.test_command else []
    if rec.argv != config_argv:
        return False, "command changed: approval invalidated"

    # Verify command hash matches
    curr_cmd_hash = compute_command_hash(config)
    if rec.command_hash != curr_cmd_hash:
        return False, "command changed: approval invalidated"

    config_cwd = config.cwd or "."
    if rec.cwd != config_cwd:
        return False, f"cwd mismatch: {rec.cwd} != {config_cwd}"

    if executable_path and rec.executable_path and rec.executable_path != executable_path:
        return False, f"executable path mismatch: {rec.executable_path} != {executable_path}"

    if executable_version and rec.executable_version and rec.executable_version != executable_version:
        return False, f"executable version mismatch: {rec.executable_version} != {executable_version}"

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
    """Saves .jev/config.json inside the repository (declarations only, never trust)."""
    p = Path(repo_dir).resolve()
    target = p / ".jev" / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    # Exclude internal ignored_embedded_trust flag
    data = config.model_dump(exclude={"ignored_embedded_trust"}, exclude_none=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return target
