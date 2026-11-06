"""src/jev/proposals.py

Command proposals and approval system for unknown ecosystems (Phase 3B).
Proposals are saved to .jev/proposals/<id>.json and never execute without approval.
Approval writes the trusted repository configuration entry into .jev/config.json
and records owner trust in <JEV_HOME>/trust/<repo_id>.json.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field

yaml = importlib.import_module("yaml")

from jev.repo_config import (
    EXACT_MAKEFILE_TARGETS,
    RepoConfig,
    TrustRecord,
    compute_command_hash,
    compute_repo_id,
    extract_makefile_target_and_recipe,
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
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|password|passwd|secret|token(?!s)|auth|credential|bearer|private[_-]?key)"
    r"(?![_-]?count\b)(?:[_-][A-Za-z0-9]+)*(?![A-Za-z0-9_])"
)

SECRET_VALUE_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"),  # GitHub personal access tokens
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),        # OpenAI / Anthropic API keys
    re.compile(r"(?i)\b(?:sk|pk)_[a-z0-9_\-]{12,}\b"),  # Stripe / other API keys
    re.compile(r"(?i)bearer\s+([A-Za-z0-9_\-\.]{16,})"),  # Bearer tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS Access Key ID
    re.compile(r"AIza[A-Za-z0-9_-]{35}\b"),       # Bare Google API keys
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),  # JWTs
    re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@"),  # Any URL scheme credentials
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:[A-Za-z0-9]+[_-])*"
        r"(?:api[_-]?key|password|passwd|secret|token(?!s)|auth|credential|bearer|private[_-]?key)"
        r"(?![_-]?count\b)(?:[_-][A-Za-z0-9]+)*(?![A-Za-z0-9_])"
        r"\s*[:=]\s*(?!\d+(?:\b|$))[^\s,;\"'\}]{3,}"
    ),
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z]+ )?PRIVATE KEY-----"),
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


logger = logging.getLogger(__name__)


def argv_contains_secrets(argv: Sequence[str]) -> bool:
    """Checks whether any argument or the combined command contains secret patterns."""
    if not argv:
        return False
    for arg in argv:
        if not isinstance(arg, str):
            continue
        if redact_secrets_in_text(arg) != arg:
            return True
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(arg):
                return True
    joined = " ".join(argv)
    if redact_secrets_in_text(joined) != joined:
        return True
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(joined):
            return True
    return False


def compute_proposal_identity_hash(
    repo_id: str,
    exact_argv: Sequence[str],
    cwd: str = ".",
    platform: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 600,
    target_rule: Optional[str] = "full_suite",
    recipe_text: Optional[str] = None,
) -> str:
    """Computes a deterministic SHA-256 identity hash for a command proposal."""
    payload: Dict[str, Any] = {
        "cwd": str(cwd or "."),
        "env": sorted((env or {}).items()),
        "exact_argv": list(exact_argv),
        "platform": str(platform or sys.platform),
        "repo_id": str(repo_id),
        "target_rule": str(target_rule or "full_suite"),
        "timeout": int(timeout),
    }
    if recipe_text:
        payload["recipe_text"] = str(recipe_text)
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
    recipe_lines: List[str] = Field(default_factory=list)
    recipe_text: Optional[str] = None
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
            recipe_text=self.recipe_text,
        )
        if self.identity_hash != expected_hash:
            return False, f"identity_hash mismatch: expected {expected_hash}, got {self.identity_hash}"
        return True, None


def get_proposals_dir(repo_dir: Union[str, Path]) -> Path:
    """Returns .jev/proposals directory for a repository, creating it if needed."""
    pdir = Path(repo_dir).resolve() / ".jev" / "proposals"
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def save_proposal(
    proposal: Proposal,
    repo_dir: Union[str, Path],
    raise_on_secret: bool = False,
) -> Optional[Path]:
    """Saves proposal to .jev/proposals/<id>.json with secrets redacted.

    If secret patterns are found inside exact_argv, refuses to save the proposal
    with a clear message to pass secrets via environment variables.
    """
    if argv_contains_secrets(proposal.exact_argv):
        msg = (
            "Refusing to save proposal: exact_argv contains secret token or credentials. "
            "A command cannot be safely redacted; pass secrets via environment variables instead."
        )
        logger.warning(msg)
        if raise_on_secret:
            raise ValueError(msg)
        return None

    # Redact secrets in every stored text field and environment variables
    proposal.source_evidence = redact_secrets_in_text(proposal.source_evidence)
    if proposal.expanded_package_script:
        proposal.expanded_package_script = redact_secrets_in_text(proposal.expanded_package_script)
    if proposal.tool_path:
        proposal.tool_path = redact_secrets_in_text(proposal.tool_path)
    if proposal.tool_version:
        proposal.tool_version = redact_secrets_in_text(proposal.tool_version)
    if proposal.network_assumptions:
        proposal.network_assumptions = redact_secrets_in_text(proposal.network_assumptions)
    proposal.scope = [redact_secrets_in_text(s) for s in proposal.scope]
    proposal.generated_paths = [redact_secrets_in_text(p) for p in proposal.generated_paths]
    proposal.environment_variables = redact_secrets_in_env(proposal.environment_variables)

    # Recompute identity hash to match the redacted environment variables
    proposal.identity_hash = compute_proposal_identity_hash(
        repo_id=proposal.repo_id,
        exact_argv=proposal.exact_argv,
        cwd=proposal.cwd,
        platform=proposal.platform,
        env=proposal.environment_variables,
        timeout=proposal.timeout,
        target_rule=proposal.target_rule,
        recipe_text=proposal.recipe_text,
    )

    pdir = get_proposals_dir(repo_dir)
    target = pdir / f"{proposal.id}.json"
    target.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_proposal(proposal_id: str, repo_dir: Union[str, Path]) -> Optional[Proposal]:
    """Loads a proposal by id from .jev/proposals/<id>.json."""
    if not proposal_id or ".." in proposal_id or "/" in proposal_id or "\\" in proposal_id:
        return None
    pdir = get_proposals_dir(repo_dir).resolve()
    clean_id = proposal_id.removesuffix(".json")
    path = (pdir / f"{clean_id}.json").resolve()
    if path.parent != pdir:
        return None
    if not path.exists():
        # Also check without prefix if user passed short hash
        matches = list(pdir.glob(f"*{clean_id}*.json"))
        if matches:
            path = matches[0].resolve()
            if path.parent != pdir:
                return None
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
    """Resolves executable path on the host platform using shutil.which without executing any subprocess."""
    if not cmd:
        return None, None
    exe_name = cmd[0]
    exe_path = shutil.which(exe_name)
    if not exe_path and sys.platform == "win32" and exe_name.lower() in ("make", "make.exe"):
        exe_path = shutil.which("mingw32-make")
    if not exe_path:
        return None, None
    return exe_path, None


INSTALL_STEP_PREFIXES = (
    "pip install",
    "python -m pip install",
    "npm install",
    "npm i",
    "npm ci",
    "yarn install",
    "yarn add",
    "pnpm install",
    "pnpm i",
    "cargo install",
    "poetry install",
    "poetry add",
    "bundle install",
    "composer install",
)


def is_install_command_line(cmd_line: str) -> bool:
    """Returns True if command line is an installation or setup command."""
    clean = cmd_line.strip()
    return any(clean.startswith(pfx) for pfx in INSTALL_STEP_PREFIXES)


def build_proposal_from_repo(repo_dir: Union[str, Path]) -> Optional[Proposal]:
    """Builds a command proposal for an unknown ecosystem from Makefile, CI, manifests, or README.
    
    Never executes any command. Returns None if no plausible test command could be extracted.
    """
    repo = Path(repo_dir).resolve()
    repo_id = compute_repo_id(repo)

    argv: Optional[List[str]] = None
    source_evidence: Optional[str] = None
    expanded_script: Optional[str] = None
    recipe_lines: List[str] = []
    recipe_text: Optional[str] = None

    # 1. Makefile / GNUmakefile
    for mf_name in ("Makefile", "GNUmakefile", "makefile"):
        mf_path = repo / mf_name
        if mf_path.exists():
            try:
                content = mf_path.read_text(encoding="utf-8", errors="replace")
                target, r_lines = extract_makefile_target_and_recipe(content)
                if target:
                    argv = ["make", target]
                    source_evidence = f"{mf_name} target '{target}'"
                    recipe_lines = r_lines
                    recipe_text = "\n".join(r_lines) if r_lines else None
                    break
            except Exception:
                pass
    
    # 2. CI workflows (.github/workflows/*.yml, *.yaml)
    unexpanded_ci_step: Optional[Tuple[str, str]] = None
    if not argv:
        wf_dir = repo / ".github" / "workflows"
        if wf_dir.is_dir():
            for wf in sorted(wf_dir.glob("*.y*ml")):
                try:
                    wf_text = wf.read_text(encoding="utf-8", errors="replace")
                    parsed_wf = yaml.safe_load(wf_text)
                    if not isinstance(parsed_wf, dict):
                        continue
                    jobs = parsed_wf.get("jobs", {})
                    if not isinstance(jobs, dict):
                        continue
                    for job_name, job_data in jobs.items():
                        if not isinstance(job_data, dict):
                            continue
                        steps = job_data.get("steps", [])
                        if not isinstance(steps, list):
                            continue
                        for step in steps:
                            if not isinstance(step, dict):
                                continue
                            run_block = step.get("run")
                            if not run_block or not isinstance(run_block, str):
                                continue
                            block_lines = [
                                ln.strip()
                                for ln in run_block.splitlines()
                                if ln.strip() and not ln.strip().startswith("#")
                            ]
                            for line in block_lines:
                                if is_install_command_line(line):
                                    continue
                                line_lower = line.lower()
                                if any(kw in line_lower for kw in ("test", "pytest", "check", "run-tests")):
                                    if "${{" in line:
                                        if unexpanded_ci_step is None:
                                            unexpanded_ci_step = (wf.name, line)
                                        continue
                                    split_cmd = shlex.split(line, posix=(sys.platform != "win32"))
                                    if split_cmd:
                                        argv = split_cmd
                                        source_evidence = f"CI workflow '{wf.name}' step: '{line}'"
                                        break
                            if argv:
                                break
                        if argv:
                            break
                    if argv:
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
                        if "${{" in raw_cmd:
                            if unexpanded_ci_step is None:
                                unexpanded_ci_step = (r_name, raw_cmd)
                        else:
                            argv = shlex.split(raw_cmd, posix=(sys.platform != "win32"))
                            source_evidence = f"{r_name} code block: '{raw_cmd}'"
                            break
                except Exception:
                    pass

    if argv and any("${{" in arg for arg in argv):
        argv = []

    if not argv:
        if unexpanded_ci_step:
            wf_name, line = unexpanded_ci_step
            argv = []
            source_evidence = f"CI workflow '{wf_name}' step: '{line}'"
        else:
            return None

    exe_path, exe_ver = inspect_executable(argv) if argv else (None, None)
    identity_hash = compute_proposal_identity_hash(
        repo_id=repo_id,
        exact_argv=argv,
        cwd=".",
        platform=sys.platform,
        env={},
        timeout=600,
        target_rule="full_suite",
        recipe_text=recipe_text,
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
        recipe_lines=recipe_lines,
        recipe_text=recipe_text,
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
    Refuses approval if exact_argv contains secrets.
    """
    repo = Path(repo_dir).resolve()
    if not proposal.exact_argv:
        raise ValueError("Cannot approve proposal: proposal has no exact_argv (needs manual command).")
    if any("${{" in arg for arg in proposal.exact_argv):
        raise ValueError("Cannot approve proposal: exact_argv contains unexpanded CI expressions (${{ ... }}).")
    if argv_contains_secrets(proposal.exact_argv):
        raise ValueError(
            "Refusing to approve proposal: exact_argv contains secret token or credentials. "
            "A command cannot be safely redacted; pass secrets via environment variables instead."
        )

    is_valid, err = proposal.verify_identity()
    if not is_valid:
        raise ValueError(f"Cannot approve proposal: {err}")

    recipe_h = None
    if proposal.recipe_text:
        recipe_h = hashlib.sha256(proposal.recipe_text.encode("utf-8")).hexdigest()

    # Build repo config declaration
    cfg = RepoConfig(
        runner_profile_id="custom",
        test_command=proposal.exact_argv,
        cwd=proposal.cwd or ".",
        target_rule=proposal.target_rule,
        generated_paths=proposal.generated_paths,
        recipe_hash=recipe_h,
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
        recipe_hash=recipe_h,
    )

    # Mark proposal as approved
    proposal.status = "approved"
    proposal.approved_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    proposal.approved_by = approved_by
    saved_path = save_proposal(proposal, repo, raise_on_secret=True)
    if saved_path is None:
        raise ValueError("Refusing to approve proposal: save_proposal refused because of secret in command")

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
    if proposal.tool_path and sys.platform == "win32" and proposal.exact_argv and proposal.exact_argv[0].lower() in ("make", "make.exe"):
        if "mingw32-make" in proposal.tool_path.lower():
            tool_info += " (resolved mingw32-make for make on Windows)"
    if proposal.tool_version:
        tool_info += f" [{proposal.tool_version}]"
    lines.append(f"Tool Path/Version:  {tool_info}")

    lines.append(f"Target Rule:        {proposal.target_rule}")
    lines.append(f"Scope:              {proposal.scope if proposal.scope else '(entire repository)'}")

    # Makefile Recipe Lines
    if proposal.recipe_lines:
        lines.append("\nMakefile Recipe Lines:")
        for r_line in proposal.recipe_lines:
            lines.append(f"  {r_line}")

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

    # Notice for unexpanded CI expressions
    has_unexpanded = False
    for arg in proposal.exact_argv:
        if "${{" in arg:
            has_unexpanded = True
            break
    if not has_unexpanded and proposal.source_evidence and "${{" in proposal.source_evidence:
        has_unexpanded = True
    if has_unexpanded or not proposal.exact_argv:
        lines.append("\nNOTICE: CI workflow expressions (${{ ... }}) detected: needs manual command.")

    # Exact argv (one argument per line)
    lines.append("\nExact Argv (one argument per line):")
    if proposal.exact_argv:
        if any("${{" in arg for arg in proposal.exact_argv):
            lines.append("  (unexpanded expression detected - needs manual command)")
        else:
            for idx, arg in enumerate(proposal.exact_argv):
                lines.append(f"  [{idx}] {redact_secrets_in_text(arg)}")
    else:
        lines.append("  (none - needs manual command)")

    # Mandatory disclaimer statement
    lines.append("\n-----------------------------------------------------------------")
    lines.append(f"NOTICE: {APPROVAL_DISCLAIMER}")
    lines.append("-----------------------------------------------------------------")
    return "\n".join(lines)
