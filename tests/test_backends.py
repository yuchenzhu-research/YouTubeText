from __future__ import annotations

import pytest

from youtubetext.asr import FasterWhisperASR, MLXWhisperASR
from youtubetext.backends import default_local_backends
from youtubetext.domain import TranscriptMethod
from youtubetext.ocr import MacVisionOCR, RapidOCRBackend
from youtubetext.runtime import HostProfile


def test_apple_silicon_composes_native_local_backends() -> None:
    backends = default_local_backends(
        HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    )

    assert isinstance(backends.ocr, MacVisionOCR)
    assert isinstance(backends.asr_factory("small"), MLXWhisperASR)
    assert backends.ocr_method is TranscriptMethod.APPLE_VISION_OCR
    assert backends.asr_method is TranscriptMethod.MLX_WHISPER


def test_windows_x64_composes_lazy_open_source_backends() -> None:
    backends = default_local_backends(
        HostProfile("Windows", "AMD64", 16 * 1024**3, 8)
    )

    assert isinstance(backends.ocr, RapidOCRBackend)
    assert isinstance(backends.asr_factory("small"), FasterWhisperASR)
    assert backends.ocr_method is TranscriptMethod.RAPID_OCR
    assert backends.asr_method is TranscriptMethod.FASTER_WHISPER
    assert backends.ocr_label == "RapidOCR"
    assert backends.asr_label == "faster-whisper"


def test_unsupported_host_has_no_accidental_model_fallback() -> None:
    with pytest.raises(RuntimeError, match="Linux x86_64"):
        default_local_backends(
            HostProfile("Linux", "x86_64", 16 * 1024**3, 8)
        )
