"""Guard against repeated screen labels masquerading as spoken captions."""

from youtubetext.acquisition import _ocr_is_usable
from youtubetext.domain import TranscriptSegment


def test_repeated_template_screen_labels_are_not_usable_subtitles() -> None:
    segments = tuple(
        TranscriptSegment(index * 2, index * 2 + 2, f"Slide item {index}")
        for index in range(60)
    )

    assert not _ocr_is_usable(segments, 120)


def test_distinct_spoken_caption_lines_remain_usable() -> None:
    lines = (
        "The weather changed abruptly today",
        "First responders reached the coast",
        "Officials announced a new evacuation",
        "Several roads remain closed tonight",
        "Residents are preparing for rainfall",
        "The next update arrives on Friday",
    )
    segments = tuple(
        TranscriptSegment(index * 2, index * 2 + 2, line)
        for index, line in enumerate(lines)
    )

    assert _ocr_is_usable(segments, 12)
