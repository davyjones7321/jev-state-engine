"""src/jev/preflight.py

Phase 4: Preflight and support profile checks.

Inspects environment, runner profile, host platform, executable availability,
command shape, watch/daemon flags, external service declarations, target representability,
and timeout/process tree management before executing any test subprocess.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, Field

from jev.mech_evidence import (
    Comparison,
    EvidenceLevel,
    MechanicalEvidence,
    MechReason,
    MechStatus,
    TargetConfidence,
)
from jev.runner_registry import RunnerProfile, load_registry, is_version_supported
from jev.repo_config import load_repo_config, RepoConfig, validate_trust


class PreflightStatus(str, Enum):
    IN_PROFILE = "IN_PROFILE"
    OUTSIDE_PROFILE = "OUTSIDE_PROFILE"
    PROFILE_UNKNOWN = "PROFILE_UNKNOWN"


IN_PROFILE = PreflightStatus.IN_PROFILE
OUTSIDE_PROFILE = PreflightStatus.OUTSIDE_PROFILE
PROFILE_UNKNOWN = PreflightStatus.PROFILE_UNKNOWN


class PreflightCheckResult(BaseModel):
    name: str
    passed: bool
    status: PreflightStatus
    reason: Optional[MechReason] = None
    detail: str = ""


class PreflightResult(BaseModel):
    status: PreflightStatus
    passed: bool
    checks: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    reason: Optional[MechReason] = None
    detail: str = ""
    evidence: Optional[MechanicalEvidence] = None
    profile_id: Optional[str] = None
    command: Optional[List[str]] = None
    platform: str = sys.platform

    @property
    def is_in_profile(self) -> bool:
        return self.status == PreflightStatus.IN_PROFILE

    @property
    def is_outside_profile(self) -> bool:
        return self.status == PreflightStatus.OUTSIDE_PROFILE

    @property
    def is_profile_unknown(self) -> bool:
        return self.status == PreflightStatus.PROFILE_UNKNOWN


# Watch and daemon indicators
WATCH_STANDALONE_FLAGS = {
    "--watch",
    "-w",
    "--watchAll",
    "--watch-all",
    "--watch-files",
    "--live",
    "--hot",
    "--daemon",
    "-d",
    "--serve",
}

INTERACTIVE_FLAGS = {
    "-i",
    "--interactive",
    "-it",
    "--prompt",
}


def check_git_and_disposable_worktree(
    repo_dir: Path,
    worktree_dir: Optional[Path] = None,
) -> PreflightCheckResult:
    """Check 1: git and disposable worktree capability."""
    git_bin = shutil.which("git")
    if not git_bin:
        return PreflightCheckResult(
            name="git_and_disposable_worktree",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.ENVIRONMENT_UNAVAILABLE,
            detail="git executable not found in PATH",
        )

    # Check git repository validity
    is_git_repo = False
    if (repo_dir / ".git").exists():
        is_git_repo = True
    else:
        # Check if parent or worktree is inside git
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and "true" in res.stdout.strip().lower():
                is_git_repo = True
        except Exception:
            is_git_repo = False

    if not is_git_repo:
        return PreflightCheckResult(
            name="git_and_disposable_worktree",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.ENVIRONMENT_UNAVAILABLE,
            detail=f"Directory '{repo_dir}' is not a valid git repository",
        )

    # If worktree_dir specified, verify it is accessible
    if worktree_dir is not None and worktree_dir.exists():
        if not worktree_dir.is_dir():
            return PreflightCheckResult(
                name="git_and_disposable_worktree",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                detail=f"Worktree directory '{worktree_dir}' is not a directory",
            )

    return PreflightCheckResult(
        name="git_and_disposable_worktree",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="git executable and repository verified",
    )


def check_host_platform(host_platform: Optional[str] = None) -> PreflightCheckResult:
    """Check 2: host platform verification."""
    plat = host_platform or sys.platform
    supported_platforms = {"linux", "darwin", "win32"}
    if plat not in supported_platforms:
        return PreflightCheckResult(
            name="host_platform",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.ENVIRONMENT_UNAVAILABLE,
            detail=f"Host platform '{plat}' is not recognized or supported",
        )
    return PreflightCheckResult(
        name="host_platform",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail=f"Host platform '{plat}' recognized",
    )


def check_profile_platform_fit(
    profile: Optional[RunnerProfile],
    host_platform: Optional[str] = None,
) -> PreflightCheckResult:
    """Check 3: profile/platform fit.
    
    Xcode/macOS requires darwin. Never substitute another runner.
    """
    if profile is None:
        return PreflightCheckResult(
            name="profile_platform_fit",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No specific runner profile to check against platform",
        )

    plat = host_platform or sys.platform
    if plat not in profile.platforms:
        if profile.profile_id == "xcode-test" or profile.platforms == ["darwin"]:
            return PreflightCheckResult(
                name="profile_platform_fit",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                detail=f"Profile '{profile.profile_id}' requires macOS/Xcode which is unavailable on host platform '{plat}'.",
            )
        return PreflightCheckResult(
            name="profile_platform_fit",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.ENVIRONMENT_UNAVAILABLE,
            detail=f"Profile '{profile.profile_id}' is not supported on host platform '{plat}' (supported: {profile.platforms}).",
        )

    return PreflightCheckResult(
        name="profile_platform_fit",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail=f"Profile '{profile.profile_id}' fits host platform '{plat}'",
    )


def check_executable_available(
    command: Optional[Sequence[str]],
    repo_dir: Path,
    worktree_dir: Optional[Path] = None,
    profile: Optional[RunnerProfile] = None,
    tool_version: Optional[str] = None,
) -> PreflightCheckResult:
    """Check 4: executable available and launchable.
    
    Missing tool: UNMEASURED(MISSING_TOOL), nothing installed, no strike.
    """
    if not command:
        return PreflightCheckResult(
            name="executable_available",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No test command to execute",
        )

    exe = str(command[0])
    is_available = False

    # Check sys.executable or python
    if exe == sys.executable or (exe in ("python", "python3") and shutil.which(exe)):
        is_available = True
    elif Path(exe).is_file():
        is_available = True
    elif worktree_dir and (worktree_dir / exe).is_file():
        is_available = True
    elif (repo_dir / exe).is_file():
        is_available = True
    elif shutil.which(exe):
        is_available = True

    if not is_available:
        return PreflightCheckResult(
            name="executable_available",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.MISSING_TOOL,
            detail=f"Executable '{exe}' not found.",
        )

    # Version check if profile has predicates and tool_version is provided
    if profile is not None and tool_version is not None:
        if not is_version_supported(profile, tool_version):
            return PreflightCheckResult(
                name="executable_available",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                detail=f"Tool '{exe}' version '{tool_version}' does not satisfy profile version requirements ({profile.min_version}..{profile.max_version})",
            )

    return PreflightCheckResult(
        name="executable_available",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail=f"Executable '{exe}' is available",
    )


def check_non_interactive_shape(command: Optional[Sequence[str]]) -> PreflightCheckResult:
    """Check 5: non-interactive command shape."""
    if not command:
        return PreflightCheckResult(
            name="non_interactive_shape",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No command provided",
        )

    # Check for interactive flags
    for arg in command[1:]:
        if arg in INTERACTIVE_FLAGS:
            return PreflightCheckResult(
                name="non_interactive_shape",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.TOOLING_ERROR,
                detail=f"Command contains interactive flag '{arg}'. Test commands must be non-interactive/batch.",
            )

    # Bare interactive interpreters with no script or module arguments
    if len(command) == 1:
        base_name = Path(command[0]).stem.lower()
        if base_name in ("python", "python3", "node", "irb", "bash", "sh", "zsh", "cmd", "powershell"):
            return PreflightCheckResult(
                name="non_interactive_shape",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.TOOLING_ERROR,
                detail=f"Command '{command[0]}' lacks arguments and runs an interactive REPL.",
            )

    return PreflightCheckResult(
        name="non_interactive_shape",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="Command shape is non-interactive",
    )


def check_watch_daemon_indicators(command: Optional[Sequence[str]]) -> PreflightCheckResult:
    """Check 6: watch/daemon indicators."""
    if not command:
        return PreflightCheckResult(
            name="watch_daemon_indicators",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No command provided",
        )

    for i, raw_arg in enumerate(command[1:], start=1):
        arg = raw_arg.strip()
        # Avoid file paths like test_watch.py or server.js
        if any(arg.endswith(ext) for ext in (".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java")):
            continue
        if "/" in arg or "\\" in arg:
            continue

        # Check exact standalone flags
        if arg in WATCH_STANDALONE_FLAGS:
            return PreflightCheckResult(
                name="watch_daemon_indicators",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.TOOLING_ERROR,
                detail=f"Watch or daemon mode indicator '{arg}' detected. Test execution must be bounded.",
            )

        # Check subcommands (e.g. `cargo watch`, `jest watch`)
        if i == 1 and arg.lower() in ("watch", "daemon", "serve"):
            return PreflightCheckResult(
                name="watch_daemon_indicators",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.TOOLING_ERROR,
                detail=f"Watch/daemon subcommand '{arg}' detected. Test execution must be bounded.",
            )

        # Check key=value flags e.g. --watch=true
        for prefix in ("--watch=", "--watchAll=", "--watch-all="):
            if arg.startswith(prefix):
                val = arg.split("=", 1)[1].strip().lower()
                if val not in ("false", "0", "off"):
                    return PreflightCheckResult(
                        name="watch_daemon_indicators",
                        passed=False,
                        status=PreflightStatus.OUTSIDE_PROFILE,
                        reason=MechReason.TOOLING_ERROR,
                        detail=f"Watch mode indicator '{arg}' detected. Test execution must be bounded.",
                    )

    return PreflightCheckResult(
        name="watch_daemon_indicators",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="No watch or daemon mode detected",
    )


def check_declared_external_services(
    repo_config: Optional[Any],
    command: Optional[Sequence[str]],
) -> PreflightCheckResult:
    """Check 7: declared external services or network."""
    if repo_config is not None:
        net = getattr(repo_config, "network_assumptions", None)
        if net and str(net).strip().lower() not in ("none", "offline", "false", "isolated"):
            return PreflightCheckResult(
                name="declared_external_services",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                detail=f"External network dependency declared in repo config: {net}",
            )

        services = getattr(repo_config, "external_services", None)
        if services and bool(services) and str(services).strip().lower() not in ("none", "false", "[]"):
            return PreflightCheckResult(
                name="declared_external_services",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                detail=f"External services declared in repo config: {services}",
            )

    if command:
        for arg in command:
            if arg in ("--network=host", "--remote-services", "--external-db"):
                return PreflightCheckResult(
                    name="declared_external_services",
                    passed=False,
                    status=PreflightStatus.OUTSIDE_PROFILE,
                    reason=MechReason.ENVIRONMENT_UNAVAILABLE,
                    detail=f"External service/network flag '{arg}' in command",
                )

    return PreflightCheckResult(
        name="declared_external_services",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="No external services or network declared",
    )


def check_target_representable(
    profile: Optional[RunnerProfile],
    test_targets: Optional[Sequence[Union[str, Path]]],
    worktree_dir: Optional[Path] = None,
) -> PreflightCheckResult:
    """Check 8: target representable."""
    if not test_targets:
        return PreflightCheckResult(
            name="target_representable",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No targeted scope requested (full suite)",
        )

    if len(test_targets) > 32:
        return PreflightCheckResult(
            name="target_representable",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.TOOLING_ERROR,
            detail=f"Target count {len(test_targets)} exceeds maximum of 32 targets",
        )

    # Capability check against profile
    if profile is not None:
        if not profile.target_capabilities:
            return PreflightCheckResult(
                name="target_representable",
                passed=False,
                status=PreflightStatus.OUTSIDE_PROFILE,
                reason=MechReason.TOOLING_ERROR,
                detail=f"Profile '{profile.profile_id}' does not support targeted test runs.",
            )

        for target in test_targets:
            raw_t = str(target)
            if "::" in raw_t:
                if "node_id" not in profile.target_capabilities and "filter" not in profile.target_capabilities:
                    return PreflightCheckResult(
                        name="target_representable",
                        passed=False,
                        status=PreflightStatus.OUTSIDE_PROFILE,
                        reason=MechReason.TOOLING_ERROR,
                        detail=f"Profile '{profile.profile_id}' does not support node_id targeting (capabilities: {profile.target_capabilities}).",
                    )

    # Existence check in worktree if worktree_dir available
    if worktree_dir is not None and worktree_dir.exists():
        for target in test_targets:
            file_part = str(target).partition("::")[0]
            target_path = worktree_dir / file_part
            if not target_path.exists():
                return PreflightCheckResult(
                    name="target_representable",
                    passed=False,
                    status=PreflightStatus.OUTSIDE_PROFILE,
                    reason=MechReason.NO_TEST_EXECUTION_SIGNAL,
                    detail=f"Target path '{file_part}' does not exist in worktree.",
                )

    return PreflightCheckResult(
        name="target_representable",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="Test targets are representable",
    )


def check_command_identity_complete(
    resolved_cmd: Optional[Any],
    command: Optional[Sequence[str]],
) -> PreflightCheckResult:
    """Check 9: command identity complete."""
    if resolved_cmd is not None:
        src = getattr(resolved_cmd, "source", None)
        if src == "ambiguous":
            return PreflightCheckResult(
                name="command_identity_complete",
                passed=False,
                status=PreflightStatus.PROFILE_UNKNOWN,
                reason=MechReason.NO_COMMAND,
                detail=f"Ambiguous runner profiles detected: {getattr(resolved_cmd, 'reason', '')}",
            )
        if src == "untrusted_repo_config":
            return PreflightCheckResult(
                name="command_identity_complete",
                passed=False,
                status=PreflightStatus.PROFILE_UNKNOWN,
                reason=MechReason.NO_COMMAND,
                detail="Untrusted repo config: run scripts/trust_repo.py",
            )
        if src in ("none", None) and (not command or len(command) == 0):
            return PreflightCheckResult(
                name="command_identity_complete",
                passed=True,
                status=PreflightStatus.IN_PROFILE,
                detail="No test framework or test command detected (no_tests_collected)",
            )

    if not command or len(command) == 0:
        return PreflightCheckResult(
            name="command_identity_complete",
            passed=True,
            status=PreflightStatus.IN_PROFILE,
            detail="No test command specified",
        )

    return PreflightCheckResult(
        name="command_identity_complete",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="Command identity complete",
    )


def check_timeout_and_process_tree(test_timeout: Optional[int]) -> PreflightCheckResult:
    """Check 10: timeout and process-tree capability."""
    if test_timeout is not None and test_timeout <= 0:
        return PreflightCheckResult(
            name="timeout_and_process_tree",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.TOOLING_ERROR,
            detail=f"Test timeout must be positive, got {test_timeout}",
        )

    # Process tree management capability check
    can_manage_process_tree = False
    if sys.platform == "win32":
        try:
            import ctypes
            can_manage_process_tree = hasattr(ctypes.windll, "kernel32")
        except Exception:
            can_manage_process_tree = False
    else:
        can_manage_process_tree = hasattr(os, "killpg")

    if not can_manage_process_tree:
        return PreflightCheckResult(
            name="timeout_and_process_tree",
            passed=False,
            status=PreflightStatus.OUTSIDE_PROFILE,
            reason=MechReason.TOOLING_ERROR,
            detail="Process-tree management capability unavailable on host",
        )

    return PreflightCheckResult(
        name="timeout_and_process_tree",
        passed=True,
        status=PreflightStatus.IN_PROFILE,
        detail="Timeout and process-tree capability verified",
    )


def run_preflight(
    repo_dir: Union[str, Path],
    worktree_dir: Optional[Union[str, Path]] = None,
    resolved_cmd: Optional[Any] = None,
    command: Optional[Sequence[str]] = None,
    profile: Optional[Union[RunnerProfile, str]] = None,
    test_targets: Optional[Sequence[Union[str, Path]]] = None,
    test_timeout: Optional[int] = None,
    repo_config: Optional[Any] = None,
    host_platform: Optional[str] = None,
    tool_version: Optional[str] = None,
) -> PreflightResult:
    """Executes all 10 preflight checks and produces durable evidence on failure.
    
    Statuses: IN_PROFILE, OUTSIDE_PROFILE, PROFILE_UNKNOWN.
    Stops before candidate/base test run when not IN_PROFILE.
    """
    repo_path = Path(repo_dir).resolve()
    wt_path = Path(worktree_dir).resolve() if worktree_dir else repo_path
    plat = host_platform or sys.platform

    # Resolve command if not supplied
    if resolved_cmd is None and command is None:
        try:
            from jev.workspace import resolve_test_command
            resolved_cmd = resolve_test_command(wt_path)
        except Exception:
            resolved_cmd = None

    cmd_list: Optional[List[str]] = None
    if command is not None:
        cmd_list = [str(x) for x in command]
    elif resolved_cmd is not None and getattr(resolved_cmd, "command", None):
        cmd_list = [str(x) for x in resolved_cmd.command]

    # Resolve runner profile
    resolved_profile: Optional[RunnerProfile] = None
    if isinstance(profile, RunnerProfile):
        resolved_profile = profile
    elif isinstance(profile, str):
        reg = load_registry()
        resolved_profile = reg.get_profile(profile)
    elif resolved_cmd is not None:
        pid = getattr(resolved_cmd, "runner_profile_id", None) or getattr(resolved_cmd, "ecosystem", None)
        if pid:
            reg = load_registry()
            resolved_profile = reg.get_profile(pid)

    pid_str = resolved_profile.profile_id if resolved_profile else (
        getattr(resolved_cmd, "runner_profile_id", None) if resolved_cmd else None
    )

    checks: Dict[str, Dict[str, Any]] = {}

    # Run the 10 checks
    c_results = [
        check_command_identity_complete(resolved_cmd, cmd_list),
        check_git_and_disposable_worktree(repo_path, wt_path),
        check_host_platform(plat),
        check_profile_platform_fit(resolved_profile, plat),
        check_executable_available(cmd_list, repo_path, wt_path, resolved_profile, tool_version),
        check_non_interactive_shape(cmd_list),
        check_watch_daemon_indicators(cmd_list),
        check_declared_external_services(repo_config, cmd_list),
        check_target_representable(resolved_profile, test_targets, wt_path),
        check_timeout_and_process_tree(test_timeout),
    ]

    for cr in c_results:
        checks[cr.name] = cr.model_dump()

    # Determine overall status and failure reason
    status = PreflightStatus.IN_PROFILE
    failure_reason: Optional[MechReason] = None
    detail_msg = "Preflight checks passed: command is IN_PROFILE"

    # Precedence:
    # 1. PROFILE_UNKNOWN (ambiguous profile or no command)
    # 2. OUTSIDE_PROFILE (missing tool, platform mismatch, watch mode, interactive, etc.)
    for cr in c_results:
        if not cr.passed:
            if cr.status == PreflightStatus.PROFILE_UNKNOWN:
                status = PreflightStatus.PROFILE_UNKNOWN
                failure_reason = cr.reason or MechReason.NO_COMMAND
                detail_msg = cr.detail
                break

    if status == PreflightStatus.IN_PROFILE:
        for cr in c_results:
            if not cr.passed:
                status = PreflightStatus.OUTSIDE_PROFILE
                failure_reason = cr.reason or MechReason.TOOLING_ERROR
                detail_msg = cr.detail
                break

    evidence: Optional[MechanicalEvidence] = None
    if status != PreflightStatus.IN_PROFILE:
        # Build durable evidence for outside profile / profile unknown
        target_paths = [str(x) for x in (test_targets or [])]
        target_scope = {
            "kind": "targets" if test_targets else "full_suite",
            "paths": target_paths,
            "new_target": False,
        }
        cmd_identity = {
            "cmd": cmd_list,
            "profile_id": pid_str,
            "platform": plat,
            "preflight_status": status.value,
        }
        evidence = MechanicalEvidence(
            mech_status=MechStatus.UNMEASURED,
            reason=failure_reason,
            evidence_level=EvidenceLevel.NONE,
            comparison=Comparison.NOT_COMPARED,
            comparison_valid=False,
            mechanically_unverified=True,
            unverified_kind="untested",
            full_suite_verified=False,
            command_identity=cmd_identity,
            target_scope=target_scope,
            runner_profile_id=pid_str,
            attribution_note=detail_msg,
        )

    return PreflightResult(
        status=status,
        passed=(status == PreflightStatus.IN_PROFILE),
        checks=checks,
        reason=failure_reason,
        detail=detail_msg,
        evidence=evidence,
        profile_id=pid_str,
        command=cmd_list,
        platform=plat,
    )
