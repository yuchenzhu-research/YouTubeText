"""Windows speech recognition backed by faster-whisper and CTranslate2."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_cache_dir

from youtubetext.domain import TranscriptSegment

from .backend import ASRResult
from .models import select_model


class FasterWhisperUnavailableError(RuntimeError):
    """Raised when the Windows speech-recognition runtime is unavailable."""


class _WhisperSegment(Protocol):
    start: float
    end: float
    text: str


class _WhisperInfo(Protocol):
    language: str


class _WhisperModel(Protocol):
    def transcribe(
        self,
        audio_path: str,
        **options: Any,
    ) -> tuple[Iterable[_WhisperSegment], _WhisperInfo]: ...


ModelFactory = Callable[[str, str, str, Path], _WhisperModel]
CudaProbe = Callable[[], bool]


class FasterWhisperASR:
    """Lazily create and reuse one local CTranslate2 Whisper model."""

    def __init__(
        self,
        model: str = "auto",
        *,
        memory_bytes: int | None = None,
        cache_root: Path | None = None,
        model_factory: ModelFactory | None = None,
        cuda_probe: CudaProbe | None = None,
    ) -> None:
        self.model_name = select_model(model, memory_bytes=memory_bytes).name
        self.cache_root = Path(
            cache_root or Path(user_cache_dir("YouTubeText")) / "faster-whisper"
        ).expanduser()
        self._model_factory = model_factory or _load_model
        self._cuda_probe = cuda_probe or _cuda_available
        self._model: _WhisperModel | None = None
        self._runtime: tuple[str, str] | None = None
        self._model_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @property
    def runtime(self) -> tuple[str, str] | None:
        """Return ``(device, compute_type)`` after first model use."""

        return self._runtime

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str = "auto",
    ) -> ASRResult:
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"audio file does not exist: {path}")

        engine_language = _whisper_language_code(language)
        with self._inference_lock:
            model = self._get_model()
            try:
                segments, detected_language = _transcribe_once(
                    model,
                    path,
                    engine_language,
                )
            except RuntimeError as cuda_error:
                if self._runtime != ("cuda", "float16") or not _is_cuda_error(
                    cuda_error
                ):
                    raise
                model = self._replace_with_cpu(cuda_error)
                try:
                    segments, detected_language = _transcribe_once(
                        model,
                        path,
                        engine_language,
                    )
                except Exception as cpu_error:
                    raise cpu_error from cuda_error

        if not segments:
            raise RuntimeError("faster-whisper returned no recognizable speech")
        resolved_language = detected_language or engine_language or "und"
        return ASRResult(
            language=resolved_language,
            model=self.model_name,
            segments=segments,
        )

    def _get_model(self) -> _WhisperModel:
        model = self._model
        if model is not None:
            return model
        with self._model_lock:
            if self._model is not None:
                return self._model
            device, compute_type = (
                ("cuda", "float16")
                if self._cuda_probe()
                else ("cpu", "int8")
            )
            try:
                model = self._model_factory(
                    self.model_name,
                    device,
                    compute_type,
                    self.cache_root,
                )
            except Exception as cuda_error:
                if device != "cuda":
                    raise
                try:
                    model = self._model_factory(
                        self.model_name,
                        "cpu",
                        "int8",
                        self.cache_root,
                    )
                except Exception as cpu_error:
                    raise cpu_error from cuda_error
                device, compute_type = "cpu", "int8"
            self._model = model
            self._runtime = (device, compute_type)
            return model

    def _replace_with_cpu(self, cuda_error: Exception) -> _WhisperModel:
        with self._model_lock:
            try:
                model = self._model_factory(
                    self.model_name,
                    "cpu",
                    "int8",
                    self.cache_root,
                )
            except Exception as cpu_error:
                raise cpu_error from cuda_error
            self._model = model
            self._runtime = ("cpu", "int8")
            return model


def _load_model(
    model_name: str,
    device: str,
    compute_type: str,
    cache_root: Path,
) -> _WhisperModel:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:  # pragma: no cover - Windows packaging failure
        raise FasterWhisperUnavailableError(
            "faster-whisper is not installed; reinstall YouTubeText on Windows"
        ) from error
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        download_root=str(cache_root),
    )


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except (ImportError, OSError, RuntimeError):
        return False


def _whisper_language_code(language: str | None) -> str | None:
    normalized = (language or "auto").strip().replace("_", "-").casefold()
    if not normalized or normalized == "auto":
        return None
    return normalized.split("-", 1)[0]


def _transcribe_once(
    model: _WhisperModel,
    path: Path,
    language: str | None,
) -> tuple[tuple[TranscriptSegment, ...], str]:
    raw_segments, info = model.transcribe(
        str(path),
        language=language,
        condition_on_previous_text=False,
        vad_filter=True,
        word_timestamps=False,
    )
    segments = _normalize_segments(raw_segments)
    detected_language = " ".join(
        str(getattr(info, "language", "") or "").split()
    )
    return segments, detected_language


def _is_cuda_error(error: BaseException) -> bool:
    detail = " ".join(str(error).casefold().split())
    return any(
        marker in detail
        for marker in (
            "cuda",
            "cudnn",
            "cublas",
            "cudart",
            "nvrtc",
            "nvidia",
            "float16",
            "gpu",
            "out of memory",
        )
    )


def _normalize_segments(
    raw_segments: Iterable[_WhisperSegment],
) -> tuple[TranscriptSegment, ...]:
    normalized: list[tuple[float, int, TranscriptSegment]] = []
    for index, raw_segment in enumerate(raw_segments):
        text = " ".join(str(getattr(raw_segment, "text", "") or "").split())
        if not text:
            continue
        try:
            start = float(getattr(raw_segment, "start"))
            end = float(getattr(raw_segment, "end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start = max(0.0, start)
        end = max(start, end)
        segment = TranscriptSegment(start, end, text)
        normalized.append((start, index, segment))
    normalized.sort(key=lambda item: (item[0], item[1]))
    return tuple(segment for _start, _index, segment in normalized)
