from __future__ import annotations

import os
import stat
from collections.abc import Sequence
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
from youtubetext.media import SampledFrame
from youtubetext.ocr import BoundingBox, OCRFrame, OCRObservation, TimedOCRFrame
from youtubetext.resume import LocalResumeStore, OCRRecipe


METADATA = SourceMetadata(
    "https://www.youtube.com/watch?v=ocr-resume-test",
    "youtube",
    "ocr-resume-test",
    "OCR resume test video",
    duration_seconds=90,
)
OPTIONS = TaskOptions(mode=ProcessingMode.OCR, language="zh-Hant")
TRANSCRIPT = Transcript(
    metadata=METADATA,
    language="zh-Hant",
    method=TranscriptMethod.APPLE_VISION_OCR,
    segments=(TranscriptSegment(0.0, 1.0, "可恢復的 OCR 字幕", 0.95),),
)
RECIPE = OCRRecipe(
    languages=("zh-Hant",),
    sampling_revision="test-sampler-v1",
    batch_size=32,
    accurate=True,
    minimum_text_height=0.012,
)


def make_sampled_frames(
    directory: Path,
    count: int,
    *,
    changed_content_at: int | None = None,
) -> tuple[SampledFrame, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    sampled: list[SampledFrame] = []
    for index in range(count):
        path = directory / f"temporary-frame-{index:03d}.jpg"
        # Keep the byte length identical so this detects content hashing rather
        # than an implementation that only compares file sizes.
        marker = "modified" if index == changed_content_at else "baseline"
        path.write_bytes(f"{marker}-frame-content-{index:03d}".encode())
        sampled.append(SampledFrame(path, index * 1.25))
    return tuple(sampled)


def recognized_frames(
    paths: Sequence[Path],
    *,
    with_observations: bool = True,
) -> tuple[OCRFrame, ...]:
    frames: list[OCRFrame] = []
    for index, path in enumerate(paths):
        observations = ()
        if with_observations:
            observations = (
                OCRObservation(
                    f"字幕 {index}",
                    0.95,
                    BoundingBox(0.1, 0.2, 0.8, 0.08),
                ),
            )
        frames.append(OCRFrame(str(path), observations))
    return tuple(frames)


async def no_progress(_completed: int, _total: int) -> None:
    return None


def checkpoint_json_files(root: Path) -> tuple[Path, ...]:
    files = tuple(sorted((root / "tasks").glob("*/stages/ocr/*/batch-*.json")))
    assert files, "the interrupted OCR run should leave at least one checkpoint"
    return files


@pytest.mark.asyncio
async def test_ocr_resume_reuses_completed_batch_after_later_batch_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    first_sample = make_sampled_frames(tmp_path / "first-sampling", 33)
    retry_sample = make_sampled_frames(tmp_path / "retry-sampling", 33)
    submitted_batches: list[tuple[Path, ...]] = []
    fail_second_batch = True

    async def recognize(
        paths: Sequence[Path], active_recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        nonlocal fail_second_batch
        assert active_recipe == RECIPE
        submitted_batches.append(tuple(paths))
        if len(paths) == 1 and fail_second_batch:
            fail_second_batch = False
            raise RuntimeError("second OCR batch was interrupted")
        return recognized_frames(paths)

    async def interrupted_work(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, recognize, no_progress)
        return TRANSCRIPT

    with pytest.raises(RuntimeError, match="second OCR batch was interrupted"):
        await store.run(METADATA, OPTIONS, interrupted_work)

    recovered: tuple[TimedOCRFrame, ...] = ()

    async def recovered_work(session) -> Transcript:
        nonlocal recovered
        recovered = await session.ocr(retry_sample, RECIPE, recognize, no_progress)
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert submitted_batches == [
        tuple(item.path for item in first_sample[:32]),
        tuple(item.path for item in first_sample[32:]),
        tuple(item.path for item in retry_sample[32:]),
    ]
    assert result.transcript == TRANSCRIPT
    assert result.reused is False
    assert [item.timestamp_seconds for item in recovered] == [
        item.timestamp_seconds for item in retry_sample
    ]
    assert [item.frame.path for item in recovered] == [
        str(item.path) for item in retry_sample
    ]
    assert len(recovered) == 33


@pytest.mark.asyncio
async def test_ocr_resume_caches_a_batch_with_no_observations(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    first_sample = make_sampled_frames(tmp_path / "first-sampling", 2)
    retry_sample = make_sampled_frames(tmp_path / "retry-sampling", 2)
    recognize_calls = 0

    async def recognize_empty(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        nonlocal recognize_calls
        recognize_calls += 1
        return recognized_frames(paths, with_observations=False)

    async def fail_after_ocr(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, recognize_empty, no_progress)
        raise RuntimeError("downstream processing failed")

    with pytest.raises(RuntimeError, match="downstream processing failed"):
        await store.run(METADATA, OPTIONS, fail_after_ocr)

    async def recognize_must_not_run(
        _paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        raise AssertionError("an empty OCR result is still a cache hit")

    recovered: tuple[TimedOCRFrame, ...] = ()

    async def recovered_work(session) -> Transcript:
        nonlocal recovered
        recovered = await session.ocr(
            retry_sample,
            RECIPE,
            recognize_must_not_run,
            no_progress,
        )
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
    assert recognize_calls == 1
    assert [item.frame.observations for item in recovered] == [(), ()]
    assert [item.frame.path for item in recovered] == [
        str(item.path) for item in retry_sample
    ]


@pytest.mark.asyncio
async def test_ocr_resume_retries_batches_that_contain_frame_errors(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    first_sample = make_sampled_frames(tmp_path / "first-sampling", 2)

    async def recognize_with_error(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        frames = list(recognized_frames(paths))
        frames[0] = OCRFrame(str(paths[0]), (), error="temporary Vision failure")
        return tuple(frames)

    async def fail_after_ocr(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, recognize_with_error, no_progress)
        raise RuntimeError("OCR quality check failed")

    with pytest.raises(RuntimeError, match="quality check failed"):
        await store.run(METADATA, OPTIONS, fail_after_ocr)

    assert tuple((root / "tasks").glob("*/stages/ocr/*/batch-*.json")) == ()
    retry_sample = make_sampled_frames(tmp_path / "retry-sampling", 2)
    retry_calls = 0

    async def recognize_successfully(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        nonlocal retry_calls
        retry_calls += 1
        return recognized_frames(paths)

    async def recovered_work(session) -> Transcript:
        await session.ocr(
            retry_sample,
            RECIPE,
            recognize_successfully,
            no_progress,
        )
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
    assert retry_calls == 1


@pytest.mark.asyncio
async def test_ocr_frames_are_not_deleted_when_checkpoint_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    sampled = make_sampled_frames(tmp_path / "sampling", 2)
    monkeypatch.setattr(resume_module, "_atomic_json", lambda _path, _payload: False)

    async def recognize(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        return recognized_frames(paths)

    async def work(session) -> Transcript:
        await session.ocr(sampled, RECIPE, recognize, no_progress)
        return TRANSCRIPT

    with pytest.raises(RuntimeError, match="checkpoint could not be saved"):
        await store.run(METADATA, OPTIONS, work)

    assert all(item.path.is_file() for item in sampled)


@pytest.mark.parametrize(
    "invalidation",
    ["language", "sampling_revision", "engine_revision", "content"],
)
@pytest.mark.asyncio
async def test_ocr_resume_misses_when_recipe_or_frame_content_changes(
    tmp_path: Path,
    invalidation: str,
) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    first_sample = make_sampled_frames(tmp_path / "first-sampling", 2)
    retry_sample = make_sampled_frames(
        tmp_path / "retry-sampling",
        2,
        changed_content_at=0 if invalidation == "content" else None,
    )

    async def initial_recognize(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        return recognized_frames(paths)

    async def fail_after_ocr(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, initial_recognize, no_progress)
        raise RuntimeError("downstream processing failed")

    with pytest.raises(RuntimeError, match="downstream processing failed"):
        await store.run(METADATA, OPTIONS, fail_after_ocr)

    retry_recipe = RECIPE
    if invalidation == "language":
        retry_recipe = OCRRecipe(
            languages=("en-US",),
            sampling_revision=RECIPE.sampling_revision,
            batch_size=RECIPE.batch_size,
            accurate=RECIPE.accurate,
            minimum_text_height=RECIPE.minimum_text_height,
        )
    elif invalidation == "sampling_revision":
        retry_recipe = OCRRecipe(
            languages=RECIPE.languages,
            sampling_revision="test-sampler-v2",
            batch_size=RECIPE.batch_size,
            accurate=RECIPE.accurate,
            minimum_text_height=RECIPE.minimum_text_height,
        )
    elif invalidation == "engine_revision":
        retry_recipe = OCRRecipe(
            languages=RECIPE.languages,
            sampling_revision=RECIPE.sampling_revision,
            engine_revision="test-vision-engine-v2",
            batch_size=RECIPE.batch_size,
            accurate=RECIPE.accurate,
            minimum_text_height=RECIPE.minimum_text_height,
        )

    submitted: list[tuple[Path, ...]] = []

    async def replacement_recognize(
        paths: Sequence[Path], active_recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        assert active_recipe == retry_recipe
        submitted.append(tuple(paths))
        return recognized_frames(paths)

    async def recovered_work(session) -> Transcript:
        await session.ocr(
            retry_sample,
            retry_recipe,
            replacement_recognize,
            no_progress,
        )
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
    assert submitted == [tuple(item.path for item in retry_sample)]


@pytest.mark.asyncio
async def test_ocr_resume_ignores_corrupt_checkpoint_json(tmp_path: Path) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    first_sample = make_sampled_frames(tmp_path / "first-sampling", 2)

    async def initial_recognize(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        return recognized_frames(paths)

    async def fail_after_ocr(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, initial_recognize, no_progress)
        raise RuntimeError("downstream processing failed")

    with pytest.raises(RuntimeError, match="downstream processing failed"):
        await store.run(METADATA, OPTIONS, fail_after_ocr)

    for checkpoint in checkpoint_json_files(root):
        checkpoint.write_text("{not valid JSON", encoding="utf-8")

    retry_sample = make_sampled_frames(tmp_path / "retry-sampling", 2)
    recognize_calls = 0

    async def replacement_recognize(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        nonlocal recognize_calls
        recognize_calls += 1
        return recognized_frames(paths)

    async def recovered_work(session) -> Transcript:
        await session.ocr(
            retry_sample,
            RECIPE,
            replacement_recognize,
            no_progress,
        )
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
    assert recognize_calls == 1


@pytest.mark.asyncio
async def test_ocr_checkpoint_contains_no_temporary_paths_or_cookie_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resume"
    store = LocalResumeStore(root, lock_poll_seconds=0.001)
    cookie_secret = "session_cookie=private-token-8675309"
    first_sample = make_sampled_frames(
        tmp_path / f"sampling-{cookie_secret}",
        2,
    )

    async def recognize(
        paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        return recognized_frames(paths)

    async def fail_after_ocr(session) -> Transcript:
        await session.ocr(first_sample, RECIPE, recognize, no_progress)
        raise RuntimeError("downstream processing failed")

    with pytest.raises(RuntimeError, match="downstream processing failed"):
        await store.run(METADATA, OPTIONS, fail_after_ocr)

    serialized = "\n".join(
        checkpoint.read_text(encoding="utf-8")
        for checkpoint in checkpoint_json_files(root)
    )
    checkpoints = checkpoint_json_files(root)
    if os.name != "nt":
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in checkpoints)
        assert stat.S_IMODE(checkpoints[0].parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(checkpoints[0].parent.parent.stat().st_mode) == 0o700
    assert cookie_secret not in serialized
    for item in first_sample:
        assert str(item.path) not in serialized
        assert item.path.name not in serialized

    retry_sample = make_sampled_frames(tmp_path / "retry-sampling", 2)

    async def recognize_must_not_run(
        _paths: Sequence[Path], _recipe: OCRRecipe
    ) -> tuple[OCRFrame, ...]:
        raise AssertionError("path-free checkpoint should remain reusable")

    async def recovered_work(session) -> Transcript:
        recovered = await session.ocr(
            retry_sample,
            RECIPE,
            recognize_must_not_run,
            no_progress,
        )
        assert [item.frame.path for item in recovered] == [
            str(item.path) for item in retry_sample
        ]
        return TRANSCRIPT

    result = await store.run(METADATA, OPTIONS, recovered_work)

    assert result.transcript == TRANSCRIPT
