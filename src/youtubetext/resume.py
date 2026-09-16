"""Deep resume module: one task lifecycle, hidden locks and stage storage."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from platformdirs import user_cache_dir

from .cache import TranscriptCache
from .domain import SourceMetadata, TaskOptions, Transcript
from .media import MediaPurpose

MEDIA_REVISION = "yt-dlp-stable-part-v1"
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ResumeResult:
    transcript: Transcript
    reused: bool = False
    warning: str = ""


class ResumeSession(Protocol):
    async def media(
        self,
        purpose: MediaPurpose,
        produce: Callable[[Path], Awaitable[Path]],
    ) -> Path: ...


class ResumeStore(Protocol):
    async def run(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
        work: Callable[[ResumeSession], Awaitable[Transcript]],
    ) -> ResumeResult: ...

    async def discard_incomplete(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
    ) -> str: ...


class EphemeralResumeStore:
    """Run the same interface with disposable state when resume is disabled."""

    def __init__(self, temp_root: Path | None = None) -> None:
        self._temp_root = Path(temp_root).expanduser() if temp_root else None

    async def run(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
        work: Callable[[ResumeSession], Awaitable[Transcript]],
    ) -> ResumeResult:
        del metadata, options
        if self._temp_root is not None:
            self._temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="task-",
            dir=self._temp_root,
        ) as directory:
            session = _ResumeSession(Path(directory), persistent=False)
            return ResumeResult(transcript=await work(session))

    async def discard_incomplete(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
    ) -> str:
        del metadata, options
        return ""


class LocalResumeStore:
    """Serialize one fallback task and preserve only useful incomplete stages."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        auth_scope: str = "anonymous",
        lock_poll_seconds: float = 0.05,
        temp_root: Path | None = None,
    ) -> None:
        if lock_poll_seconds <= 0:
            raise ValueError("lock_poll_seconds must be greater than zero")
        self.root = (
            Path(root).expanduser()
            if root is not None
            else Path(user_cache_dir("YouTubeText"))
        )
        self._transcripts = TranscriptCache(
            self.root / "transcripts",
            auth_scope=auth_scope,
        )
        self._tasks = self.root / "tasks"
        self._locks = self.root / "locks"
        self._lock_poll_seconds = float(lock_poll_seconds)
        self._temp_root = Path(temp_root).expanduser() if temp_root else None

    async def run(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
        work: Callable[[ResumeSession], Awaitable[Transcript]],
    ) -> ResumeResult:
        key = self._transcripts.key(metadata, options)
        lock_descriptor: int | None = None
        try:
            lock_descriptor = await self._acquire_lock(key)
            _ensure_private_directory(self._tasks)
            task_root = self._tasks / key
            _ensure_private_directory(task_root)
        except OSError:
            if lock_descriptor is not None:
                _release_lock(lock_descriptor)
            result = await EphemeralResumeStore(self._temp_root).run(
                metadata,
                options,
                work,
            )
            return ResumeResult(
                transcript=result.transcript,
                warning=(
                    "Local resume storage was unavailable; this run used temporary "
                    "files only."
                ),
            )

        try:
            cached = await _run_blocking(self._transcripts.load, metadata, options)
            if cached is not None:
                cleanup_ok = await _run_blocking(_remove_tree, task_root)
                warning = (
                    "The completed transcript was reused, but stale temporary media "
                    "could not be removed."
                    if not cleanup_ok
                    else ""
                )
                return ResumeResult(cached, reused=True, warning=warning)

            session = _ResumeSession(task_root, persistent=True)
            transcript = await work(session)
            saved = await _run_blocking(self._transcripts.save, transcript, options)
            if not saved:
                return ResumeResult(
                    transcript,
                    warning=(
                        "Local resume cache could not be saved; this transcript will "
                        "not be reusable."
                    ),
                )

            cleanup_ok = await _run_blocking(_remove_tree, task_root)
            warning = (
                "The transcript was cached, but temporary media could not be removed."
                if not cleanup_ok
                else ""
            )
            return ResumeResult(transcript, warning=warning)
        finally:
            _release_lock(lock_descriptor)

    async def discard_incomplete(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
    ) -> str:
        """Remove abandoned fallback media after a platform caption succeeds."""

        key = self._transcripts.key(metadata, options)
        try:
            lock_descriptor = await self._acquire_lock(key)
        except OSError:
            return "Local resume temporary files could not be checked or removed."

        try:
            cleanup_ok = await _run_blocking(_remove_tree, self._tasks / key)
            return (
                "Platform captions were used, but stale temporary media could not "
                "be removed."
                if not cleanup_ok
                else ""
            )
        finally:
            _release_lock(lock_descriptor)

    async def _acquire_lock(self, key: str) -> int:
        _ensure_private_directory(self.root)
        _ensure_private_directory(self._locks)
        path = self._locks / f"{key}.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(path, 0o600)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    await asyncio.sleep(self._lock_poll_seconds)
        except BaseException:
            os.close(descriptor)
            raise


class _ResumeSession:
    def __init__(self, task_root: Path, *, persistent: bool) -> None:
        self._root = task_root
        self._persistent = persistent

    async def media(
        self,
        purpose: MediaPurpose,
        produce: Callable[[Path], Awaitable[Path]],
    ) -> Path:
        media_directory = self._root / "media" / purpose.value
        stages_directory = self._root / "stages"
        _ensure_private_directory(media_directory)
        if self._persistent:
            _ensure_private_directory(stages_directory)
            manifest = stages_directory / f"media-{purpose.value}.json"
            state = _load_media_manifest(manifest, purpose)
            if state is not None and state.get("status") == "complete":
                cached = _completed_media(state, media_directory)
                if cached is not None:
                    return cached
            if state is None or state.get("status") != "downloading":
                if not _remove_tree(media_directory):
                    raise RuntimeError("stale resume media could not be removed")
                _ensure_private_directory(media_directory)
            _atomic_json(
                manifest,
                {
                    "revision": MEDIA_REVISION,
                    "purpose": purpose.value,
                    "status": "downloading",
                },
            )

        produced = Path(await produce(media_directory)).resolve()
        media_root = media_directory.resolve()
        if not produced.is_file() or not produced.is_relative_to(media_root):
            raise RuntimeError("media producer returned a file outside its task directory")
        if produced.stat().st_size <= 0:
            raise RuntimeError("media producer returned an empty file")

        if self._persistent:
            relative = produced.relative_to(media_root)
            _atomic_json(
                manifest,
                {
                    "revision": MEDIA_REVISION,
                    "purpose": purpose.value,
                    "status": "complete",
                    "path": str(relative),
                    "size": produced.stat().st_size,
                },
            )
        return produced


def _load_media_manifest(
    path: Path,
    purpose: MediaPurpose,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("revision") != MEDIA_REVISION:
            return None
        if payload.get("purpose") != purpose.value:
            return None
        if payload.get("status") not in {"downloading", "complete"}:
            return None
        return payload
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _completed_media(
    payload: dict[str, object],
    media_directory: Path,
) -> Path | None:
    try:
        relative = Path(str(payload["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = (media_directory / relative).resolve()
        if not candidate.is_relative_to(media_directory.resolve()):
            return None
        expected_size = int(payload["size"])
        if expected_size <= 0 or candidate.stat().st_size != expected_size:
            return None
        return candidate
    except (OSError, KeyError, TypeError, ValueError):
        return None


async def _run_blocking(call: Callable[..., T], *args: object) -> T:
    """Keep lock-protected file mutations alive until their worker thread stops."""

    worker = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except BaseException:
            pass
        raise


def _atomic_json(path: Path, payload: object) -> bool:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor: int | None = None
    try:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        return True
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _remove_tree(path: Path) -> bool:
    try:
        if not path.exists():
            return True
        if path.is_symlink():
            return False
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
