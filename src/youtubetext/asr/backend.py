"""Small, replaceable speech-to-text contract.

ASR turns audio into timestamped text.  It deliberately has no summarization
API: summarization, if ever added, belongs to a separate text-processing stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from youtubetext.domain import TranscriptSegment


@dataclass(frozen=True, slots=True)
class ASRResult:
    """Normalized speech-recognition output independent of an ASR vendor."""

    language: str
    model: str
    segments: tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("ASR result must contain at least one segment")
        if any(
            current.start_seconds < previous.start_seconds
            for previous, current in zip(self.segments, self.segments[1:])
        ):
            raise ValueError("ASR segments must be sorted by start time")

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def timestamped_text(self) -> str:
        return "\n".join(
            f"[{_timestamp(segment.start_seconds)} --> "
            f"{_timestamp(segment.end_seconds)}] {segment.text}"
            for segment in self.segments
        )


class ASRBackend(Protocol):
    """The complete interface the acquisition pipeline needs from ASR."""

    def transcribe(self, audio_path: str | Path, *, language: str = "auto") -> ASRResult:
        """Transcribe a local audio file; implementations may block."""


def _timestamp(seconds: float) -> str:
    total_milliseconds = round(max(0.0, seconds) * 1000)
    total_seconds, milliseconds = divmod(total_milliseconds, 1000)
    minutes, second = divmod(total_seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}.{milliseconds:03d}"
