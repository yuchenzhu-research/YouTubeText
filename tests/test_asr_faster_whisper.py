from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from youtubetext.asr import FasterWhisperASR
from youtubetext.asr.models import GIB


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str


@dataclass
class FakeInfo:
    language: str = "zh"


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, path: str, **options: Any):
        self.calls.append((path, options))
        return iter(
            (
                FakeSegment(4.0, 5.0, " 第二句 "),
                FakeSegment(1.0, 2.5, "第一   句"),
                FakeSegment(3.0, 3.5, "  "),
            )
        ), FakeInfo()


def test_faster_whisper_is_lazy_reused_and_normalizes_segments(tmp_path: Path) -> None:
    audio = tmp_path / "speech.m4a"
    audio.touch()
    model = FakeModel()
    factory_calls: list[tuple[str, str, str, Path]] = []

    def factory(name: str, device: str, compute_type: str, cache_root: Path):
        factory_calls.append((name, device, compute_type, cache_root))
        return model

    cache = tmp_path / "models"
    backend = FasterWhisperASR(
        "small",
        cache_root=cache,
        model_factory=factory,
        cuda_probe=lambda: False,
    )
    assert backend.runtime is None

    first = backend.transcribe(audio, language="zh-Hant")
    second = backend.transcribe(audio, language="auto")

    assert factory_calls == [("small", "cpu", "int8", cache)]
    assert backend.runtime == ("cpu", "int8")
    assert first.model == "small"
    assert first.language == "zh"
    assert first.text == "第一 句\n第二句"
    assert second.text == first.text
    assert model.calls[0][1] == {
        "language": "zh",
        "condition_on_previous_text": False,
        "vad_filter": True,
        "word_timestamps": False,
    }
    assert model.calls[1][1]["language"] is None


def test_faster_whisper_prefers_cuda_and_falls_back_to_cpu(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    model = FakeModel()
    attempts: list[tuple[str, str]] = []

    def factory(_name: str, device: str, compute_type: str, _cache: Path):
        attempts.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA runtime is unavailable")
        return model

    backend = FasterWhisperASR(
        "small",
        model_factory=factory,
        cuda_probe=lambda: True,
    )

    result = backend.transcribe(audio)

    assert result.text
    assert attempts == [("cuda", "float16"), ("cpu", "int8")]
    assert backend.runtime == ("cpu", "int8")


def test_faster_whisper_retries_cuda_generator_failure_on_cpu(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    cpu_model = FakeModel()
    attempts: list[tuple[str, str]] = []

    class FailingCudaModel:
        def transcribe(self, _path: str, **_options: Any):
            def segments():
                yield FakeSegment(0, 1, "discarded partial CUDA segment")
                raise RuntimeError("cuDNN failed during CUDA inference")

            return segments(), FakeInfo()

    def factory(_name: str, device: str, compute_type: str, _cache: Path):
        attempts.append((device, compute_type))
        return FailingCudaModel() if device == "cuda" else cpu_model

    backend = FasterWhisperASR(
        "small",
        model_factory=factory,
        cuda_probe=lambda: True,
    )

    result = backend.transcribe(audio)

    assert result.text == "第一 句\n第二句"
    assert "discarded partial" not in result.text
    assert attempts == [("cuda", "float16"), ("cpu", "int8")]
    assert backend.runtime == ("cpu", "int8")


def test_faster_whisper_does_not_mask_non_cuda_runtime_errors(tmp_path: Path) -> None:
    audio = tmp_path / "broken.wav"
    audio.touch()
    attempts: list[str] = []

    class BrokenMediaModel:
        def transcribe(self, _path: str, **_options: Any):
            raise RuntimeError("invalid audio stream")

    def factory(_name: str, device: str, _compute_type: str, _cache: Path):
        attempts.append(device)
        return BrokenMediaModel()

    backend = FasterWhisperASR(
        "small",
        model_factory=factory,
        cuda_probe=lambda: True,
    )

    with pytest.raises(RuntimeError, match="invalid audio stream"):
        backend.transcribe(audio)
    assert attempts == ["cuda"]
    assert backend.runtime == ("cuda", "float16")


@pytest.mark.parametrize(
    ("language", "expected"),
    (("auto", None), ("en-US", "en"), ("zh_Hant", "zh"), ("es", "es")),
)
def test_faster_whisper_reduces_bcp47_language(
    tmp_path: Path,
    language: str,
    expected: str | None,
) -> None:
    audio = tmp_path / "speech.wav"
    audio.touch()
    model = FakeModel()
    backend = FasterWhisperASR(
        "base",
        model_factory=lambda *_args: model,
        cuda_probe=lambda: False,
    )

    backend.transcribe(audio, language=language)

    assert model.calls[0][1]["language"] == expected


def test_faster_whisper_rejects_missing_or_silent_audio(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FasterWhisperASR(
            model_factory=lambda *_args: FakeModel(),
            cuda_probe=lambda: False,
        ).transcribe(tmp_path / "missing.wav")

    class SilentModel:
        def transcribe(self, _path: str, **_options: Any):
            return iter(()), FakeInfo("")

    audio = tmp_path / "silent.wav"
    audio.touch()
    backend = FasterWhisperASR(
        model_factory=lambda *_args: SilentModel(),
        cuda_probe=lambda: False,
    )
    with pytest.raises(RuntimeError, match="no recognizable speech"):
        backend.transcribe(audio)


def test_faster_whisper_serializes_shared_model_inference(tmp_path: Path) -> None:
    class ConcurrentModel(FakeModel):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0

        def transcribe(self, path: str, **options: Any):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return super().transcribe(path, **options)

    audio = tmp_path / "speech.wav"
    audio.touch()
    model = ConcurrentModel()
    backend = FasterWhisperASR(
        "small",
        memory_bytes=8 * GIB,
        model_factory=lambda *_args: model,
        cuda_probe=lambda: False,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(backend.transcribe, audio) for _index in range(2)]
        for future in futures:
            future.result(timeout=1)

    assert model.peak == 1
