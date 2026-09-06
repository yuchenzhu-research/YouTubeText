"""Public values and errors for the source acquisition module."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from youtubetext.domain import SourceMetadata, TranscriptSegment


class SubtitleKind(str, Enum):
    """How a platform supplied a subtitle track."""

    MANUAL = "manual"
    AUTOMATIC = "automatic"


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """A selected and cleaned platform subtitle track."""

    language: str
    kind: SubtitleKind
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        language = str(self.language or "").strip()
        if not language:
            raise ValueError("subtitle language must not be empty")
        if not self.segments:
            raise ValueError("a subtitle track must contain at least one segment")
        object.__setattr__(self, "language", language)

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Normalized source metadata and its best available subtitle track."""

    metadata: SourceMetadata
    subtitle: SubtitleTrack | None = None


class SourceError(RuntimeError):
    """Base error raised at the source module's interface."""


class UnsupportedSourceError(SourceError):
    """The URL does not belong to a supported video platform."""

    def __init__(self, url: str) -> None:
        super().__init__(
            "unsupported source URL; expected youtube.com, youtu.be, "
            "bilibili.com, or b23.tv"
        )
        self.url = url


class SourceFetchError(SourceError):
    """yt-dlp or subtitle parsing failed after a platform was recognized."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"source {stage} failed: {message}")
        self.stage = stage
