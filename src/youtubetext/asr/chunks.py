"""Bounded audio chunks and content-addressed, private ASR checkpoints."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ..domain import TranscriptSegment
from ..media import locate_ffmpeg
from ..runtime import run_blocking
from .backend import ASRResult

ASR_CHECKPOINT_SCHEMA = 1
ASR_CHUNK_REVISION = "pcm16k-overlap-single-window-timeline-v2"


@dataclass(frozen=True, slots=True)
class ASRRecipe:
    """Inputs that change decoding, model selection, or chunk boundaries."""

    model: str
    language: str
    engine_revision: str
    chunk_seconds: float = 300.0
    overlap_seconds: float = 2.0

    def __post_init__(self) -> None:
        for field in ("model", "language", "engine_revision"):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"ASR {field} must not be empty")
            object.__setattr__(self, field, value)
        length = float(self.chunk_seconds)
        overlap = float(self.overlap_seconds)
        if not math.isfinite(length) or length <= 0:
            raise ValueError("ASR chunk_seconds must be finite and positive")
        if not math.isfinite(overlap) or not 0 <= overlap < length:
            raise ValueError("ASR overlap_seconds must be non-negative and smaller than a chunk")
        object.__setattr__(self, "chunk_seconds", length)
        object.__setattr__(self, "overlap_seconds", overlap)


@dataclass(frozen=True, slots=True)
class _ChunkResult:
    language: str
    model: str
    segments: tuple[TranscriptSegment, ...]


async def transcribe_chunks(
    media_path: Path,
    recipe: ASRRecipe,
    transcribe: Callable[[Path], ASRResult],
    progress: Callable[[int, int], Awaitable[None]],
    *,
    task_root: Path,
    persistent: bool,
) -> ASRResult:
    """Recognize one chunk at a time, committing only complete chunk results.

    Unknown-duration inputs go directly to the supplied engine. No duration is
    inferred from source metadata: only the actual local media can define safe
    checkpoint boundaries. Cancellation waits for blocking decoding/inference
    before deleting that worker's input.
    """

    media_path = Path(media_path)
    duration = await run_blocking(_probe_duration, media_path)
    if duration is None:
        await progress(0, 1)
        result = await run_blocking(transcribe, media_path)
        await progress(1, 1)
        return result

    windows = _chunk_windows(duration, recipe)
    checkpoint_root: Path | None = None
    identity = ""
    if persistent:
        identity = await run_blocking(_identity, media_path, recipe, duration)
        checkpoint_root = task_root / "stages" / "asr" / identity
        for directory in (task_root, task_root / "stages", task_root / "stages" / "asr", checkpoint_root):
            await run_blocking(_private_directory, directory)

    results: list[_ChunkResult] = []
    await progress(0, len(windows))
    for index, (start, end) in enumerate(windows):
        checkpoint = (
            checkpoint_root / f"chunk-{index:08d}.json"
            if checkpoint_root is not None else None
        )
        result = None
        if checkpoint is not None:
            result = await run_blocking(
                _load_checkpoint, checkpoint, identity, index, start, end,
            )
        if result is None:
            # Every known-duration window uses the same timeline-preserving
            # preparation, including short videos. ASR decoders commonly drop
            # a delayed audio stream's leading offset when given its container
            # directly, shifting short-video speech toward time zero.
            await run_blocking(_private_directory, task_root)
            with tempfile.TemporaryDirectory(prefix="asr-chunk-", dir=task_root) as temporary:
                chunk_path = Path(temporary) / "audio.wav"
                await run_blocking(_extract_chunk, media_path, chunk_path, start, end)
                result = await run_blocking(
                    _recognize, transcribe, chunk_path, recipe, start, end,
                )
            if checkpoint is not None:
                await run_blocking(
                    _save_checkpoint, checkpoint, identity, index, start, end, result,
                )
        results.append(result)
        await progress(index + 1, len(windows))

    segments: list[TranscriptSegment] = []
    for (start, _end), result in zip(windows, results):
        # Compare only across chunk boundaries. Identical repeated words within
        # one engine result are never collapsed, nor are different/negated texts.
        previous = (
            tuple(segment for segment in segments if segment.end_seconds >= start)
            if recipe.overlap_seconds > 0 else ()
        )
        segments.extend(
            segment for segment in result.segments
            if not any(_same_boundary_cue(segment, old) for old in previous)
        )
    if not segments:
        raise RuntimeError("Whisper returned no recognizable speech")
    segments.sort(key=lambda segment: (segment.start_seconds, segment.end_seconds))
    first_speech = next(result for result in results if result.segments)
    return ASRResult(first_speech.language, first_speech.model, tuple(segments))


def _chunk_windows(duration: float, recipe: ASRRecipe) -> tuple[tuple[float, float], ...]:
    if duration <= recipe.chunk_seconds:
        return ((0.0, duration),)
    stride = recipe.chunk_seconds - recipe.overlap_seconds
    extra_chunks = (duration - recipe.chunk_seconds) / stride
    if not math.isfinite(extra_chunks) or extra_chunks >= 1_000_000:
        raise ValueError("ASR recipe would create too many chunks")
    count = math.ceil(extra_chunks) + 1
    return tuple(
        (index * stride, min(duration, index * stride + recipe.chunk_seconds))
        for index in range(count)
    )


def _probe_duration(path: Path) -> float | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if completed.returncode != 0:
            return None
        duration = float(completed.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _extract_chunk(source: Path, target: Path, start: float, end: float) -> None:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    length = end - start
    try:
        completed = subprocess.run(
            [str(locate_ffmpeg()), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-ss", f"{start:.9f}", "-i", str(source), "-t", f"{length:.9f}",
             "-map", "0:a:0", "-vn", "-af",
             f"aresample=async=1:first_pts=0,apad=whole_dur={length:.9f}",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)],
            capture_output=True, text=True, check=False,
            timeout=max(60.0, min(600.0, length * 2)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Whisper audio chunk could not be prepared") from exc
    if completed.returncode or not target.is_file() or target.stat().st_size <= 44:
        raise RuntimeError("Whisper audio chunk could not be prepared")
    target.chmod(0o600)


def _recognize(
    transcribe: Callable[[Path], ASRResult],
    path: Path,
    recipe: ASRRecipe,
    start: float,
    end: float,
) -> _ChunkResult:
    try:
        result = transcribe(path)
    except RuntimeError as exc:
        # Existing adapters use these exact errors for valid silent chunks.
        # Other backend failures remain failures and are never checkpointed.
        if str(exc) not in {
            "mlx-whisper returned no recognizable speech",
            "faster-whisper returned no recognizable speech",
        }:
            raise
        return _ChunkResult("und" if recipe.language == "auto" else recipe.language, recipe.model, ())
    segments: list[TranscriptSegment] = []
    for segment in result.segments:
        local_start = min(end - start, segment.start_seconds)
        local_end = min(end - start, segment.end_seconds)
        if segment.start_seconds >= end - start:
            continue
        segments.append(
            TranscriptSegment(
                start + local_start, start + local_end, segment.text, segment.confidence,
            )
        )
    return _ChunkResult(result.language, result.model, tuple(segments))


def _same_boundary_cue(left: TranscriptSegment, right: TranscriptSegment) -> bool:
    if left.text != right.text:
        return False
    shorter = min(left.end_seconds - left.start_seconds, right.end_seconds - right.start_seconds)
    if shorter <= 0:
        return left.start_seconds == right.start_seconds and left.end_seconds == right.end_seconds
    overlap = min(left.end_seconds, right.end_seconds) - max(left.start_seconds, right.start_seconds)
    # Conservative exact-text matching tolerates small clock jitter without
    # substring/fuzzy matching that could erase negation or another repetition.
    midpoint_delta = abs(
        (left.start_seconds + left.end_seconds - right.start_seconds - right.end_seconds) / 2
    )
    return overlap >= shorter * 0.8 and midpoint_delta <= max(0.2, shorter * 0.2)


def _identity(path: Path, recipe: ASRRecipe, duration: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    payload = {
        "revision": ASR_CHUNK_REVISION,
        "media_sha256": digest.hexdigest(),
        "recipe": asdict(recipe),
        "duration_seconds": duration,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _save_checkpoint(
    path: Path,
    identity: str,
    index: int,
    start: float,
    end: float,
    result: _ChunkResult,
) -> None:
    payload = {
        "schema": ASR_CHECKPOINT_SCHEMA,
        "revision": ASR_CHUNK_REVISION,
        "identity": identity,
        "chunk_index": index,
        "start_seconds": start,
        "end_seconds": end,
        "language": result.language,
        "model": result.model,
        "segments": [asdict(segment) for segment in result.segments],
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("Whisper resume checkpoint could not be saved") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _load_checkpoint(
    path: Path,
    identity: str,
    index: int,
    start: float,
    end: float,
) -> _ChunkResult | None:
    try:
        if path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or any((
            payload.get("schema") != ASR_CHECKPOINT_SCHEMA,
            payload.get("revision") != ASR_CHUNK_REVISION,
            payload.get("identity") != identity,
            payload.get("chunk_index") != index,
            payload.get("start_seconds") != start,
            payload.get("end_seconds") != end,
        )):
            return None
        language, model = payload["language"], payload["model"]
        if not isinstance(language, str) or not language or not isinstance(model, str) or not model:
            return None
        raw_segments = payload["segments"]
        if not isinstance(raw_segments, list):
            return None
        segments = tuple(TranscriptSegment(**segment) for segment in raw_segments)
        if any(segment.start_seconds < start or segment.end_seconds > end for segment in segments):
            return None
        if any(current.start_seconds < previous.start_seconds for previous, current in zip(segments, segments[1:])):
            return None
        return _ChunkResult(language, model, segments)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return None
