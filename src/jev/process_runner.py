from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Win32 ctypes structures and constants for Job Objects
if sys.platform == "win32":
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    JobObjectBasicAccountingInformation = 1
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


@dataclass
class CommandResult:
    argv: List[str]
    cwd: Union[str, Path]
    exit_code: Optional[int]
    duration_ms: float
    timed_out: bool
    launch_failed: bool
    termination_reason: Optional[str]
    tool_path: Optional[str]
    stdout_tail: str
    stderr_tail: str
    stdout_sha256: str
    stderr_sha256: str
    cleanup_method: str  # "job_object" | "process_group" | "taskkill_fallback" | "none"
    descendants_remaining: int
    stdout: str = ""
    stderr: str = ""


# Environment scrubbing allowlist and sensitive filters
SAFE_ENV_NAMES = {
    # System / OS essentials
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "LANG",
    "CI",
    # Windows system essentials
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "PUBLIC",
    "ALLUSERSPROFILE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    # POSIX system essentials
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "HOSTNAME",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    # Python
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    # Node / npm
    "NODE_PATH",
    "NODE_ENV",
    "NVM_BIN",
    "NVM_DIR",
    "NODE_OPTIONS",
    # Git
    "GIT_EXEC_PATH",
    "GIT_TEMPLATE_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    # Common build toolchains
    "CARGO_HOME",
    "RUSTUP_HOME",
    "GOPATH",
    "GOROOT",
    "JAVA_HOME",
    "MAVEN_HOME",
    "GRADLE_HOME",
    "DOTNET_ROOT",
    # Test harness runner configuration
    "JEV_FAKE_RUNNER_CONFIG",
    "FAKE_RUNNER_CONFIG",
}

SAFE_ENV_PREFIXES = (
    "LC_",
    "NPM_CONFIG_",
)

SENSITIVE_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
)


def get_scrubbed_env(
    declared_env: Optional[Dict[str, str]] = None,
    opt_out: bool = False,
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build a scrubbed environment dictionary for child processes.

    1. Layer 1 (Allowlist): Keep only explicitly allowed environment variables.
    2. Layer 2 (Sensitive Substrings): Drop any variable whose name contains
       KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL, or AUTH.
    3. Explicitly declared variables (from approved proposals/callers) are merged back.
    4. If opt_out is True (or JEV_DISABLE_ENV_SCRUB=1), the full environment is preserved
       with declared_env merged.
    """
    if opt_out or os.environ.get("JEV_DISABLE_ENV_SCRUB") == "1":
        env_copy = dict(base_env if base_env is not None else os.environ)
        if declared_env:
            env_copy.update(declared_env)
        return env_copy

    source_env = base_env if base_env is not None else os.environ
    clean_env: Dict[str, str] = {}

    for k, v in source_env.items():
        k_upper = k.upper()
        # Layer 1: Allowlist check
        is_allowed = (k_upper in SAFE_ENV_NAMES) or any(
            k_upper.startswith(p) for p in SAFE_ENV_PREFIXES
        )
        if not is_allowed:
            continue

        # Layer 2: Drop sensitive substrings
        if any(sub in k_upper for sub in SENSITIVE_SUBSTRINGS):
            continue

        clean_env[k] = v
        if sys.platform == "win32" and k != k_upper:
            clean_env[k_upper] = v

    # Add back variables explicitly declared by the proposal / caller
    if declared_env:
        clean_env.update(declared_env)

    return clean_env


def _count_live_posix_descendants(pgid: int, proc_path: Optional[Path] = None) -> int:
    """Count non-zombie processes belonging to pgid.

    Reads /proc/<pid>/stat state != 'Z' for members of the group;
    if /proc is unavailable, falls back to the killpg result.
    Never reports 0 when a live descendant exists.
    """
    proc_dir = Path(proc_path) if proc_path else Path("/proc")
    if not proc_dir.is_dir():
        if hasattr(os, "killpg"):
            try:
                os.killpg(pgid, 0)
                return 1
            except (ProcessLookupError, OSError):
                return 0
        return 0

    live_count = 0
    checked_any = False
    try:
        for entry in os.scandir(proc_dir):
            if entry.name.isdigit():
                stat_file = Path(entry.path) / "stat"
                try:
                    text = stat_file.read_text(encoding="utf-8", errors="replace")
                    rparen = text.rfind(")")
                    if rparen != -1:
                        rest = text[rparen + 1:].strip().split()
                        if len(rest) >= 3:
                            proc_state = rest[0]
                            proc_pgrp = int(rest[2])
                            if proc_pgrp == pgid:
                                checked_any = True
                                if proc_state != "Z":
                                    live_count += 1
                except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
                    continue
    except Exception:
        if hasattr(os, "killpg"):
            try:
                os.killpg(pgid, 0)
                return 1
            except (ProcessLookupError, OSError):
                return 0
        return 0

    if not checked_any:
        if hasattr(os, "killpg"):
            try:
                os.killpg(pgid, 0)
                return 1
            except (ProcessLookupError, OSError):
                return 0
        return 0

    return live_count


def run_command(
    argv: Union[List[str], str],
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    shell: Optional[bool] = None,
    scrub_env: bool = True,
    opt_out: Optional[bool] = None,
) -> CommandResult:
    resolved_cwd = Path(cwd).resolve() if cwd else Path.cwd()

    if isinstance(argv, str):
        is_shell = True if shell is None else shell
        argv_list = [argv]
        cmd_to_spawn = argv if is_shell else argv.split()
    else:
        is_shell = False if shell is None else shell
        argv_list = list(argv)
        cmd_to_spawn = argv_list

    tool_path = None
    if not is_shell and argv_list:
        tool_path = shutil.which(argv_list[0])
        if not tool_path and sys.platform == 'win32' and argv_list[0].lower() in ('make', 'make.exe'):
            mingw = shutil.which('mingw32-make')
            if mingw:
                tool_path = mingw
                cmd_to_spawn[0] = mingw

    start_time = time.perf_counter()
    hJob = None
    job_created = False

    if sys.platform == "win32":
        try:
            hJob = kernel32.CreateJobObjectW(None, None)
            if hJob:
                info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                set_res = kernel32.SetInformationJobObject(
                    hJob,
                    JobObjectExtendedLimitInformation,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
                job_created = bool(set_res)
        except Exception:
            job_created = False
            hJob = None

    effective_opt_out = opt_out if opt_out is not None else not scrub_env
    child_env = get_scrubbed_env(declared_env=env, opt_out=effective_opt_out)

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(resolved_cwd),
        "env": child_env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": is_shell,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd_to_spawn, **popen_kwargs)
    except (FileNotFoundError, PermissionError, OSError) as e:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        if hJob and sys.platform == "win32":
            kernel32.CloseHandle(hJob)
        empty_sha = hashlib.sha256(b"").hexdigest()
        return CommandResult(
            argv=argv_list,
            cwd=resolved_cwd,
            exit_code=None,
            duration_ms=duration_ms,
            timed_out=False,
            launch_failed=True,
            termination_reason="launch_failed",
            tool_path=tool_path,
            stdout_tail="",
            stderr_tail="",
            stdout_sha256=empty_sha,
            stderr_sha256=empty_sha,
            cleanup_method="none",
            descendants_remaining=0,
            stdout="",
            stderr="",
        )

    if sys.platform == "win32" and job_created and hJob:
        try:
            assigned = kernel32.AssignProcessToJobObject(hJob, int(proc._handle))
            if not assigned:
                job_created = False
        except Exception:
            job_created = False

    timed_out = False
    termination_reason = None
    cleanup_method = "none"
    descendants_remaining = 0
    stdout = ""
    stderr = ""

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        termination_reason = "timeout"
        if sys.platform == "win32":
            if job_created and hJob:
                cleanup_method = "job_object"
                kernel32.TerminateJobObject(hJob, 1)
                time.sleep(0.05)
                acc = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
                res = kernel32.QueryInformationJobObject(
                    hJob,
                    JobObjectBasicAccountingInformation,
                    ctypes.byref(acc),
                    ctypes.sizeof(acc),
                    None,
                )
                descendants_remaining = int(acc.ActiveProcesses) if res else 0
            else:
                cleanup_method = "taskkill_fallback"
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                        env=get_scrubbed_env(),
                    )
                except Exception:
                    pass
                descendants_remaining = 0
            try:
                proc.kill()
            except Exception:
                pass
        else:
            cleanup_method = "process_group"
            try:
                try:
                    pgid = os.getpgid(proc.pid)
                except (ProcessLookupError, OSError):
                    pgid = proc.pid

                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

                grace_deadline = time.perf_counter() + 2.0
                while time.perf_counter() < grace_deadline:
                    try:
                        os.killpg(pgid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        break
                    except Exception:
                        break

                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

                # Reap the leader first
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=0.5)
                    except Exception:
                        pass

                # Poll up to 1s for the group to disappear
                group_disappeared = False
                poll_deadline = time.perf_counter() + 1.0
                while time.perf_counter() < poll_deadline:
                    try:
                        os.killpg(pgid, 0)
                        time.sleep(0.02)
                    except (ProcessLookupError, OSError):
                        group_disappeared = True
                        break

                if group_disappeared:
                    descendants_remaining = 0
                else:
                    try:
                        os.killpg(pgid, 0)
                        descendants_remaining = _count_live_posix_descendants(pgid)
                    except (ProcessLookupError, OSError):
                        descendants_remaining = 0
            except (ProcessLookupError, OSError):
                descendants_remaining = 0

            try:
                proc.kill()
            except Exception:
                pass

        exit_code = None
        try:
            out, err = proc.communicate(timeout=1.0)
            stdout = out or ""
            stderr = err or ""
        except Exception:
            stdout = ""
            stderr = ""
    finally:
        if hJob and sys.platform == "win32":
            kernel32.CloseHandle(hJob)

    duration_ms = (time.perf_counter() - start_time) * 1000.0
    stdout_str = stdout or ""
    stderr_str = stderr or ""

    stdout_tail = stdout_str[-8000:] if len(stdout_str) > 8000 else stdout_str
    stderr_tail = stderr_str[-8000:] if len(stderr_str) > 8000 else stderr_str
    stdout_sha256 = hashlib.sha256(stdout_str.encode("utf-8", errors="replace")).hexdigest()
    stderr_sha256 = hashlib.sha256(stderr_str.encode("utf-8", errors="replace")).hexdigest()

    return CommandResult(
        argv=argv_list,
        cwd=resolved_cwd,
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        launch_failed=False,
        termination_reason=termination_reason,
        tool_path=tool_path,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        cleanup_method=cleanup_method,
        descendants_remaining=descendants_remaining,
        stdout=stdout_str,
        stderr=stderr_str,
    )
