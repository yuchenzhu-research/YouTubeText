"""Run URL work in an isolated child process owned by the terminal command."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

SUPERVISED_ENV = "YOUTUBETEXT_SUPERVISED_WORKER"
RUN_TEMP_ENV = "YOUTUBETEXT_RUN_TEMP"
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsKillOnCloseJob:
    """Own one worker tree until the supervisor closes the job handle."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle
        limits = _JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            self._handle, process._handle
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None and not self._kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def run_supervised(
    command: Sequence[str],
    *,
    temp_parent: str | os.PathLike[str] | None = None,
    grace_seconds: float = 0.75,
) -> int:
    """Run one command with a disposable temp root and return its exit code."""

    if not command:
        raise ValueError("supervised command must not be empty")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must not be negative")

    parent = Path(temp_parent).expanduser() if temp_parent is not None else None
    with tempfile.TemporaryDirectory(prefix="youtubetext-run-", dir=parent) as directory:
        run_root = Path(directory).resolve()
        child_environment = dict(os.environ)
        child_environment[SUPERVISED_ENV] = "1"
        child_environment[RUN_TEMP_ENV] = str(run_root)
        child_environment["TMPDIR"] = str(run_root)
        child_environment["TEMP"] = str(run_root)
        child_environment["TMP"] = str(run_root)
        job = _WindowsKillOnCloseJob() if os.name == "nt" else None
        try:
            process = subprocess.Popen(
                [str(part) for part in command],
                env=child_environment,
                **_popen_options(),
            )
            if job is not None:
                try:
                    job.assign(process)
                except OSError:
                    _terminate_windows_process_tree(process, grace_seconds=0)
                    raise
            try:
                return process.wait()
            except KeyboardInterrupt:
                _terminate_process_tree(
                    process, grace_seconds=grace_seconds, windows_job=job
                )
                return 130
            except BaseException:
                _terminate_process_tree(
                    process, grace_seconds=grace_seconds, windows_job=job
                )
                raise
        finally:
            if job is not None:
                job.close()


def _popen_options() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                _CREATE_NEW_PROCESS_GROUP,
            )
        }
    return {"start_new_session": True}


def _terminate_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float,
    windows_job: _WindowsKillOnCloseJob | None = None,
) -> None:
    """Stop and reap the isolated worker group with bounded escalation."""

    if os.name == "nt":
        _terminate_windows_process_tree(
            process, grace_seconds=grace_seconds, job=windows_job
        )
        return
    _terminate_posix_process_group(process, grace_seconds=grace_seconds)


def _terminate_posix_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float,
) -> None:
    """Escalate signals against one POSIX process group."""

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
    for escalation in signals:
        if not _process_group_exists(process.pid):
            break
        _signal_process_group(process, escalation)
        if _wait_until_group_stops(process, grace_seconds):
            break

    try:
        process.wait(timeout=max(0.1, grace_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _terminate_windows_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float,
    job: _WindowsKillOnCloseJob | None = None,
) -> None:
    """Offer Ctrl+Break, then close the job or force-stop a live root tree."""

    break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if break_event is not None:
        try:
            process.send_signal(break_event)
        except (OSError, ValueError):
            pass
        _wait_for_process(process, grace_seconds)

    if job is not None:
        job.close()
        if _wait_for_process(process, max(0.1, grace_seconds)):
            return
        process.kill()
        process.wait()
        return

    if process.poll() is not None:
        return

    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(5.0, grace_seconds),
        )
    except (OSError, subprocess.SubprocessError):
        pass

    if _wait_for_process(process, grace_seconds):
        return
    process.kill()
    process.wait()


def _signal_process_group(process: subprocess.Popen, value: signal.Signals) -> None:
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        return


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can deny signal-zero probing of a live process group even
        # though the child group is still ours to terminate.
        return True
    return True


def _wait_until_group_stops(
    process: subprocess.Popen,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        process.poll()
        if not _process_group_exists(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _wait_for_process(process: subprocess.Popen, timeout_seconds: float) -> bool:
    try:
        process.wait(timeout=max(0.0, timeout_seconds))
        return True
    except subprocess.TimeoutExpired:
        return False
