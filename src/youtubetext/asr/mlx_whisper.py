"""Apple-Silicon ASR adapter backed by ``mlx-whisper``."""
from __future__ import annotations

import math
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
HALLUCINATION_SILENCE_THRESHOLD_SECONDS = 2.0


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
    ) -> None:
        self.model: WhisperModel = select_model(model, memory_bytes=memory_bytes)
        self.cache = cache or WhisperModelCache()
        self._transcribe_callable = transcribe_callable
        self._duration_probe = duration_probe or _ffprobe_duration

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
        if duration is not None:
            options["clip_timestamps"] = [0.0, duration]

        raw = engine(str(path), **options)
        return _normalize_result(
            raw,
            self.model.name,
            engine_language,
            media_duration=duration,
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
