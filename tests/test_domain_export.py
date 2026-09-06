import json

import pytest

from youtubetext.domain import (
    SourceMetadata,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.export import export_transcript, render_markdown, render_text


def sample_transcript() -> Transcript:
    return Transcript(
        metadata=SourceMetadata(
            url="https://youtu.be/abc123",
            webpage_url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            source_id="abc123",
            title="A title / with unsafe punctuation",
            author="Author",
            duration_seconds=75,
        ),
        language="zh-Hant",
        method=TranscriptMethod.APPLE_VISION_OCR,
        segments=(
            TranscriptSegment(0, 4.2, "第一句"),
            TranscriptSegment(65, 70, "Second line"),
        ),
    )


def test_transcript_rejects_unsorted_segments():
    with pytest.raises(ValueError, match="sorted"):
        Transcript(
            metadata=sample_transcript().metadata,
            language="en",
            method=TranscriptMethod.MLX_WHISPER,
            segments=(TranscriptSegment(5, 6, "later"), TranscriptSegment(1, 2, "earlier")),
        )


def test_render_formats_are_timestamped():
    transcript = sample_transcript()
    markdown = render_markdown(transcript)
    text = render_text(transcript)
    assert "**[00:00 - 00:04]**" in markdown
    assert "[01:05 - 01:10] Second line" in text
    assert "Extraction: apple-vision-ocr" in markdown


def test_export_writes_three_atomic_outputs(tmp_path):
    output = export_transcript(sample_transcript(), tmp_path)
    assert output.directory.name == "abc123-A title - with unsafe punctuation"
    assert output.markdown.read_text(encoding="utf-8").startswith("# A title")
    assert output.text.is_file()
    payload = json.loads(output.metadata.read_text(encoding="utf-8"))
    assert payload["extraction_method"] == "apple-vision-ocr"
    assert payload["segment_count"] == 2
    assert not list(output.directory.glob("*.partial"))
