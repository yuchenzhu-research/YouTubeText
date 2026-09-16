from __future__ import annotations

from youtubetext.acquisition import merge_ocr_and_asr
from youtubetext.domain import TranscriptSegment


def test_hybrid_preserves_distinct_speech_under_visible_text() -> None:
    visible = (TranscriptSegment(0, 10, "The screen shows a market chart"),)
    speech = (TranscriptSegment(0, 10, "The presenter discusses interest rates"),)

    assert merge_ocr_and_asr(visible, speech) == (*visible, *speech)


def test_hybrid_prefers_visible_caption_when_speech_matches() -> None:
    visible = (TranscriptSegment(0, 10, "Global markets are slowing."),)
    speech = (TranscriptSegment(0, 10, "global markets are slowing"),)

    assert merge_ocr_and_asr(visible, speech) == visible


def test_hybrid_does_not_repeat_a_caption_visible_across_frames() -> None:
    visible = (
        TranscriptSegment(0, 5, "Global markets are slowing"),
        TranscriptSegment(5, 10, "Global markets are slowing"),
    )
    speech = (TranscriptSegment(0, 10, "Global markets are slowing"),)

    assert merge_ocr_and_asr(visible, speech) == visible


def test_hybrid_preserves_speech_when_one_word_reverses_meaning() -> None:
    visible = (TranscriptSegment(0, 10, "The plan is approved"),)
    speech = (TranscriptSegment(0, 10, "The plan is not approved"),)

    assert merge_ocr_and_asr(visible, speech) == (*visible, *speech)


def test_hybrid_prefers_split_visible_captions_matching_one_speech_segment() -> None:
    visible = (
        TranscriptSegment(0, 5, "Global markets are slowing"),
        TranscriptSegment(5, 10, "because interest rates are rising."),
    )
    speech = (
        TranscriptSegment(
            0,
            10,
            "global markets are slowing because interest rates are rising",
        ),
    )

    assert merge_ocr_and_asr(visible, speech) == visible
