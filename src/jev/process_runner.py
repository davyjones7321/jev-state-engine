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

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(resolved_cwd),
        "env": env,
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
