from __future__ import annotations

import pytest

from youtubetext.domain import TranscriptSegment
from youtubetext.ocr import (
    BoundingBox,
    OCRFrame,
    OCRObservation,
    TimedOCRFrame,
    caption_from_observations,
    captions_are_similar,
    subtitle_segments_from_frames,
)


def observation(
    text: str,
    *,
    confidence: float = 0.9,
    x: float = 0.1,
    y: float = 0.2,
    height: float = 0.08,
) -> OCRObservation:
    return OCRObservation(text, confidence, BoundingBox(x, y, 0.5, height))


def timed(seconds: float, *observations: OCRObservation) -> TimedOCRFrame:
    return TimedOCRFrame(seconds, OCRFrame(f"frame-{seconds}.jpg", tuple(observations)))


def test_caption_selection_filters_noise_and_orders_two_lines() -> None:
    text, confidence = caption_from_observations(
        [
            observation("角标", y=0.9),
            observation("右侧台标", x=0.86, y=0.2, confidence=1.0),
            observation("第二行", x=0.2, y=0.2, confidence=0.8),
            observation("第一行", x=0.2, y=0.4, confidence=0.3),
            observation("x", height=0.01),
        ]
    )

    assert text == "第一行 第二行"
    assert confidence == pytest.approx(0.55)


def test_caption_similarity_tolerates_spacing_punctuation_and_ocr_suffix() -> None:
    assert captions_are_similar(
        "美国的关税政策，正在变化", "美国的关税政策正在变化。"
    )
    assert captions_are_similar("这是一个测试字幕", "这是一个测试字幕啊")
    assert not captions_are_similar("关税政策发生变化", "今天北京天气晴朗")


def test_frames_collapse_to_timestamped_segments_and_keep_best_text() -> None:
    frames = [
        timed(0.0, observation("第一句字幕", confidence=0.6)),
        timed(1.0, observation("第一句字幕", confidence=0.96)),
        timed(2.0),
        timed(3.0, observation("第二句字幕", confidence=0.91)),
        timed(4.0, observation("第二句字幕", confidence=0.88)),
    ]

    segments = subtitle_segments_from_frames(frames, frame_duration_seconds=1.0)

    assert [(item.start_seconds, item.end_seconds, item.text) for item in segments] == [
        (0.0, 2.0, "第一句字幕"),
        (3.0, 5.0, "第二句字幕"),
    ]
    assert segments[0].confidence == pytest.approx(0.96)
    assert isinstance(segments[0], TranscriptSegment)


def test_two_blank_frames_close_an_active_segment() -> None:
    frames = [
        timed(0.0, observation("短暂字幕")),
        timed(1.0),
        timed(2.0),
        timed(5.0, observation("后续字幕")),
    ]

    segments = subtitle_segments_from_frames(frames, frame_duration_seconds=1.0)

    assert segments[0].end_seconds == 1.0
    assert segments[1].start_seconds == 5.0


def test_invalid_timing_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        subtitle_segments_from_frames([], frame_duration_seconds=0)
    with pytest.raises(ValueError):
        subtitle_segments_from_frames(
            [], frame_duration_seconds=1, blank_tolerance_seconds=-1
        )
