import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from youtubetext.domain import (
    SourceMetadata,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.export import (
    _atomic_write,
    _atomic_write_many,
    export_transcript,
    render_clean_markdown,
    render_markdown,
    render_text,
    safe_directory_name,
)


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


def test_clean_markdown_contains_full_text_without_segment_timestamps():
    clean_markdown = render_clean_markdown(sample_transcript())

    assert "第一句\n\nSecond line" in clean_markdown
    assert "**[00:00 - 00:04]**" not in clean_markdown
    assert "Extraction: apple-vision-ocr" in clean_markdown


def test_export_writes_four_atomic_outputs(tmp_path):
    output = export_transcript(sample_transcript(), tmp_path)
    assert output.directory.name == "abc123-A title - with unsafe punctuation"
    assert output.markdown.read_text(encoding="utf-8").startswith("# A title")
    assert output.clean_markdown.name == "transcript-clean.md"
    assert output.clean_markdown.is_file()
    assert "**[" not in output.clean_markdown.read_text(encoding="utf-8")
    assert output.text.is_file()
    payload = json.loads(output.metadata.read_text(encoding="utf-8"))
    assert payload["extraction_method"] == "apple-vision-ocr"
    assert payload["segment_count"] == 2
    assert not list(output.directory.glob("*.partial"))


def test_concurrent_exports_to_same_directory_do_not_interleave(tmp_path, monkeypatch):
    from youtubetext import export as export_module

    original_write = export_module._atomic_write_many
    active = 0
    peak = 0
    count_lock = threading.Lock()
    start = threading.Barrier(2)

    def slow_write(outputs):
        nonlocal active, peak
        with count_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            original_write(outputs)
        finally:
            with count_lock:
                active -= 1

    monkeypatch.setattr(export_module, "_atomic_write_many", slow_write)
    first = replace(sample_transcript(), segments=(TranscriptSegment(0, 1, "first"),))
    second = replace(
        first,
        method=TranscriptMethod.MLX_WHISPER,
        segments=(TranscriptSegment(0, 1, "second"),),
    )

    def write(transcript):
        start.wait()
        return export_transcript(transcript, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, transcript) for transcript in (first, second)]
        outputs = [future.result() for future in futures]

    assert outputs[0].directory == outputs[1].directory
    assert peak == 1
    content = outputs[0].markdown.read_text(encoding="utf-8")
    payload = json.loads(outputs[0].metadata.read_text(encoding="utf-8"))
    expected = {
        "first": "apple-vision-ocr",
        "second": "mlx-whisper",
    }
    assert ("first" in content) != ("second" in content)
    assert payload["extraction_method"] == expected["first" if "first" in content else "second"]
    assert not list(outputs[0].directory.glob("*.partial"))


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("LPT9.", "_LPT9"),
        ("  report. ", "report"),
        ("..", "untitled"),
        ("文釗談古論今", "文釗談古論今"),
    ),
)
def test_safe_directory_name_is_portable_to_windows(value, expected):
    assert safe_directory_name(value) == expected


def test_safe_directory_name_removes_every_windows_invalid_character():
    name = safe_directory_name('Episode: <one> "draft" / \\ | ? *')

    assert name
    assert not set('<>:"/\\|?*').intersection(name)
    assert not name.endswith((" ", "."))


def test_safe_directory_name_sanitizes_reserved_fallback():
    assert safe_directory_name("...", fallback="AUX") == "_AUX"


def test_repeated_atomic_writes_leave_no_shared_partial_file(tmp_path):
    destination = tmp_path / "transcript.txt"

    _atomic_write(destination, "first")
    _atomic_write(destination, "second")

    assert destination.read_text(encoding="utf-8") == "second"
    assert not list(tmp_path.glob("*.partial"))


def test_grouped_write_failure_keeps_all_previous_outputs(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")
    original_write_text = type(first).write_text

    def fail_second_staging_write(path, content, **kwargs):
        if path.name.startswith(".second.txt."):
            raise OSError("disk full")
        return original_write_text(path, content, **kwargs)

    monkeypatch.setattr(type(first), "write_text", fail_second_staging_write)

    with pytest.raises(OSError, match="disk full"):
        _atomic_write_many(((first, "new first"), (second, "new second")))

    assert first.read_text(encoding="utf-8") == "old first"
    assert second.read_text(encoding="utf-8") == "old second"
    assert not list(tmp_path.glob("*.partial"))
