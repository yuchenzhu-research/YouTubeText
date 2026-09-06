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
        process = subprocess.Popen(
            [str(part) for part in command],
            env=child_environment,
            start_new_session=True,
        )
        try:
            return process.wait()
        except KeyboardInterrupt:
            _terminate_process_group(process, grace_seconds=grace_seconds)
            return 130
        except BaseException:
            _terminate_process_group(process, grace_seconds=grace_seconds)
            raise


def _terminate_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float,
) -> None:
    """Stop and reap the isolated worker group with bounded escalation."""

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
