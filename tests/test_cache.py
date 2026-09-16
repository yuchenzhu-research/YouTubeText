from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

from youtubetext.cache import CACHE_SCHEMA, TranscriptCache
from youtubetext.domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.sources import YtDlpAuth


def sample_transcript() -> Transcript:
    return Transcript(
        metadata=SourceMetadata(
            "https://youtu.be/cache-test",
            "youtube",
            "cache-test",
            "Cached title",
            author="Creator",
            duration_seconds=42,
            webpage_url="https://www.youtube.com/watch?v=cache-test",
        ),
        language="zh-Hant",
        method=TranscriptMethod.APPLE_VISION_OCR,
        segments=(TranscriptSegment(1.5, 3.25, "完整字幕", 0.9),),
        warnings=("example warning",),
    )


def test_complete_transcript_round_trips_with_private_permissions(tmp_path):
    cache = TranscriptCache(tmp_path / "cache")
    transcript = sample_transcript()
    options = TaskOptions(mode=ProcessingMode.AUTO)

    assert cache.save(transcript, options) is True
    assert cache.load(transcript.metadata, options) == transcript

    files = list(cache.root.glob("*.json"))
    assert len(files) == 1
    if os.name != "nt":
        assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


def test_cache_key_ignores_output_directory_but_tracks_processing_options(tmp_path):
    cache = TranscriptCache(tmp_path / "cache")
    transcript = sample_transcript()
    original = TaskOptions(output_dir=tmp_path / "first")
    different_output = TaskOptions(output_dir=tmp_path / "second")
    different_language = TaskOptions(language="en", output_dir=tmp_path / "first")

    cache.save(transcript, original)

    assert cache.load(transcript.metadata, different_output) == transcript
    assert cache.load(transcript.metadata, different_language) is None

    for changed in (
        TaskOptions(mode=ProcessingMode.WHISPER),
        TaskOptions(whisper_model="base"),
        TaskOptions(preferred_caption_languages=("zh-Hant", "en")),
    ):
        assert cache.load(transcript.metadata, changed) is None


def test_authentication_scopes_cannot_reuse_each_others_transcripts(tmp_path):
    transcript = sample_transcript()
    options = TaskOptions()
    anonymous = TranscriptCache(tmp_path / "cache", auth_scope="anonymous")
    browser = TranscriptCache(tmp_path / "cache", auth_scope="browser:safari")

    anonymous.save(transcript, options)

    assert anonymous.load(transcript.metadata, options) == transcript
    assert browser.load(transcript.metadata, options) is None


def test_cookie_file_contents_define_separate_cache_partitions(tmp_path):
    first_file = tmp_path / "first-cookies.txt"
    second_file = tmp_path / "second-cookies.txt"
    first_file.write_text("first account", encoding="utf-8")
    second_file.write_text("second account", encoding="utf-8")
    transcript = sample_transcript()
    options = TaskOptions()
    first = TranscriptCache(
        tmp_path / "cache",
        auth_scope=YtDlpAuth(cookie_file=first_file).cache_scope(),
    )
    second = TranscriptCache(
        tmp_path / "cache",
        auth_scope=YtDlpAuth(cookie_file=second_file).cache_scope(),
    )

    first.save(transcript, options)

    assert first.load(transcript.metadata, options) == transcript
    assert second.load(transcript.metadata, options) is None


def test_bilibili_pages_with_the_same_video_id_do_not_share_cache(tmp_path):
    first_metadata = SourceMetadata(
        "https://www.bilibili.com/video/BV1cache?p=1",
        "bilibili",
        "BV1cache",
        "First page",
        webpage_url="https://www.bilibili.com/video/BV1cache/?p=1",
    )
    second_metadata = replace(
        first_metadata,
        url="https://www.bilibili.com/video/BV1cache?p=2",
        webpage_url="https://www.bilibili.com/video/BV1cache/?p=2",
    )
    transcript = replace(sample_transcript(), metadata=first_metadata)
    cache = TranscriptCache(tmp_path / "cache")

    cache.save(transcript, TaskOptions())

    assert cache.load(second_metadata, TaskOptions()) is None


def test_corrupt_or_old_cache_is_treated_as_a_miss(tmp_path):
    transcript = sample_transcript()
    options = TaskOptions()
    cache = TranscriptCache(tmp_path / "cache")
    cache.save(transcript, options)
    path = next(cache.root.glob("*.json"))

    path.write_text("{broken", encoding="utf-8")
    assert cache.load(transcript.metadata, options) is None

    path.write_text(
        json.dumps({"schema": CACHE_SCHEMA + 1, "transcript": {}}),
        encoding="utf-8",
    )
    assert cache.load(transcript.metadata, options) is None


def test_payload_with_a_different_source_identity_is_rejected(tmp_path):
    transcript = sample_transcript()
    options = TaskOptions()
    cache = TranscriptCache(tmp_path / "cache")
    cache.save(transcript, options)
    path = next(cache.root.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["transcript"]["metadata"]["source_id"] = "different-video"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load(transcript.metadata, options) is None


def test_cache_filename_contains_no_url_or_auth_identity(tmp_path):
    transcript = sample_transcript()
    cache = TranscriptCache(
        tmp_path / "cache",
        auth_scope="cookie-file:highly-sensitive-identity",
    )

    cache.save(transcript, TaskOptions())

    name = next(cache.root.glob("*.json")).name
    assert "cache-test" not in name
    assert "sensitive" not in name


def test_failed_atomic_replace_preserves_the_previous_cache(tmp_path, monkeypatch):
    cache = TranscriptCache(tmp_path / "cache")
    original = sample_transcript()
    options = TaskOptions()
    cache.save(original, options)
    updated = replace(
        original,
        segments=(TranscriptSegment(1.5, 3.25, "更新后的字幕", 0.9),),
    )
    real_replace = Path.replace

    def fail_partial_replace(path, target):
        if path.name.endswith(".partial"):
            raise OSError("simulated replace failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_partial_replace)

    assert cache.save(updated, options) is False
    assert cache.load(original.metadata, options) == original
    assert list(cache.root.glob("*.partial")) == []
