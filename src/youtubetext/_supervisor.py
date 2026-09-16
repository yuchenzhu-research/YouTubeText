"""Run URL work in an isolated child process owned by the terminal command."""

from __future__ import annotations

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
        process = subprocess.Popen(
            [str(part) for part in command],
            env=child_environment,
            **_popen_options(),
        )
        try:
            return process.wait()
        except KeyboardInterrupt:
            _terminate_process_tree(process, grace_seconds=grace_seconds)
            return 130
        except BaseException:
            _terminate_process_tree(process, grace_seconds=grace_seconds)
            raise


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
) -> None:
    """Stop and reap the isolated worker group with bounded escalation."""

    if os.name == "nt":
        _terminate_windows_process_tree(process, grace_seconds=grace_seconds)
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
) -> None:
    """Offer Ctrl+Break, then use taskkill to include descendant processes."""

    break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
    if break_event is not None:
        try:
            process.send_signal(break_event)
        except (OSError, ValueError):
            pass
        if _wait_for_process(process, grace_seconds):
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
