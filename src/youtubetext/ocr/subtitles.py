"""Pure policies for recovering stable subtitles from timed OCR frames."""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Sequence

from youtubetext.domain import TranscriptSegment

from .types import OCRObservation, TimedOCRFrame

_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
DEFAULT_SIMILARITY_THRESHOLD = 0.84


def normalize_caption(text: str) -> str:
    """Normalize spacing and punctuation for similarity comparisons."""

    return _NORMALIZE_RE.sub("", text.casefold())


def captions_are_similar(
    left: str,
    right: str,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """Return whether two noisy OCR strings represent the same subtitle."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    first = normalize_caption(left)
    second = normalize_caption(right)
    if not first or not second:
        return False
    if first == second:
        return True
    if first in second or second in first:
        shorter, longer = sorted((len(first), len(second)))
        return shorter >= 4 and shorter / longer >= 0.72
    return difflib.SequenceMatcher(None, first, second, autojunk=False).ratio() >= threshold


def caption_from_observations(
    observations: Sequence[OCRObservation],
    *,
    minimum_confidence: float = 0.3,
    minimum_height: float = 0.018,
    maximum_y: float = 0.76,
    center_left: float = 0.3,
    center_right: float = 0.7,
) -> tuple[str, float]:
    """Select and order likely caption lines from a lower-video crop."""

    candidates = [
        observation
        for observation in observations
        if observation.text.strip()
        and observation.confidence >= minimum_confidence
        and observation.bounding_box.height >= minimum_height
        and observation.bounding_box.y <= maximum_y
        and observation.bounding_box.x <= center_right
        and observation.bounding_box.x + observation.bounding_box.width >= center_left
        and len(normalize_caption(observation.text)) >= 2
    ]
    candidates.sort(key=lambda item: (-item.bounding_box.y, item.bounding_box.x))
    if not candidates:
        return "", 0.0

    text = " ".join(item.text.strip() for item in candidates)
    confidence = sum(item.confidence for item in candidates) / len(candidates)
    return text, confidence


def subtitle_segments_from_frames(
    frames: Sequence[TimedOCRFrame],
    *,
    frame_duration_seconds: float,
    blank_tolerance_seconds: float | None = None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> tuple[TranscriptSegment, ...]:
    """Collapse chronologically ordered frame OCR into timed subtitle segments.

    The function has no filesystem or model dependency, so caption selection and
    de-duplication can be tested independently from Apple Vision.
    """

    if frame_duration_seconds <= 0:
        raise ValueError("frame_duration_seconds must be greater than zero")
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0 and 1")
    tolerance = (
        frame_duration_seconds * 1.5
        if blank_tolerance_seconds is None
        else blank_tolerance_seconds
    )
    if tolerance < 0:
        raise ValueError("blank_tolerance_seconds must not be negative")

    ordered = sorted(frames, key=lambda item: item.timestamp_seconds)
    segments: list[TranscriptSegment] = []
    active_text = ""
    active_confidence = 0.0
    active_start = 0.0
    last_seen = 0.0

    def flush(end_seconds: float | None = None) -> None:
        nonlocal active_text, active_confidence, active_start, last_seen
        if active_text:
            natural_end = last_seen + frame_duration_seconds
            end = natural_end if end_seconds is None else min(natural_end, end_seconds)
            segments.append(
                TranscriptSegment(
                    start_seconds=active_start,
                    end_seconds=max(active_start + frame_duration_seconds, end),
                    text=active_text,
                    confidence=active_confidence,
                )
            )
        active_text = ""
        active_confidence = 0.0

    for timed_frame in ordered:
        timestamp = float(timed_frame.timestamp_seconds)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("frame timestamps must be finite and non-negative")
        text, confidence = caption_from_observations(timed_frame.frame.observations)
        if not text:
            if active_text and timestamp - last_seen > tolerance:
                flush()
            continue

        if active_text and captions_are_similar(
            active_text,
            text,
            threshold=similarity_threshold,
        ):
            last_seen = timestamp
            if (
                confidence > active_confidence
                or len(normalize_caption(text)) > len(normalize_caption(active_text))
            ):
                active_text = text
                active_confidence = confidence
            continue

        flush(end_seconds=timestamp)
        active_text = text
        active_confidence = confidence
        active_start = timestamp
        last_seen = timestamp

    flush()
    return tuple(segments)
