"""OCR adapters and burned-in subtitle recovery for YouTubeText."""

from .subtitles import (
    caption_from_observations,
    captions_are_similar,
    normalize_caption,
    subtitle_segments_from_frames,
)
from .types import BoundingBox, OCRFrame, OCRObservation, SubtitleSegment, TimedOCRFrame
from .vision import (
    MacVisionOCR,
    OCRExecutionError,
    OCRUnavailableError,
    find_vision_ocr_binary,
    vision_language_codes,
)

__all__ = [
    "BoundingBox",
    "MacVisionOCR",
    "OCRExecutionError",
    "OCRFrame",
    "OCRObservation",
    "OCRUnavailableError",
    "SubtitleSegment",
    "TimedOCRFrame",
    "caption_from_observations",
    "captions_are_similar",
    "find_vision_ocr_binary",
    "normalize_caption",
    "subtitle_segments_from_frames",
    "vision_language_codes",
]
