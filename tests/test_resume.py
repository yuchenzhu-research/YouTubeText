from __future__ import annotations

import asyncio
import os
import stat
import threading
from pathlib import Path

import pytest

import youtubetext.resume as resume_module

from youtubetext.domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.media import MediaPurpose
from youtubetext.resume import LocalResumeStore


METADATA = SourceMetadata(
    "https://www.youtube.com/watch?v=resume-test",
    "youtube",
    "resume-test",
    "Resume test video",
    author="Test creator",
    duration_seconds=90,
    webpage_url="https://www.youtube.com/watch?v=resume-test",
)
OPTIONS = TaskOptions(mode=ProcessingMode.AUTO, language="zh-Hant")
TRANSCRIPT = Transcript(
    metadata=METADATA,
    language="zh-Hant",
    method=TranscriptMethod.APPLE_VISION_OCR,
    segments=(TranscriptSegment(1.0, 3.0, "可恢復的完整字幕", 0.95),),
)


@pytest.mark.asyncio
async def test_complete_transcript_is_reused_without_repeating_work(tmp_path: Path):
    store = LocalResumeStore(tmp_path / "resume", lock_poll_seconds=0.001)
    calls = 0

    async def work(_session):
        nonlocal calls
        calls += 1
        return TRANSCRIPT

    first = await store.run(METADATA, OPTIONS, work)
    second = await store.run(METADATA, OPTIONS, work)

    assert first.transcript == TRANSCRIPT
    assert first.reused is False
    assert second.transcript == TRANSCRIPT
    assert second.reused is True
    assert calls == 1
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "resume").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "resume" / "locks").stat().st_mode) == 0o700
    transcript_files = list((tmp_path / "resume" / "transcripts").glob("*.json"))
    assert len(transcript_files) == 1
    if os.name != "nt":
        assert stat.S_IMODE(transcript_files[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_two_store_instances_singleflight_the_same_task(tmp_path: Path):
    root = tmp_path / "resume"
    first_store = LocalResumeStore(root, lock_poll_seconds=0.001)
    second_store = LocalResumeStore(root, lock_poll_seconds=0.001)
    work_started = asyncio.Event()
    allow_work_to_finish = asyncio.Event()
    calls = 0

    async def work(_session):
        nonlocal calls
        calls += 1
        work_started.set()
        await allow_work_to_finish.wait()
        return TRANSCRIPT

    first_task = asyncio.create_task(first_store.run(METADATA, OPTIONS, work))
    await asyncio.wait_for(work_started.wait(), timeout=1)
    second_task = asyncio.create_task(second_store.run(METADATA, OPTIONS, work))
    await asyncio.sleep(0.02)
    allow_work_to_finish.set()

    first, second = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=2,
    )

    assert calls == 1
    assert first.transcript == second.transcript == TRANSCRIPT
    assert {first.reused, second.reused} == {False, True}


@pytest.mark.asyncio
async def test_partial_media_continues_in_the_same_path_then_task_is_cleaned(
    tmp_path: Path,
):
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    media_directories: list[Path] = []
    partial_paths: list[Path] = []

    async def interrupted_producer(directory: Path) -> Path:
        media_directories.append(directory)
        partial = directory / "analysis-video.mp4.part"
        partial.write_bytes(b"partial-")
        partial_paths.append(partial)
        raise RuntimeError("simulated interrupted download")

    async def interrupted_work(session):
        await session.media(MediaPurpose.ANALYSIS_VIDEO, interrupted_producer)
        return TRANSCRIPT

    with pytest.raises(RuntimeError, match="interrupted download"):
        await store.run(METADATA, OPTIONS, interrupted_work)

    assert partial_paths[0].read_bytes() == b"partial-"
    task_directories = list((root / "tasks").iterdir())
    assert len(task_directories) == 1
    assert partial_paths[0].is_relative_to(task_directories[0])

    async def continued_producer(directory: Path) -> Path:
        media_directories.append(directory)
        partial = directory / "analysis-video.mp4.part"
        partial_paths.append(partial)
        assert partial.read_bytes() == b"partial-"
        partial.write_bytes(partial.read_bytes() + b"complete")
        completed = directory / "analysis-video.mp4"
        partial.replace(completed)
        return completed

    async def completed_work(session):
        media = await session.media(MediaPurpose.ANALYSIS_VIDEO, continued_producer)
        assert media.read_bytes() == b"partial-complete"
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, completed_work)

    assert result.transcript == TRANSCRIPT
    assert media_directories[0] == media_directories[1]
    assert partial_paths[0] == partial_paths[1]
    assert list((root / "tasks").iterdir()) == []


@pytest.mark.asyncio
async def test_completed_media_is_reused_after_downstream_failure(tmp_path: Path):
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    producer_calls = 0
    produced_path: Path | None = None

    async def producer(directory: Path) -> Path:
        nonlocal producer_calls, produced_path
        producer_calls += 1
        produced_path = directory / "analysis-video.mp4"
        produced_path.write_bytes(b"complete-video")
        return produced_path

    async def failing_work(session):
        media = await session.media(MediaPurpose.ANALYSIS_VIDEO, producer)
        assert media == produced_path
        raise RuntimeError("simulated OCR failure")

    with pytest.raises(RuntimeError, match="OCR failure"):
        await store.run(METADATA, OPTIONS, failing_work)

    assert producer_calls == 1
    assert produced_path is not None and produced_path.is_file()

    async def producer_must_not_run(_directory: Path) -> Path:
        raise AssertionError("completed media should have been reused")

    async def recovered_work(session):
        media = await session.media(MediaPurpose.ANALYSIS_VIDEO, producer_must_not_run)
        assert media == produced_path
        assert media.read_bytes() == b"complete-video"
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
    assert result.reused is False
    assert producer_calls == 1
    assert list((root / "tasks").iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_waits_for_cache_commit_before_releasing_lock(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "resume"
    first_store = LocalResumeStore(root, lock_poll_seconds=0.001)
    second_store = LocalResumeStore(root, lock_poll_seconds=0.001)
    save_started = threading.Event()
    allow_save = threading.Event()
    real_save = first_store._transcripts.save

    def blocking_save(transcript, options):
        save_started.set()
        assert allow_save.wait(timeout=2)
        return real_save(transcript, options)

    monkeypatch.setattr(first_store._transcripts, "save", blocking_save)
    work_calls = 0

    async def work(_session):
        nonlocal work_calls
        work_calls += 1
        return TRANSCRIPT

    first_task = asyncio.create_task(first_store.run(METADATA, OPTIONS, work))
    assert await asyncio.to_thread(save_started.wait, 1)
    first_task.cancel()
    second_task = asyncio.create_task(second_store.run(METADATA, OPTIONS, work))
    await asyncio.sleep(0.02)

    assert not second_task.done()
    assert work_calls == 1

    allow_save.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    second = await asyncio.wait_for(second_task, timeout=2)

    assert second.reused is True
    assert work_calls == 1


@pytest.mark.parametrize("invalidation", ["revision", "size"])
@pytest.mark.asyncio
async def test_invalid_completed_media_is_removed_before_redownload(
    tmp_path: Path,
    monkeypatch,
    invalidation: str,
):
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    produced_path: Path | None = None

    async def first_producer(directory: Path) -> Path:
        nonlocal produced_path
        produced_path = directory / "analysis-video.mp4"
        produced_path.write_bytes(b"old-media")
        return produced_path

    async def interrupted(session):
        await session.media(MediaPurpose.ANALYSIS_VIDEO, first_producer)
        raise RuntimeError("downstream failure")

    with pytest.raises(RuntimeError, match="downstream failure"):
        await store.run(METADATA, OPTIONS, interrupted)

    assert produced_path is not None
    if invalidation == "revision":
        monkeypatch.setattr(
            resume_module,
            "MEDIA_REVISION",
            "yt-dlp-stable-part-test-v2",
        )
    else:
        produced_path.write_bytes(b"old-media-with-a-different-size")

    producer_calls = 0

    async def replacement_producer(directory: Path) -> Path:
        nonlocal producer_calls
        producer_calls += 1
        assert list(directory.iterdir()) == []
        replacement = directory / "analysis-video.mp4"
        replacement.write_bytes(b"new-media")
        return replacement

    async def recovered(session):
        media = await session.media(
            MediaPurpose.ANALYSIS_VIDEO,
            replacement_producer,
        )
        assert media.read_bytes() == b"new-media"
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered)

    assert result.transcript == TRANSCRIPT
    assert producer_calls == 1


@pytest.mark.asyncio
async def test_storage_failure_uses_configured_ephemeral_root(tmp_path: Path):
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("not a directory", encoding="utf-8")
    temporary_root = tmp_path / "supervised-run"
    store = LocalResumeStore(blocked_root, temp_root=temporary_root)
    observed_directories: list[Path] = []

    async def producer(directory: Path) -> Path:
        observed_directories.append(directory)
        path = directory / "audio.m4a"
        path.write_bytes(b"audio")
        return path

    async def work(session):
        await session.media(MediaPurpose.AUDIO, producer)
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, work)

    assert "temporary files only" in result.warning
    assert len(observed_directories) == 1
    assert observed_directories[0].is_relative_to(temporary_root)
    assert list(temporary_root.iterdir()) == []
