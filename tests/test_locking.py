from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

import youtubetext._locking as locking_module
from youtubetext._locking import (
    FileLock,
    _WindowsLockOperations,
    open_file_lock,
)


def test_host_file_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "task lock 字幕.lock"
    first = open_file_lock(path)
    second = open_file_lock(path)

    try:
        assert first.try_acquire()
        assert not second.try_acquire()
        first.close()
        assert first.closed
        assert second.try_acquire()
    finally:
        first.close()
        second.close()


def test_closed_file_lock_rejects_acquisition(tmp_path: Path) -> None:
    lock = open_file_lock(tmp_path / "task.lock")

    lock.close()
    lock.close()

    with pytest.raises(ValueError, match="closed"):
        lock.try_acquire()


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.busy = False
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, descriptor: int, mode: int, count: int) -> None:
        self.calls.append((descriptor, mode, count))
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        if self.busy and mode == self.LK_NBLCK:
            raise OSError(errno.EACCES, "lock is busy")


def test_windows_lock_keeps_empty_file_and_normalizes_busy(tmp_path: Path) -> None:
    path = tmp_path / "windows.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    api = _FakeMsvcrt()
    operations = _WindowsLockOperations(api)
    lock = FileLock(descriptor, operations)

    operations.prepare(descriptor)
    assert path.read_bytes() == b""
    api.busy = True
    assert not lock.try_acquire()
    api.busy = False
    assert lock.try_acquire()
    lock.close()

    assert [mode for _descriptor, mode, _count in api.calls] == [
        api.LK_NBLCK,
        api.LK_NBLCK,
        api.LK_UNLCK,
    ]
    assert all(count == 1 for _descriptor, _mode, count in api.calls)


def test_windows_lock_propagates_non_contention_errors(tmp_path: Path) -> None:
    path = tmp_path / "windows.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    api = _FakeMsvcrt()
    operations = _WindowsLockOperations(api)
    lock = FileLock(descriptor, operations)
    operations.prepare(descriptor)

    def invalid_lock(_descriptor: int, _mode: int, _count: int) -> None:
        raise OSError(errno.EINVAL, "invalid lock request")

    api.locking = invalid_lock  # type: ignore[method-assign]
    with pytest.raises(OSError) as error:
        lock.try_acquire()
    assert error.value.errno == errno.EINVAL
    lock.close()


def test_unlock_failure_still_closes_descriptor(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path / "windows.lock", os.O_RDWR | os.O_CREAT, 0o600)
    api = _FakeMsvcrt()
    operations = _WindowsLockOperations(api)
    lock = FileLock(descriptor, operations)
    operations.prepare(descriptor)
    assert lock.try_acquire()

    def failed_unlock(_descriptor: int, mode: int, _count: int) -> None:
        if mode == api.LK_UNLCK:
            raise OSError(errno.EIO, "unlock failed")

    api.locking = failed_unlock  # type: ignore[method-assign]
    with pytest.raises(OSError, match="unlock failed"):
        lock.close()
    assert lock.closed
    with pytest.raises(OSError) as error:
        os.fstat(descriptor)
    assert error.value.errno == errno.EBADF
    lock.close()


def test_windows_dir_fd_is_never_silently_ignored(
    monkeypatch,
) -> None:
    monkeypatch.setattr(locking_module.os, "name", "nt")
    with pytest.raises(NotImplementedError, match="absolute path"):
        open_file_lock("task.lock", dir_fd=123)


def test_file_lock_is_released_by_another_process(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root)
    path = tmp_path / "process lock.lock"
    script = """
import sys
from youtubetext._locking import open_file_lock
lock = open_file_lock(sys.argv[1])
assert lock.try_acquire()
print('ready', flush=True)
sys.stdin.readline()
lock.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(path)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender: FileLock | None = None
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        contender = open_file_lock(path)
        assert not contender.try_acquire()

        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
        assert contender.try_acquire()
    finally:
        if contender is not None:
            contender.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_import_does_not_require_fcntl() -> None:
    source_root = Path(__file__).parents[1] / "src"
    script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl is unavailable')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import youtubetext.cli
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
