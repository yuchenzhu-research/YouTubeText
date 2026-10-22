"""Local inputs use the same exports without copying or deleting source files."""
import asyncio
import json
import os
import shutil
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from youtubetext.acquisition import TranscriptAcquisitionError, TranscriptPipeline
from youtubetext.asr import ASRResult
from youtubetext.cache import TranscriptCache
from youtubetext.domain import ProcessingMode, TaskOptions, TranscriptMethod, TranscriptSegment
from youtubetext.engine import YouTubeTextEngine
from youtubetext.inputs import InputSources, local_path
from youtubetext.planning import DownloadRequirement, PlanRoute, build_processing_plan
from youtubetext.resume import LocalResumeStore
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates


def metadata(_path):
    return {"format": {"duration": "10"}, "streams": [{"codec_type": "audio"}]}


class NoNetwork:
    def __getattr__(self, _name):
        raise AssertionError("local input must not contact a remote source or downloader")


def local_sources(probe=metadata):
    return InputSources(NoNetwork(), probe=probe)


def test_local_paths_file_urls_and_literal_percent_escapes_have_one_identity(tmp_path):
    path = tmp_path / "本地 clip %20.wav"
    path.write_bytes(b"read-only local input")
    sources = local_sources()

    ordinary = sources.fetch(str(path))
    file_url = sources.fetch(path.as_uri())

    assert local_path(path.as_uri()) == path
    assert ordinary == file_url
    assert ordinary.metadata.platform == "local"
    assert ordinary.metadata.media_type == "audio"
    assert ordinary.metadata.url == path.as_uri()
    assert path.read_bytes() == b"read-only local input"


def test_content_change_invalidates_identity_even_when_size_and_mtime_match(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"first")
    stat = path.stat()
    sources = local_sources()
    before = sources.fetch(str(path)).metadata
    path.write_bytes(b"other")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = sources.fetch(str(path)).metadata

    assert before.source_id != after.source_id
    assert TranscriptCache(tmp_path / "cache").key(before, TaskOptions()) != TranscriptCache(tmp_path / "cache").key(after, TaskOptions())


@pytest.mark.parametrize("value", ["file://remote-host/tmp/clip.wav", "file:///tmp/clip.wav?query", "file:///tmp/clip.wav#fragment"])
def test_local_file_urls_reject_remote_hosts_or_ambiguous_components(value):
    with pytest.raises(ValueError):
        local_path(value)


def test_remote_urls_are_left_to_existing_source_client():
    assert local_path("https://www.youtube.com/watch?v=video") is None


def test_local_inspection_advertises_but_does_not_parse_sidecar(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    path.with_suffix(".srt").write_text("not valid subtitles", encoding="utf-8")
    sources = local_sources()

    inspected = sources.inspect(str(path), preferred_languages=("zh-Hant",))

    assert inspected.subtitle.language == "zh-Hant"
    fallback = sources.fetch(str(path), strict_subtitles=False)
    assert fallback.subtitle is None
    assert fallback.warnings


def test_valid_sidecar_preserves_actual_cues(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    path.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:05,250\n本地字幕\n", encoding="utf-8")
    result = local_sources().fetch(str(path))

    assert result.subtitle.text == "本地字幕"
    assert result.subtitle.segments[0].end_seconds == 5.25


@pytest.mark.parametrize("strict", [False, True])
def test_invalid_vtt_does_not_hide_a_valid_local_srt(tmp_path, strict):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"media")
    path.with_suffix(".vtt").write_text("WEBVTT\n\n", encoding="utf-8")
    path.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:05,250\nUsable local words\n", encoding="utf-8")

    result = local_sources().fetch(str(path), strict_subtitles=strict)

    assert result.subtitle.text == "Usable local words"
    assert any("another local sidecar" in warning for warning in result.warnings)


def test_local_audio_plan_never_claims_media_download(tmp_path):
    path = tmp_path / "speech.wav"
    path.write_bytes(b"media")
    inspection = local_sources().inspect(str(path))
    plan = build_processing_plan(inspection, ProcessingMode.AUTO)

    assert plan.route is PlanRoute.WHISPER
    assert plan.media_download_requirement is DownloadRequirement.NONE
    assert plan.subtitle_download_requirement is DownloadRequirement.NONE
    assert plan.media_purpose is None
    assert not build_processing_plan(inspection, ProcessingMode.OCR).processable


def test_audio_with_cover_art_remains_an_audio_input(tmp_path):
    path = tmp_path / "album.mp3"
    path.write_bytes(b"media")
    def probe(_path):
        return {"format": {"duration": 10}, "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "disposition": {"attached_pic": 1}},
        ]}
    assert local_sources(probe).fetch(str(path)).metadata.media_type == "audio"


def test_audio_sidecar_remains_first_in_execution_plan(tmp_path):
    path = tmp_path / "speech.wav"
    path.write_bytes(b"media")
    path.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:10,000\nWords\n")
    plan = build_processing_plan(local_sources().inspect(str(path)), ProcessingMode.AUTO)
    assert plan.route is PlanRoute.CAPTIONS_THEN_OCR_THEN_WHISPER
    assert plan.processable


class Speech:
    def __init__(self):
        self.paths = []

    def transcribe(self, path, **_options):
        self.paths.append(Path(path))
        return ASRResult("en", "base", (TranscriptSegment(0, 2, "Local speech"),))


def capacity():
    return CapacityPlan.for_host(HostProfile("Darwin", "arm64", 8 * 1024**3, 8))


async def no_progress(_event):
    pass


@pytest.mark.asyncio
async def test_local_audio_auto_exports_and_resumes_without_touching_original(tmp_path):
    path = tmp_path / "speech.wav"
    content = b"original media stays byte-identical"
    path.write_bytes(content)
    speech = Speech()
    pipeline = TranscriptPipeline(
        sources=local_sources(), media=NoNetwork(), frames=NoNetwork(), ocr=NoNetwork(),
        asr_factory=lambda _model: speech, resume_store=LocalResumeStore(tmp_path / "cache"),
        temp_root=tmp_path / "temporary",
    )
    engine = YouTubeTextEngine(pipeline, capacity())
    options = TaskOptions(output_dir=tmp_path / "output")

    first = (await engine.process([str(path)], options))[0]
    second = (await engine.process([path.as_uri()], options))[0]

    assert first.succeeded and second.succeeded
    assert speech.paths == [path]
    assert path.read_bytes() == content
    assert first.transcript.method is TranscriptMethod.MLX_WHISPER
    assert first.output.srt.is_file()
    assert len(list(first.output.directory.iterdir())) == 7
    assert not any((tmp_path / "cache" / "tasks").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [
    pytest.param((" clip.wav", "clip.wav"), id="leading-spaces"),
    pytest.param(
        ("clip.wav", "clip.wav "), id="trailing-spaces",
        marks=pytest.mark.skipif(
            os.name == "nt", reason="Windows normalizes trailing spaces in filenames",
        ),
    ),
])
async def test_relative_filename_spaces_do_not_redirect_to_another_file(tmp_path, monkeypatch, names):
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    monkeypatch.chdir(tmp_path)
    speech = Speech()
    pipeline = TranscriptPipeline(
        sources=local_sources(), media=NoNetwork(), frames=NoNetwork(), ocr=NoNetwork(),
        asr_factory=lambda _model: speech, temp_root=tmp_path / "temporary",
    )
    results = await YouTubeTextEngine(pipeline, capacity()).process(
        list(names), TaskOptions(output_dir=tmp_path / "output"),
    )

    assert all(result.succeeded for result in results)
    assert {path.name for path in speech.paths} == set(names)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ProcessingMode.OCR, ProcessingMode.HYBRID])
async def test_audio_only_file_rejects_modes_requiring_video(tmp_path, mode):
    path = tmp_path / "speech.wav"
    path.write_bytes(b"unchanged")
    pipeline = TranscriptPipeline(
        sources=local_sources(), media=NoNetwork(), frames=NoNetwork(), ocr=NoNetwork(),
        asr_factory=lambda _model: Speech(), temp_root=tmp_path / "temporary",
    )
    with pytest.raises(TranscriptAcquisitionError, match="video stream"):
        await pipeline.acquire(str(path), TaskOptions(mode=mode), ResourceGates(capacity()), no_progress)
    assert path.read_bytes() == b"unchanged"


def test_real_local_wave_metadata_is_read_with_ffprobe(tmp_path):
    if shutil.which("ffprobe") is None:
        pytest.skip("FFprobe is unavailable")
    path = tmp_path / "real-silence.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 8000)

    result = InputSources(NoNetwork()).fetch(str(path))

    assert result.metadata.duration_seconds == pytest.approx(1.0)
    assert result.metadata.media_type == "audio"


def test_ocr_recipe_changes_invalidate_completed_transcript_cache(tmp_path):
    path = tmp_path / "local.wav"
    path.write_bytes(b"media")
    meta = local_sources().fetch(str(path)).metadata
    cache = TranscriptCache(tmp_path / "cache")
    options = TaskOptions()

    assert cache.key(meta, options) != cache.key(meta, replace(options, ocr_profile="accurate"))
    assert cache.key(meta, options) != cache.key(meta, replace(options, ocr_region=(0, 0, 1, 0.3)))
