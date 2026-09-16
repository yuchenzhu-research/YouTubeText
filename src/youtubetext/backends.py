"""Host-specific local model composition behind one small pipeline seam."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .asr import ASRBackend, FasterWhisperASR, MLXWhisperASR
from .domain import TranscriptMethod
from .ocr import (
    MacVisionOCR,
    OCRFrame,
    RapidOCRBackend,
    rapid_language_codes,
    vision_language_codes,
)
from .runtime import HostKind, HostProfile, detect_host


class OCRProvider(Protocol):
    @property
    def checkpoint_revision(self) -> str: ...

    async def recognize_images_async(
        self,
        image_paths: Sequence[str | Path],
        *,
        languages: Sequence[str] = (),
        accurate: bool = True,
        minimum_text_height: float = 0.012,
    ) -> tuple[OCRFrame, ...]: ...


ASRFactory = Callable[[str], ASRBackend]
OCRLanguageResolver = Callable[[str | None], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class LocalBackends:
    ocr: OCRProvider
    asr_factory: ASRFactory
    ocr_label: str
    asr_label: str
    ocr_method: TranscriptMethod
    asr_method: TranscriptMethod
    ocr_language_codes: OCRLanguageResolver = vision_language_codes


def default_local_backends(host: HostProfile | None = None) -> LocalBackends:
    """Compose lazy local-model adapters for one supported host."""

    profile = host or detect_host()
    if profile.kind is HostKind.APPLE_SILICON:
        return LocalBackends(
            ocr=MacVisionOCR(),
            asr_factory=_mlx_whisper,
            ocr_label="Apple Vision OCR",
            asr_label="MLX Whisper",
            ocr_method=TranscriptMethod.APPLE_VISION_OCR,
            asr_method=TranscriptMethod.MLX_WHISPER,
        )
    if profile.kind is HostKind.WINDOWS_X64:
        return LocalBackends(
            ocr=RapidOCRBackend(),
            asr_factory=_faster_whisper,
            ocr_label="RapidOCR",
            asr_label="faster-whisper",
            ocr_method=TranscriptMethod.RAPID_OCR,
            asr_method=TranscriptMethod.FASTER_WHISPER,
            ocr_language_codes=rapid_language_codes,
        )
    raise RuntimeError(
        "YouTubeText has no local model backends for "
        f"{profile.system} {profile.machine}"
    )


def _mlx_whisper(model: str) -> ASRBackend:
    return MLXWhisperASR(model)


def _faster_whisper(model: str) -> ASRBackend:
    return FasterWhisperASR(model)
