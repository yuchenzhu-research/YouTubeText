import json
import re
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
    render_srt,
    render_text,
    render_vtt,
    safe_directory_name,
)
from youtubetext.sources.subtitles import parse_subtitle


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


def test_export_writes_seven_atomic_outputs(tmp_path):
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
    assert output.srt.name == "transcript.srt"
    assert output.vtt.name == "transcript.vtt"
    assert output.segments.name == "segments.json"
    assert {path.name for path in output.directory.iterdir()} == {
        "transcript.md",
        "transcript-clean.md",
        "transcript.txt",
        "metadata.json",
        "transcript.srt",
        "transcript.vtt",
        "segments.json",
    }
    assert [cue.text for cue in parse_subtitle(output.srt)] == ["第一句", "Second line"]
    assert [cue.text for cue in parse_subtitle(output.vtt)] == ["第一句", "Second line"]
    assert not list(output.directory.glob("*.partial"))


@pytest.mark.parametrize(("suffix", "renderer", "separator"), (
    (".srt", render_srt, ","),
    (".vtt", render_vtt, "."),
))
def test_subtitle_exports_round_trip_literal_text_and_millisecond_edges(
    tmp_path, suffix, renderer, separator
):
    segments = (
        TranscriptSegment(0, 0, "zero"),
        TranscriptSegment(0.0001, 0.0004, "submillisecond"),
        TranscriptSegment(59.9995, 60.0005, "下一分钟"),
        TranscriptSegment(
            3599.9995,
            3600.0004,
            '<b>literal</b> & &amp; <00:00:01.000> --> 中文 🎵',
        ),
        TranscriptSegment(360001.23456, 360003.99951, "one hundred hours"),
    )
    transcript = replace(sample_transcript(), segments=segments)
    content = renderer(transcript)
    path = tmp_path / f"transcript{suffix}"
    path.write_text(content, encoding="utf-8")

    cues = parse_subtitle(path)
    assert [cue.text for cue in cues] == [segment.text for segment in segments]
    assert [(cue.start_seconds, cue.end_seconds) for cue in cues] == [
        (0, 0.001),
        (0, 0.001),
        (60, 60.001),
        (3600, 3600.001),
        (360001.235, 360004),
    ]
    assert all(cue.end_seconds > cue.start_seconds for cue in cues)
    assert f"00:01:00{separator}000 --> 00:01:00{separator}001" in content
    assert f"01:00:00{separator}000 --> 01:00:00{separator}001" in content
    assert f"100:00:01{separator}235 --> 100:00:04{separator}000" in content
    assert "&lt;b&gt;literal&lt;/b&gt; &amp; &amp;amp;" in content
    assert "--&gt; 中文 🎵" in content
    assert not any("-->" in line for line in content.splitlines() if not re.match(r"\d+:\d+:\d+", line))
    if suffix == ".vtt":
        assert content.startswith("WEBVTT\n\n")


def test_segments_json_preserves_full_precision_and_confidence(tmp_path):
    transcript = replace(
        sample_transcript(),
        segments=(
            TranscriptSegment(0.0001, 0.0001, "零长度 <b> 🎵", 0.731),
            TranscriptSegment(3661.123456789, 3662.987654321, "Second & line"),
        ),
        warnings=("Possible missing interval",),
    )
    output = export_transcript(transcript, tmp_path)
    payload = json.loads(output.segments.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["metadata"]["source_id"] == transcript.metadata.source_id
    assert payload["metadata"]["webpage_url"] == transcript.metadata.webpage_url
    assert payload["language"] == "zh-Hant"
    assert payload["extraction_method"] == "apple-vision-ocr"
    assert payload["warnings"] == ["Possible missing interval"]
    assert payload["segments"] == [
        {
            "start_seconds": segment.start_seconds,
            "end_seconds": segment.end_seconds,
            "text": segment.text,
            "confidence": segment.confidence,
        }
        for segment in transcript.segments
    ]
    metadata = json.loads(output.metadata.read_text(encoding="utf-8"))
    assert "schema_version" not in metadata
    assert "segments" not in metadata
    assert metadata["segment_count"] == 2


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
    chosen = "first" if "first" in content else "second"
    assert payload["extraction_method"] == expected[chosen]
    bundle = outputs[0]
    assert chosen in bundle.clean_markdown.read_text(encoding="utf-8")
    assert bundle.text.read_text(encoding="utf-8").endswith(f"] {chosen}\n")
    assert parse_subtitle(bundle.srt)[0].text == chosen
    assert parse_subtitle(bundle.vtt)[0].text == chosen
    segments = json.loads(bundle.segments.read_text(encoding="utf-8"))
    assert segments["extraction_method"] == expected[chosen]
    assert segments["segments"][0]["text"] == chosen
    assert not list(outputs[0].directory.glob("*.partial"))


def test_both_json_exports_preserve_suspicious_gap_report(tmp_path):
    transcript = replace(
        sample_transcript(),
        metadata=replace(sample_transcript().metadata, duration_seconds=3600),
        segments=(TranscriptSegment(0, 1440, "Only the opening portion", 0.95),),
    )
    output = export_transcript(transcript, tmp_path)

    metadata = json.loads(output.metadata.read_text(encoding="utf-8"))
    segments = json.loads(output.segments.read_text(encoding="utf-8"))
    assert metadata["completeness"] == segments["completeness"]
    assert metadata["completeness"]["status"] == "needs-review"
    assert metadata["completeness"]["coverage_ratio"] == 0.4
    assert metadata["completeness"]["long_gaps"] == [
        {"start_seconds": 1440, "end_seconds": 3600},
    ]
    assert segments["segments"][0]["text"] == "Only the opening portion"


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


@pytest.mark.parametrize("failed_filename", ("transcript.srt", "transcript.vtt", "segments.json"))
def test_new_export_staging_failure_keeps_complete_previous_bundle(
    tmp_path, monkeypatch, failed_filename
):
    original = sample_transcript()
    output = export_transcript(original, tmp_path)
    previous = {path.name: path.read_bytes() for path in output.directory.iterdir()}
    original_write_text = type(output.srt).write_text

    def fail_staging_write(path, content, **kwargs):
        if path.name.startswith(f".{failed_filename}."):
            raise OSError("disk full")
        return original_write_text(path, content, **kwargs)

    monkeypatch.setattr(type(output.srt), "write_text", fail_staging_write)

    with pytest.raises(OSError, match="disk full"):
        export_transcript(
            replace(original, segments=(TranscriptSegment(0, 1, "replacement"),)),
            tmp_path,
        )

    assert {path.name: path.read_bytes() for path in output.directory.iterdir()} == previous
    assert not list(output.directory.glob("*.partial"))
