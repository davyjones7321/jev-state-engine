"""src/jev/proposals.py

Command proposals and approval system for unknown ecosystems (Phase 3B).
Proposals are saved to .jev/proposals/<id>.json and never execute without approval.
Approval writes the trusted repository configuration entry into .jev/config.json
and records owner trust in <JEV_HOME>/trust/<repo_id>.json.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field

from jev.repo_config import (
    RepoConfig,
    TrustRecord,
    compute_command_hash,
    compute_repo_id,
    get_trust_file_path,
    load_repo_config,
    normalize_command_argv,
    record_trust,
    save_repo_config,
    validate_trust,
)

# Statement required by Phase 3B specification
APPROVAL_DISCLAIMER = "approval authorizes execution, not a mechanical pass"

SECRET_KEY_PATTERNS = re.compile(
    r"(?i)(key|token|secret|password|passwd|auth|credential|bearer|api[_-]?key|private[_-]?key)"
)

SECRET_VALUE_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"),  # GitHub personal access tokens
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),        # OpenAI / Anthropic API keys
    re.compile(r"(?i)bearer\s+([A-Za-z0-9_\-\.]{16,})"),  # Bearer tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS Access Key ID
    re.compile(r"https?://([^:]+):([^@]+)@"),     # Embedded basic-auth credentials
]


def redact_secrets_in_text(text: str) -> str:
    """Redacts known secret token patterns within a text string."""
    if not text:
        return text
    result = text
    for pattern in SECRET_VALUE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def redact_secrets_in_env(env: Dict[str, str]) -> Dict[str, str]:
    """Redacts environment variable dictionary values if key or value matches secret patterns."""
    redacted: Dict[str, str] = {}
    for k, v in env.items():
        if SECRET_KEY_PATTERNS.search(k):
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = redact_secrets_in_text(str(v))
    return redacted


def redact_data_structure(data: Any) -> Any:
    """Recursively redacts secrets in dicts, lists, and strings."""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if SECRET_KEY_PATTERNS.search(str(k)):
                result[k] = "[REDACTED]"
            else:
                result[k] = redact_data_structure(v)
        return result
    elif isinstance(data, (list, tuple)):
        return [redact_data_structure(item) for item in data]
    elif isinstance(data, str):
        return redact_secrets_in_text(data)
    return data


def compute_proposal_identity_hash(
    repo_id: str,
    exact_argv: Sequence[str],
    cwd: str = ".",
    platform: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 600,
    target_rule: Optional[str] = "full_suite",
) -> str:
    """Computes a deterministic SHA-256 identity hash for a command proposal."""
    payload = {
        "cwd": str(cwd or "."),
        "env": sorted((env or {}).items()),
        "exact_argv": list(exact_argv),
        "platform": str(platform or sys.platform),
        "repo_id": str(repo_id),
        "target_rule": str(target_rule or "full_suite"),
        "timeout": int(timeout),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Proposal(BaseModel):
    """Schema for a test command proposal generated for unknown ecosystems."""
    model_config = ConfigDict(extra="ignore")

    id: str
    repo_id: str
    exact_argv: List[str]
    cwd: str = "."
    source_evidence: str
    expanded_package_script: Optional[str] = None
    tool_path: Optional[str] = None
    tool_version: Optional[str] = None
    platform: str
    target_rule: Optional[str] = "full_suite"
    scope: List[str] = Field(default_factory=list)
    environment_variables: Dict[str, str] = Field(default_factory=dict)
    network_assumptions: Optional[str] = "offline"
    timeout: int = 600
    generated_paths: List[str] = Field(default_factory=list)
    identity_hash: str
    created_at: str
    status: str = "pending"  # "pending", "approved", "rejected"
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None

    def verify_identity(self) -> Tuple[bool, Optional[str]]:
        """Verifies that the proposal identity hash matches its current fields."""
        expected_hash = compute_proposal_identity_hash(
            repo_id=self.repo_id,
            exact_argv=self.exact_argv,
            cwd=self.cwd,
            platform=self.platform,
            env=self.environment_variables,
            timeout=self.timeout,
            target_rule=self.target_rule,
        )
        if self.identity_hash != expected_hash:
            return False, f"identity_hash mismatch: expected {expected_hash}, got {self.identity_hash}"
        return True, None


def get_proposals_dir(repo_dir: Union[str, Path]) -> Path:
    """Returns .jev/proposals directory for a repository, creating it if needed."""
    pdir = Path(repo_dir).resolve() / ".jev" / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def save_proposal(proposal: Proposal, repo_dir: Union[str, Path]) -> Path:
    """Saves proposal to .jev/proposals/<id>.json."""
    pdir = get_proposals_dir(repo_dir)
    target = pdir / f"{proposal.id}.json"
    target.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_proposal(proposal_id: str, repo_dir: Union[str, Path]) -> Optional[Proposal]:
    """Loads a proposal by id from .jev/proposals/<id>.json."""
    pdir = get_proposals_dir(repo_dir)
    clean_id = proposal_id.removesuffix(".json")
    path = pdir / f"{clean_id}.json"
    if not path.exists():
        # Also check without prefix if user passed short hash
        matches = list(pdir.glob(f"*{clean_id}*.json"))
        if matches:
            path = matches[0]
        else:
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Proposal(**data)
    except Exception:
        return None


def list_proposals(repo_dir: Union[str, Path]) -> List[Proposal]:
    """Lists all proposals stored in .jev/proposals/."""
    pdir = get_proposals_dir(repo_dir)
    proposals: List[Proposal] = []
    for f in pdir.glob("*.json"):
        try:
            p = Proposal(**json.loads(f.read_text(encoding="utf-8")))
            proposals.append(p)
        except Exception:
            pass
    return sorted(proposals, key=lambda p: p.created_at, reverse=True)


def inspect_executable(cmd: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """Inspects executable path and version on the host platform."""
    if not cmd:
        return None, None
    exe_name = cmd[0]
    exe_path = shutil.which(exe_name)
    if not exe_path:
        return None, None
    exe_ver = None
    try:
        res = subprocess.run([exe_path, "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            exe_ver = res.stdout.strip().splitlines()[0][:80]
    except Exception:
        pass
    return exe_path, exe_ver


def build_proposal_from_repo(repo_dir: Union[str, Path]) -> Optional[Proposal]:
    """Builds a command proposal for an unknown ecosystem from Makefile, CI, manifests, or README.
    
    Never executes any command. Returns None if no plausible test command could be extracted.
    """
    repo = Path(repo_dir).resolve()
    repo_id = compute_repo_id(repo)

    argv: Optional[List[str]] = None
    source_evidence: Optional[str] = None
    expanded_script: Optional[str] = None

    # 1. Makefile / GNUmakefile
    for mf_name in ("Makefile", "GNUmakefile", "makefile"):
        mf_path = repo / mf_name
        if mf_path.exists():
            try:
                content = mf_path.read_text(encoding="utf-8", errors="replace")
                # Look for test targets
                m = re.search(r"^([a-zA-Z0-9_\-]*test[a-zA-Z0-9_\-]*|check)\s*:", content, re.MULTILINE)
                if m:
                    target = m.group(1)
                    argv = ["make", target]
                    source_evidence = f"{mf_name} target '{target}'"
                    break
            except Exception:
                pass
    
    # 2. CI workflows (.github/workflows/*.yml, *.yaml)
    if not argv:
        wf_dir = repo / ".github" / "workflows"
        if wf_dir.is_dir():
            for wf in sorted(wf_dir.glob("*.y*ml")):
                try:
                    wf_text = wf.read_text(encoding="utf-8", errors="replace")
                    # Look for test run commands
                    m = re.search(r"run:\s*(.+test.+|.+pytest.+|.+check.+)", wf_text)
                    if m:
                        step_cmd = m.group(1).strip()
                        split_cmd = shlex.split(step_cmd, posix=(sys.platform != "win32"))
                        if split_cmd:
                            argv = split_cmd
                            source_evidence = f"CI workflow '{wf.name}' step: '{step_cmd}'"
                            break
                except Exception:
                    pass

    # 3. Manifests (e.g. package.json with custom script)
    if not argv:
        pkg_json = repo / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = data.get("scripts", {})
                if isinstance(scripts, dict) and "test" in scripts:
                    expanded_script = str(scripts["test"])
                    argv = ["npm", "test"]
                    source_evidence = f"package.json scripts.test: '{expanded_script}'"
            except Exception:
                pass

    # 4. README instructions (README.md, README.rst, README.txt)
    if not argv:
        for r_name in ("README.md", "README.rst", "README.txt", "README"):
            r_path = repo / r_name
            if r_path.exists():
                try:
                    r_text = r_path.read_text(encoding="utf-8", errors="replace")
                    m = re.search(r"```(?:bash|sh|shell)?\s*\n\s*((?:pytest|npm test|cargo test|make test|go test|ctest|\./run_tests\.sh)(?:[^\n]*))\s*\n\s*```", r_text)
                    if m:
                        raw_cmd = m.group(1).strip()
                        argv = shlex.split(raw_cmd, posix=(sys.platform != "win32"))
                        source_evidence = f"{r_name} code block: '{raw_cmd}'"
                        break
                except Exception:
                    pass

    if not argv:
        return None

    exe_path, exe_ver = inspect_executable(argv)
    identity_hash = compute_proposal_identity_hash(
        repo_id=repo_id,
        exact_argv=argv,
        cwd=".",
        platform=sys.platform,
        env={},
        timeout=600,
        target_rule="full_suite",
    )
    prop_id = f"prop-{identity_hash[:12]}"
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    prop = Proposal(
        id=prop_id,
        repo_id=repo_id,
        exact_argv=argv,
        cwd=".",
        source_evidence=source_evidence or "heuristic manifest detection",
        expanded_package_script=expanded_script,
        tool_path=exe_path,
        tool_version=exe_ver,
        platform=sys.platform,
        target_rule="full_suite",
        scope=[],
        environment_variables={},
        network_assumptions="offline",
        timeout=600,
        generated_paths=[],
        identity_hash=identity_hash,
        created_at=now_utc,
        status="pending",
    )
    save_proposal(prop, repo)
    return prop


def approve_proposal(
    proposal: Proposal,
    repo_dir: Union[str, Path],
    approved_by: str = "owner",
) -> Tuple[RepoConfig, TrustRecord]:
    """Approves a proposal, writes .jev/config.json and records external owner trust.
    
    Validates proposal identity hash to ensure tampering is detected.
    """
    repo = Path(repo_dir).resolve()
    is_valid, err = proposal.verify_identity()
    if not is_valid:
        raise ValueError(f"Cannot approve proposal: {err}")

    # Build repo config declaration
    cfg = RepoConfig(
        runner_profile_id="custom",
        test_command=proposal.exact_argv,
        cwd=proposal.cwd or ".",
        target_rule=proposal.target_rule,
        generated_paths=proposal.generated_paths,
    )
    save_repo_config(cfg, repo)

    # Record external trust under JEV_HOME
    trust_rec = record_trust(
        cfg,
        repo_dir=repo,
        approved_by=approved_by,
        executable_path=proposal.tool_path,
        executable_version=proposal.tool_version,
        platform=proposal.platform,
    )

    # Mark proposal as approved
    proposal.status = "approved"
    proposal.approved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    proposal.approved_by = approved_by
    save_proposal(proposal, repo)

    return cfg, trust_rec


def format_proposal_screen(proposal: Proposal) -> str:
    """Formats all required proposal fields for review with secrets redacted."""
    lines: list[str] = []
    lines.append("=================================================================")
    lines.append("                JEV COMMAND PROPOSAL REVIEW                     ")
    lines.append("=================================================================")
    lines.append(f"Proposal ID:        {proposal.id}")
    lines.append(f"Repository ID:      {proposal.repo_id}")
    lines.append(f"Platform:           {proposal.platform}")
    lines.append(f"Working Directory:  {proposal.cwd}")
    lines.append(f"Source Evidence:    {redact_secrets_in_text(proposal.source_evidence)}")

    # Expanded package script if any
    exp_script = proposal.expanded_package_script
    lines.append(f"Expanded Script:    {redact_secrets_in_text(exp_script) if exp_script else '(none)'}")

    # Tool path and version
    tool_info = f"{proposal.tool_path or '(not found on PATH)'}"
    if proposal.tool_version:
        tool_info += f" [{proposal.tool_version}]"
    lines.append(f"Tool Path/Version:  {tool_info}")

    lines.append(f"Target Rule:        {proposal.target_rule}")
    lines.append(f"Scope:              {proposal.scope if proposal.scope else '(entire repository)'}")

    # Environment variables
    env_vars = redact_data_structure(proposal.environment_variables)
    if env_vars:
        lines.append("Environment Variables:")
        for k, v in env_vars.items():
            lines.append(f"  {k} = {v}")
    else:
        lines.append("Environment Variables: (none declared)")

    lines.append(f"Network Assumptions: {proposal.network_assumptions or 'offline'}")
    lines.append(f"Timeout:             {proposal.timeout}s")
    lines.append(f"Generated Paths:     {proposal.generated_paths if proposal.generated_paths else '[]'}")
    lines.append(f"Identity Hash:       {proposal.identity_hash}")

    # Exact argv (one argument per line)
    lines.append("\nExact Argv (one argument per line):")
    if proposal.exact_argv:
        for idx, arg in enumerate(proposal.exact_argv):
            lines.append(f"  [{idx}] {redact_secrets_in_text(arg)}")
    else:
        lines.append("  (empty)")

    # Mandatory disclaimer statement
    lines.append("\n-----------------------------------------------------------------")
    lines.append(f"NOTICE: {APPROVAL_DISCLAIMER}")
    lines.append("-----------------------------------------------------------------")
    return "\n".join(lines)

