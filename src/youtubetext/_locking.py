"""Private cross-platform file locks for resume-task singleflight."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Protocol


class _LockOperations(Protocol):
    def prepare(self, descriptor: int) -> None: ...

    def try_acquire(self, descriptor: int) -> bool: ...

    def release(self, descriptor: int) -> None: ...


class _FcntlAPI(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class _MsvcrtAPI(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, count: int) -> None: ...


class FileLock:
    """Own one descriptor; instances are idempotent but not thread-safe."""

    def __init__(self, descriptor: int, operations: _LockOperations) -> None:
        self._descriptor: int | None = descriptor
        self._operations = operations
        self._acquired = False

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    def try_acquire(self) -> bool:
        descriptor = self._require_descriptor()
        if self._acquired:
            return True
        self._acquired = self._operations.try_acquire(descriptor)
        return self._acquired

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            if self._acquired:
                self._operations.release(descriptor)
        finally:
            self._acquired = False
            os.close(descriptor)

    def _require_descriptor(self) -> int:
        if self._descriptor is None:
            raise ValueError("file lock is closed")
        return self._descriptor


class _PosixLockOperations:
    def __init__(self, api: _FcntlAPI) -> None:
        self._api = api

    def prepare(self, descriptor: int) -> None:
        del descriptor

    def try_acquire(self, descriptor: int) -> bool:
        try:
            self._api.flock(
                descriptor,
                self._api.LOCK_EX | self._api.LOCK_NB,
            )
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise

    def release(self, descriptor: int) -> None:
        self._api.flock(descriptor, self._api.LOCK_UN)


class _WindowsLockOperations:
    def __init__(self, api: _MsvcrtAPI) -> None:
        self._api = api

    def prepare(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)

    def try_acquire(self, descriptor: int) -> bool:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            self._api.locking(descriptor, self._api.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno == errno.EACCES:
                return False
            raise

    def release(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        self._api.locking(descriptor, self._api.LK_UNLCK, 1)


def open_file_lock(
    path: str | Path,
    *,
    dir_fd: int | None = None,
) -> FileLock:
    """Open a private lock file without importing unavailable host modules."""

    if os.name == "nt" and dir_fd is not None:
        raise NotImplementedError("Windows file locks require an absolute path")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    descriptor: int | None = None
    try:
        if dir_fd is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            descriptor = os.open(path, flags, 0o600, dir_fd=dir_fd)
        os.set_inheritable(descriptor, False)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        operations = _host_operations()
        operations.prepare(descriptor)
        return FileLock(descriptor, operations)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _host_operations() -> _LockOperations:
    if os.name == "nt":
        import msvcrt

        return _WindowsLockOperations(msvcrt)

    import fcntl

    return _PosixLockOperations(fcntl)
