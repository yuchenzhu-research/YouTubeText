"""Deep resume module: one task lifecycle, hidden locks and stage storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from platformdirs import user_cache_dir

from ._locking import FileLock, open_file_lock
from .cache import TranscriptCache
from .domain import SourceMetadata, TaskOptions, Transcript
from .media import FRAME_SAMPLING_REVISION, MediaPurpose, SampledFrame
from .ocr import BoundingBox, OCRFrame, OCRObservation, TimedOCRFrame
from .runtime import run_blocking

MEDIA_REVISION = "yt-dlp-stable-part-v1"
OCR_CHECKPOINT_SCHEMA = 1
OCR_REVISION = "apple-vision-raw-observations-v1"


@dataclass(frozen=True, slots=True)
class ResumeResult:
    transcript: Transcript
    reused: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class CacheUsage:
    root: Path
    transcript_count: int = 0
    transcript_bytes: int = 0
    task_count: int = 0
    task_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.transcript_bytes + self.task_bytes

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "completed_transcripts": {
                "count": self.transcript_count,
                "bytes": self.transcript_bytes,
            },
            "incomplete_tasks": {
                "count": self.task_count,
                "bytes": self.task_bytes,
            },
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class CacheCleanup:
    root: Path
    removed_tasks: int = 0
    removed_bytes: int = 0
    active_tasks: int = 0
    failed_tasks: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "removed_tasks": self.removed_tasks,
            "removed_bytes": self.removed_bytes,
            "active_tasks": self.active_tasks,
            "failed_tasks": self.failed_tasks,
        }


@dataclass(frozen=True, slots=True)
class OCRRecipe:
    """All inputs that can change Apple Vision's raw observations."""

    languages: tuple[str, ...]
    batch_size: int = 32
    sampling_revision: str = FRAME_SAMPLING_REVISION
    engine_revision: str = "unspecified-ocr-engine-v1"
    accurate: bool = True
    minimum_text_height: float = 0.012

    def __post_init__(self) -> None:
        languages = tuple(
            dict.fromkeys(language.strip() for language in self.languages if language.strip())
        )
        batch_size = int(self.batch_size)
        sampling_revision = self.sampling_revision.strip()
        engine_revision = self.engine_revision.strip()
        minimum_text_height = float(self.minimum_text_height)
        if batch_size < 1:
            raise ValueError("OCR batch_size must be at least one")
        if not sampling_revision:
            raise ValueError("OCR sampling_revision must not be empty")
        if not engine_revision:
            raise ValueError("OCR engine_revision must not be empty")
        if not math.isfinite(minimum_text_height) or not 0 <= minimum_text_height <= 1:
            raise ValueError("OCR minimum_text_height must be between zero and one")
        object.__setattr__(self, "languages", languages)
        object.__setattr__(self, "batch_size", batch_size)
        object.__setattr__(self, "sampling_revision", sampling_revision)
        object.__setattr__(self, "engine_revision", engine_revision)
        object.__setattr__(self, "accurate", bool(self.accurate))
        object.__setattr__(self, "minimum_text_height", minimum_text_height)


class ResumeSession(Protocol):
    async def media(
        self,
        purpose: MediaPurpose,
        produce: Callable[[Path], Awaitable[Path]],
    ) -> Path: ...

    async def ocr(
        self,
        sampled_frames: Sequence[SampledFrame],
        recipe: OCRRecipe,
        recognize: Callable[
            [Sequence[Path], OCRRecipe],
            Awaitable[tuple[OCRFrame, ...]],
        ],
        progress: Callable[[int, int], Awaitable[None]],
    ) -> tuple[TimedOCRFrame, ...]: ...


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

    def usage(self) -> CacheUsage:
        """Return aggregate cache size without exposing task or source identities."""

        transcript_count, transcript_bytes = _flat_json_usage(self._transcripts.root)
        task_count, task_bytes = _task_tree_usage(self._tasks)
        return CacheUsage(
            root=self.root,
            transcript_count=transcript_count,
            transcript_bytes=transcript_bytes,
            task_count=task_count,
            task_bytes=task_bytes,
        )

    def clear_incomplete(self) -> CacheCleanup:
        """Delete abandoned task state while skipping every active task lock."""

        if os.name == "nt":
            raise NotImplementedError(
                "cache clear-incomplete is not supported on Windows; "
                "no cache files were deleted"
            )

        root_descriptor: int | None = None
        tasks_descriptor: int | None = None
        locks_descriptor: int | None = None
        try:
            try:
                root_descriptor = _open_directory(self.root)
            except FileNotFoundError:
                return CacheCleanup(root=self.root)
            except OSError:
                return CacheCleanup(root=self.root, failed_tasks=1)

            try:
                tasks_descriptor = _open_directory("tasks", dir_fd=root_descriptor)
            except FileNotFoundError:
                return CacheCleanup(root=self.root)
            except OSError:
                return CacheCleanup(root=self.root, failed_tasks=1)

            try:
                task_keys = _resumable_task_keys(tasks_descriptor)
            except OSError:
                return CacheCleanup(root=self.root, failed_tasks=1)
            if not task_keys:
                return CacheCleanup(root=self.root)

            try:
                os.fchmod(root_descriptor, 0o700)
                locks_descriptor = _open_private_directory(
                    root_descriptor,
                    "locks",
                )
            except OSError:
                return CacheCleanup(
                    root=self.root,
                    failed_tasks=len(task_keys),
                )

            removed_tasks = 0
            removed_bytes = 0
            active_tasks = 0
            failed_tasks = 0
            for task_key in task_keys:
                lock: FileLock | None = None
                try:
                    lock = open_file_lock(
                        f"{task_key}.lock",
                        dir_fd=locks_descriptor,
                    )
                    if not lock.try_acquire():
                        active_tasks += 1
                        continue

                    if not _is_directory(task_key, dir_fd=tasks_descriptor):
                        continue
                    task_bytes = _regular_tree_bytes_at(
                        tasks_descriptor,
                        task_key,
                    )
                    if _remove_tree_at(tasks_descriptor, task_key):
                        removed_tasks += 1
                        removed_bytes += task_bytes
                    else:
                        failed_tasks += 1
                except OSError:
                    failed_tasks += 1
                finally:
                    if lock is not None:
                        lock.close()

            return CacheCleanup(
                root=self.root,
                removed_tasks=removed_tasks,
                removed_bytes=removed_bytes,
                active_tasks=active_tasks,
                failed_tasks=failed_tasks,
            )
        finally:
            for directory_descriptor in (
                locks_descriptor,
                tasks_descriptor,
                root_descriptor,
            ):
                if directory_descriptor is not None:
                    os.close(directory_descriptor)

    async def run(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
        work: Callable[[ResumeSession], Awaitable[Transcript]],
    ) -> ResumeResult:
        key = self._transcripts.key(metadata, options)
        lock: FileLock | None = None
        try:
            lock = await self._acquire_lock(key)
            _ensure_private_directory(self._tasks)
            task_root = self._tasks / key
            _ensure_private_directory(task_root)
        except OSError:
            if lock is not None:
                lock.close()
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
            cached = await run_blocking(self._transcripts.load, metadata, options)
            if cached is not None:
                cleanup_ok = await run_blocking(_remove_tree, task_root)
                warning = (
                    "The completed transcript was reused, but stale temporary media "
                    "could not be removed."
                    if not cleanup_ok
                    else ""
                )
                return ResumeResult(cached, reused=True, warning=warning)

            session = _ResumeSession(task_root, persistent=True)
            transcript = await work(session)
            saved = await run_blocking(self._transcripts.save, transcript, options)
            if not saved:
                return ResumeResult(
                    transcript,
                    warning=(
                        "Local resume cache could not be saved; this transcript will "
                        "not be reusable."
                    ),
                )

            cleanup_ok = await run_blocking(_remove_tree, task_root)
            warning = (
                "The transcript was cached, but temporary media could not be removed."
                if not cleanup_ok
                else ""
            )
            return ResumeResult(transcript, warning=warning)
        finally:
            lock.close()

    async def discard_incomplete(
        self,
        metadata: SourceMetadata,
        options: TaskOptions,
    ) -> str:
        """Remove abandoned fallback media after a platform caption succeeds."""

        key = self._transcripts.key(metadata, options)
        try:
            lock = await self._acquire_lock(key)
        except OSError:
            return "Local resume temporary files could not be checked or removed."

        try:
            cleanup_ok = await run_blocking(_remove_tree, self._tasks / key)
            return (
                "Platform captions were used, but stale temporary media could not "
                "be removed."
                if not cleanup_ok
                else ""
            )
        finally:
            lock.close()

    async def _acquire_lock(self, key: str) -> FileLock:
        _ensure_private_directory(self.root)
        _ensure_private_directory(self._locks)
        path = self._locks / f"{key}.lock"
        lock = open_file_lock(path)
        try:
            while True:
                if lock.try_acquire():
                    return lock
                await asyncio.sleep(self._lock_poll_seconds)
        except BaseException:
            lock.close()
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
            if not _atomic_json(
                manifest,
                {
                    "revision": MEDIA_REVISION,
                    "purpose": purpose.value,
                    "status": "downloading",
                },
            ):
                raise RuntimeError("media resume state could not be saved")

        produced = Path(await produce(media_directory)).resolve()
        media_root = media_directory.resolve()
        if not produced.is_file() or not produced.is_relative_to(media_root):
            raise RuntimeError("media producer returned a file outside its task directory")
        if produced.stat().st_size <= 0:
            raise RuntimeError("media producer returned an empty file")

        if self._persistent:
            relative = produced.relative_to(media_root)
            if not _atomic_json(
                manifest,
                {
                    "revision": MEDIA_REVISION,
                    "purpose": purpose.value,
                    "status": "complete",
                    "path": str(relative),
                    "size": produced.stat().st_size,
                },
            ):
                raise RuntimeError("completed media resume state could not be saved")
        return produced

    async def ocr(
        self,
        sampled_frames: Sequence[SampledFrame],
        recipe: OCRRecipe,
        recognize: Callable[
            [Sequence[Path], OCRRecipe],
            Awaitable[tuple[OCRFrame, ...]],
        ],
        progress: Callable[[int, int], Awaitable[None]],
    ) -> tuple[TimedOCRFrame, ...]:
        total = len(sampled_frames)
        if not total:
            return ()

        checkpoint_root: Path | None = None
        recipe_key = _ocr_recipe_key(recipe)
        if self._persistent:
            stages_root = self._root / "stages"
            ocr_root = stages_root / "ocr"
            _ensure_private_directory(stages_root)
            _ensure_private_directory(ocr_root)
            checkpoint_root = ocr_root / recipe_key
            _ensure_private_directory(checkpoint_root)

        recognized: list[TimedOCRFrame] = []
        for start in range(0, total, recipe.batch_size):
            batch = tuple(sampled_frames[start : start + recipe.batch_size])
            await progress(start, total)
            identities: tuple[dict[str, object], ...] = ()
            checkpoint: Path | None = None
            frames: tuple[OCRFrame, ...] | None = None

            if checkpoint_root is not None:
                identities = await run_blocking(_ocr_identities, batch, start)
                batch_index = start // recipe.batch_size
                checkpoint = checkpoint_root / f"batch-{batch_index:08d}.json"
                frames = _load_ocr_checkpoint(
                    checkpoint,
                    recipe_key=recipe_key,
                    batch_index=batch_index,
                    identities=identities,
                    sampled_frames=batch,
                )

            if frames is None:
                frames = tuple(
                    await recognize(tuple(item.path for item in batch), recipe)
                )
                if len(frames) != len(batch):
                    raise RuntimeError("OCR recognizer returned an unexpected frame count")
                frames = tuple(
                    OCRFrame(
                        path=str(item.path),
                        observations=frame.observations,
                        error=frame.error,
                    )
                    for item, frame in zip(batch, frames)
                )
                if checkpoint is not None and not any(frame.error for frame in frames):
                    payload = _encode_ocr_checkpoint(
                        recipe_key=recipe_key,
                        batch_index=start // recipe.batch_size,
                        identities=identities,
                        frames=frames,
                    )
                    saved = await run_blocking(_atomic_json, checkpoint, payload)
                    if not saved:
                        raise RuntimeError("OCR resume checkpoint could not be saved")

            recognized.extend(
                TimedOCRFrame(item.timestamp_seconds, frame)
                for item, frame in zip(batch, frames)
            )
            for item in batch:
                item.path.unlink(missing_ok=True)

        return tuple(recognized)


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


def _ocr_recipe_key(recipe: OCRRecipe) -> str:
    payload = {
        "schema": OCR_CHECKPOINT_SCHEMA,
        "revision": OCR_REVISION,
        "sampling_revision": recipe.sampling_revision,
        "engine_revision": recipe.engine_revision,
        "languages": list(recipe.languages),
        "batch_size": recipe.batch_size,
        "accurate": recipe.accurate,
        "minimum_text_height": recipe.minimum_text_height,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ocr_identities(
    sampled_frames: Sequence[SampledFrame],
    start: int,
) -> tuple[dict[str, object], ...]:
    identities: list[dict[str, object]] = []
    for offset, sampled in enumerate(sampled_frames):
        timestamp = float(sampled.timestamp_seconds)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("sampled frame timestamps must be finite and non-negative")
        digest = hashlib.sha256()
        with sampled.path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        identities.append(
            {
                "ordinal": start + offset,
                "timestamp_seconds": timestamp,
                "sha256": digest.hexdigest(),
            }
        )
    return tuple(identities)


def _encode_ocr_checkpoint(
    *,
    recipe_key: str,
    batch_index: int,
    identities: Sequence[dict[str, object]],
    frames: Sequence[OCRFrame],
) -> dict[str, object]:
    return {
        "schema": OCR_CHECKPOINT_SCHEMA,
        "revision": OCR_REVISION,
        "recipe": recipe_key,
        "batch_index": batch_index,
        "frames": [
            {
                "identity": identity,
                "failed": frame.error is not None,
                "observations": [
                    {
                        "text": observation.text,
                        "confidence": observation.confidence,
                        "bounding_box": {
                            "x": observation.bounding_box.x,
                            "y": observation.bounding_box.y,
                            "width": observation.bounding_box.width,
                            "height": observation.bounding_box.height,
                        },
                    }
                    for observation in frame.observations
                ],
            }
            for identity, frame in zip(identities, frames)
        ],
    }


def _load_ocr_checkpoint(
    path: Path,
    *,
    recipe_key: str,
    batch_index: int,
    identities: Sequence[dict[str, object]],
    sampled_frames: Sequence[SampledFrame],
) -> tuple[OCRFrame, ...] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != OCR_CHECKPOINT_SCHEMA:
            return None
        if payload.get("revision") != OCR_REVISION:
            return None
        if payload.get("recipe") != recipe_key:
            return None
        if payload.get("batch_index") != batch_index:
            return None
        raw_frames = payload.get("frames")
        if not isinstance(raw_frames, list) or len(raw_frames) != len(sampled_frames):
            return None

        frames: list[OCRFrame] = []
        for raw, identity, sampled in zip(raw_frames, identities, sampled_frames):
            if not isinstance(raw, dict) or raw.get("identity") != identity:
                return None
            frames.append(_decode_ocr_frame(raw, sampled.path))
        return tuple(frames)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def _decode_ocr_frame(raw: dict[str, object], path: Path) -> OCRFrame:
    failed = raw.get("failed")
    if not isinstance(failed, bool):
        raise TypeError("cached OCR failure state must be boolean")
    if failed:
        raise ValueError("failed OCR frames are not complete checkpoints")
    raw_observations = raw["observations"]
    if not isinstance(raw_observations, list):
        raise TypeError("cached OCR observations must be a list")

    observations: list[OCRObservation] = []
    for raw_observation in raw_observations:
        if not isinstance(raw_observation, dict):
            raise TypeError("cached OCR observation must be an object")
        text = raw_observation["text"]
        confidence = _finite_number(raw_observation["confidence"])
        raw_box = raw_observation["bounding_box"]
        if not isinstance(text, str) or not text.strip() or not isinstance(raw_box, dict):
            raise TypeError("cached OCR observation is invalid")
        if not 0 <= confidence <= 1:
            raise ValueError("cached OCR confidence is invalid")
        observations.append(
            OCRObservation(
                text=text,
                confidence=confidence,
                bounding_box=BoundingBox(
                    x=_finite_number(raw_box["x"]),
                    y=_finite_number(raw_box["y"]),
                    width=_finite_number(raw_box["width"]),
                    height=_finite_number(raw_box["height"]),
                ),
            )
        )
    return OCRFrame(
        path=str(path),
        observations=tuple(observations),
        error=None,
    )


def _finite_number(value: object) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("cached OCR number is not finite")
    return number


def _flat_json_usage(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if not entry.name.endswith(".json") or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                    count += 1
                    size += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return count, size


def _task_tree_usage(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    try:
        with os.scandir(root) as entries:
            task_directories = []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        task_directories.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return 0, 0

    for task_directory in task_directories:
        count += 1
        size += _regular_tree_bytes(task_directory)
    return count, size


def _resumable_task_keys(directory_descriptor: int) -> tuple[str, ...]:
    with os.scandir(directory_descriptor) as entries:
        keys: list[str] = []
        for entry in entries:
            try:
                valid_key = len(entry.name) == 64 and all(
                    character in "0123456789abcdef" for character in entry.name
                )
                if valid_key and entry.is_dir(follow_symlinks=False):
                    keys.append(entry.name)
            except OSError:
                continue
    return tuple(sorted(keys))


def _regular_tree_bytes(root: Path) -> int:
    size = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return size


def _regular_tree_bytes_at(parent_descriptor: int, name: str) -> int:
    try:
        root_descriptor = _open_directory(name, dir_fd=parent_descriptor)
    except OSError:
        return 0

    size = 0
    pending = [root_descriptor]
    while pending:
        directory_descriptor = pending.pop()
        try:
            with os.scandir(directory_descriptor) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(entry_stat.st_mode):
                            pending.append(
                                _open_directory(
                                    entry.name,
                                    dir_fd=directory_descriptor,
                                )
                            )
                        elif stat.S_ISREG(entry_stat.st_mode):
                            size += entry_stat.st_size
                    except OSError:
                        continue
        except OSError:
            pass
        finally:
            os.close(directory_descriptor)
    return size


def _open_directory(
    path: str | Path,
    *,
    dir_fd: int | None = None,
) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags, dir_fd=dir_fd)


def _open_private_directory(parent_descriptor: int, name: str) -> int:
    try:
        descriptor = _open_directory(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        descriptor = _open_directory(name, dir_fd=parent_descriptor)
    try:
        os.fchmod(descriptor, 0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _is_directory(name: str, *, dir_fd: int) -> bool:
    try:
        return stat.S_ISDIR(
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        )
    except FileNotFoundError:
        return False


def _remove_tree_at(parent_descriptor: int, name: str) -> bool:
    try:
        shutil.rmtree(name, dir_fd=parent_descriptor)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _atomic_json(path: Path, payload: object) -> bool:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    descriptor: int | None = None
    try:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
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
