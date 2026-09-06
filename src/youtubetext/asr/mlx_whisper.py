"""Apple-Silicon ASR adapter backed by ``mlx-whisper``."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from youtubetext.domain import TranscriptSegment

from .backend import ASRResult
from .models import WhisperModel, WhisperModelCache, select_model

TranscribeCallable = Callable[..., Mapping[str, Any]]
DurationProbe = Callable[[Path], float | None]
TrailingSilenceProbe = Callable[[Path, float], float | None]
HALLUCINATION_SILENCE_THRESHOLD_SECONDS = 2.0
TRAILING_SILENCE_MINIMUM_SECONDS = 2.0
TRAILING_SILENCE_END_TOLERANCE_SECONDS = 0.5
TRAILING_SILENCE_PADDING_SECONDS = 0.25


class MLXWhisperASR:
    """Lazily download and run one MLX Whisper model.

    Passing ``transcribe_callable`` replaces the external engine.  In that
    mode the adapter neither imports MLX nor prepares model weights, making the
    entire normalization path deterministic and offline-testable.
    """

    def __init__(
        self,
        model: str = "auto",
        *,
        memory_bytes: int | None = None,
        cache: WhisperModelCache | None = None,
        transcribe_callable: TranscribeCallable | None = None,
        duration_probe: DurationProbe | None = None,
        trailing_silence_probe: TrailingSilenceProbe | None = None,
    ) -> None:
        self.model: WhisperModel = select_model(model, memory_bytes=memory_bytes)
        self.cache = cache or WhisperModelCache()
        self._transcribe_callable = transcribe_callable
        self._duration_probe = duration_probe or _ffprobe_duration
        self._trailing_silence_probe = trailing_silence_probe or _ffmpeg_trailing_silence

    @property
    def model_cached(self) -> bool:
        if self._transcribe_callable is not None:
            # The injected engine is deliberately independent of local model
            # state; reporting a host cache here would make offline tests and
            # alternative adapters environment-dependent.
            return False
        return self.cache.is_cached(self.model)

    def transcribe(self, audio_path: str | Path, *, language: str = "auto") -> ASRResult:
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"audio file does not exist: {path}")

        requested_language = (language or "auto").strip()
        engine_language = None if requested_language.lower() == "auto" else requested_language
        duration = _safe_duration(self._duration_probe, path)
        clip_end = _effective_clip_end(
            self._trailing_silence_probe,
            path,
            duration,
        )

        if self._transcribe_callable is None:
            model_reference = str(self.cache.ensure(self.model))
            engine = _mlx_transcribe
        else:
            # An injected engine is an explicit test/adapter seam.  Giving it
            # the immutable repository ID avoids any disk or network activity.
            model_reference = self.model.repository
            engine = self._transcribe_callable

        options: dict[str, Any] = {
            "path_or_hf_repo": model_reference,
            "language": engine_language,
            "condition_on_previous_text": False,
            "word_timestamps": True,
            "hallucination_silence_threshold": HALLUCINATION_SILENCE_THRESHOLD_SECONDS,
            "verbose": None,
        }
        if clip_end is not None:
            options["clip_timestamps"] = [0.0, clip_end]

        raw = engine(str(path), **options)
        return _normalize_result(
            raw,
            self.model.name,
            engine_language,
            media_duration=clip_end,
        )


def _mlx_transcribe(audio_path: str, **kwargs: Any) -> Mapping[str, Any]:
    try:
        import mlx_whisper
    except ImportError as exc:  # pragma: no cover - packaging failure path
        raise RuntimeError(
            "mlx-whisper is required and only supported on Apple Silicon macOS"
        ) from exc
    return mlx_whisper.transcribe(audio_path, **kwargs)


def _normalize_result(
    raw: Mapping[str, Any],
    model_name: str,
    requested_language: str | None,
    *,
    media_duration: float | None = None,
) -> ASRResult:
    if not isinstance(raw, Mapping):
        raise TypeError("mlx-whisper returned a non-mapping result")

    normalized: list[tuple[float, float, int, str]] = []
    raw_segments = raw.get("segments") or ()
    had_timestamped_segments = bool(raw_segments)
    for index, segment in enumerate(raw_segments):
        if not isinstance(segment, Mapping):
            continue
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        try:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start = max(0.0, start)
        if media_duration is not None and start >= media_duration:
            continue
        end = max(start, end)
        if media_duration is not None:
            end = min(end, media_duration)
        normalized.append((start, end, index, text))

    if not normalized and not had_timestamped_segments:
        text = " ".join(str(raw.get("text") or "").split())
        if text:
            normalized.append((0.0, media_duration or 0.0, 0, text))
    if not normalized:
        raise RuntimeError("mlx-whisper returned no recognizable speech")

    # Some engines return chunk completion order.  Time plus original index is
    # a stable key even when two segments start at exactly the same instant.
    normalized.sort(key=lambda item: (item[0], item[2]))
    segments = tuple(
        TranscriptSegment(start_seconds=start, end_seconds=end, text=text)
        for start, end, _, text in normalized
    )
    detected_language = " ".join(str(raw.get("language") or "").split())
    language = detected_language or requested_language or "und"
    return ASRResult(language=language, model=model_name, segments=segments)


def _safe_duration(probe: DurationProbe, path: Path) -> float | None:
    try:
        duration = probe(path)
        if duration is None:
            return None
        value = float(duration)
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _ffprobe_duration(path: Path) -> float | None:
    """Read media duration locally; an unavailable/broken ffprobe is non-fatal."""

    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            return None
        value = float(completed.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _effective_clip_end(
    probe: TrailingSilenceProbe,
    path: Path,
    duration: float | None,
) -> float | None:
    if duration is None:
        return None
    try:
        silence_start = probe(path, duration)
        if silence_start is None:
            return duration
        start = float(silence_start)
    except Exception:
        return duration
    if not math.isfinite(start) or start < 0 or start >= duration:
        return duration
    if duration - start < TRAILING_SILENCE_MINIMUM_SECONDS:
        return duration
    return min(duration, start + TRAILING_SILENCE_PADDING_SECONDS)


def _ffmpeg_trailing_silence(path: Path, duration: float) -> float | None:
    """Return the start of a long silence that reaches the end of the media."""

    executable = shutil.which("ffmpeg")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-nostats",
                "-nostdin",
                "-i",
                str(path),
                "-vn",
                "-af",
                f"silencedetect=noise=-45dB:d={TRAILING_SILENCE_MINIMUM_SECONDS:g}",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    active_start: float | None = None
    trailing: tuple[float, float] | None = None
    for line in (completed.stderr or "").splitlines():
        start_match = re.search(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", line)
        if start_match:
            active_start = float(start_match.group(1))
        end_match = re.search(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", line)
        if end_match and active_start is not None:
            trailing = (active_start, float(end_match.group(1)))
            active_start = None

    if trailing is None:
        return None
    start, end = trailing
    if end < duration - TRAILING_SILENCE_END_TOLERANCE_SECONDS:
        return None
    if end - start < TRAILING_SILENCE_MINIMUM_SECONDS:
        return None
    return start
