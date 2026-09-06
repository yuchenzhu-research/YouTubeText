"""Engine-neutral values used by the OCR boundary."""

from __future__ import annotations

from dataclasses import dataclass

from youtubetext.domain import TranscriptSegment


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A normalized Vision-style box whose origin is at the bottom left."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class OCRObservation:
    """One text candidate found in an image."""

    text: str
    confidence: float
    bounding_box: BoundingBox


@dataclass(frozen=True, slots=True)
class OCRFrame:
    """All OCR observations returned for one image."""

    path: str
    observations: tuple[OCRObservation, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TimedOCRFrame:
    """OCR output paired with the source video's timestamp."""

    timestamp_seconds: float
    frame: OCRFrame


# The OCR package returns the same segment value as platform captions and ASR.
# Keeping this semantic alias avoids forcing the acquisition pipeline to convert
# between identical data structures.
SubtitleSegment = TranscriptSegment
