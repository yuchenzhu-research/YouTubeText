"""Stable domain values shared by the CLI, acquisition and export modules."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ProcessingMode(str, Enum):
    AUTO = "auto"
    CAPTIONS = "captions"
    OCR = "ocr"
    WHISPER = "whisper"
    HYBRID = "hybrid"


class TranscriptMethod(str, Enum):
    PLATFORM_CAPTIONS = "platform-captions"
    APPLE_VISION_OCR = "apple-vision-ocr"
    MLX_WHISPER = "mlx-whisper"
    OCR_WHISPER = "ocr-whisper"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    url: str
    platform: str
    source_id: str
    title: str
    author: str = ""
    duration_seconds: float = 0.0
    webpage_url: str = ""

    def __post_init__(self) -> None:
        if self.platform not in {"youtube", "bilibili"}:
            raise ValueError(f"unsupported platform: {self.platform}")
        if not self.url.strip():
            raise ValueError("source URL must not be empty")
        if not self.title.strip():
            object.__setattr__(self, "title", self.source_id or "untitled")
        duration = float(self.duration_seconds or 0.0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration must be a finite non-negative number")
        object.__setattr__(self, "duration_seconds", duration)


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        start = float(self.start_seconds)
        end = float(self.end_seconds)
        text = " ".join(str(self.text or "").split())
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("segment timestamps must be finite")
        if start < 0 or end < start:
            raise ValueError("segment timestamps are out of order")
        if not text:
            raise ValueError("segment text must not be empty")
        confidence = self.confidence
        if confidence is not None:
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
        object.__setattr__(self, "start_seconds", start)
        object.__setattr__(self, "end_seconds", end)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "confidence", confidence)


@dataclass(frozen=True, slots=True)
class Transcript:
    metadata: SourceMetadata
    language: str
    method: TranscriptMethod
    segments: tuple[TranscriptSegment, ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a transcript must contain at least one segment")
        previous = -1.0
        for segment in self.segments:
            if segment.start_seconds < previous:
                raise ValueError("transcript segments must be sorted by start time")
            previous = segment.start_seconds

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)


@dataclass(frozen=True, slots=True)
class TaskOptions:
    mode: ProcessingMode = ProcessingMode.AUTO
    language: str = "auto"
    preferred_caption_languages: tuple[str, ...] = ()
    whisper_model: str = "auto"
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "YouTubeText-output")

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser())


@dataclass(frozen=True, slots=True)
class OutputFiles:
    directory: Path
    markdown: Path
    text: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class TaskResult:
    url: str
    transcript: Transcript | None = None
    output: OutputFiles | None = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.transcript is not None and self.output is not None and not self.error

