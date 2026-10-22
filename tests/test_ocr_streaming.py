"""Fixed-density OCR sampling, bounded image lifetimes, and global resumes."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import aclosing
from pathlib import Path

import pytest

from youtubetext.acquisition import TranscriptPipeline
from youtubetext.domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.media import FrameSampler, SampledFrame
from youtubetext.ocr import BoundingBox, OCRFrame, OCRObservation
from youtubetext.resume import EphemeralResumeStore, LocalResumeStore
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates


METADATA = SourceMetadata(
    "https://www.youtube.com/watch?v=streaming",
    "youtube", "streaming", "Streaming subtitle test", duration_seconds=120,
)


def gates():
    return ResourceGates(CapacityPlan.for_host(HostProfile("Darwin", "arm64", 8 * 1024**3, 8)))


async def no_progress(_event):
    pass


class StreamingFrames:
    def __init__(self, values, *, window=32):
        self.values = values
        self.window = window
        self.arguments = []
        self.peak_files = 0
        self.closed = False

    async def iter_batches(self, _video, output, _duration, **arguments):
        self.arguments.append(arguments)
        interval = {"fast": 1, "balanced": 0.5, "accurate": 0.25}[arguments["profile"]]
        previous = ()
        try:
            for start in range(0, len(self.values), self.window):
                assert all(not item.path.exists() for item in previous)
                current = []
                for index in range(start, min(start + self.window, len(self.values))):
                    path = output / f"frame_{index:012d}.jpg"
                    path.write_text(self.values[index], encoding="utf-8")
                    current.append(SampledFrame(path, index * interval))
                previous = tuple(current)
                self.peak_files = max(self.peak_files, len(tuple(output.glob("*.jpg"))))
                yield previous
        finally:
            self.closed = True
            for item in previous:
                item.path.unlink(missing_ok=True)


class TextOCR:
    def __init__(self, *, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call

    async def recognize_images_async(self, paths, **_arguments):
        self.calls.append(tuple(Path(path).read_text(encoding="utf-8") for path in paths))
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("simulated interrupted OCR")
        return tuple(
            OCRFrame(
                str(path),
                (
                    OCRObservation(text, 0.95, BoundingBox(0.1, 0.2, 0.8, 0.1)),
                ) if text else (),
            )
            for path, text in zip(paths, self.calls[-1])
        )


async def read_stream(tmp_path, frames, ocr, *, store=None, options=None, store_options=None):
    options = options or TaskOptions(mode=ProcessingMode.OCR)
    pipeline = TranscriptPipeline(
        frames=frames,
        ocr=ocr,
        asr_factory=lambda _model: None,
        temp_root=tmp_path,
    )
    frame_root = tmp_path / "images"
    frame_root.mkdir(parents=True, exist_ok=True)
    store = store or EphemeralResumeStore(tmp_path / "temporary")

    async def work(session):
        segments, warnings = await pipeline._read_burned_captions(
            METADATA.url, METADATA, tmp_path / "source.mp4", frame_root,
            options, gates(), no_progress, session,
        )
        return Transcript(
            METADATA, "en", TranscriptMethod.APPLE_VISION_OCR,
            segments or (TranscriptSegment(0, 0.5, "No subtitle"),), warnings,
        )

    return await store.run(METADATA, store_options or options, work)


@pytest.mark.asyncio
async def test_exact_duplicate_crops_reuse_ocr_but_keep_all_timeline_positions(tmp_path):
    frames = StreamingFrames(("First caption", "First caption", "First caption", "", "", "First caption", "Final caption", "Final caption"), window=3)
    ocr = TextOCR()

    result = await read_stream(tmp_path, frames, ocr)

    assert sum(len(call) for call in ocr.calls) == 3
    assert [segment.text for segment in result.transcript.segments] == [
        "First caption", "First caption", "Final caption",
    ]
    assert result.transcript.segments[0].end_seconds == 1.5
    assert result.transcript.segments[-1].end_seconds == 4
    assert frames.peak_files <= 3
    assert frames.closed
    assert not tuple((tmp_path / "images").glob("*.jpg"))


@pytest.mark.asyncio
async def test_profile_and_region_reach_sampler_and_subtitle_timestamps(tmp_path):
    frames = StreamingFrames(tuple(f"Caption {index}" for index in range(5)), window=2)
    options = TaskOptions(mode=ProcessingMode.OCR, ocr_profile="accurate", ocr_region=(0.1, 0.1, 0.7, 0.2))

    result = await read_stream(tmp_path, frames, TextOCR(), options=options)

    assert frames.arguments == [{
        "profile": "accurate", "region": (0.1, 0.1, 0.7, 0.2), "batch_size": 32,
    }]
    assert result.transcript.segments[-1].end_seconds == 1.25


@pytest.mark.asyncio
async def test_streamed_resume_uses_global_ordinals_across_partial_windows(tmp_path):
    resume_root = tmp_path / "resume"
    values = tuple(f"Caption number {index:03d}" for index in range(8))
    first_frames = StreamingFrames(values, window=3)
    first_ocr = TextOCR(fail_on_call=3)
    store = LocalResumeStore(resume_root, lock_poll_seconds=0.001)

    with pytest.raises(RuntimeError, match="interrupted OCR"):
        await read_stream(tmp_path / "first", first_frames, first_ocr, store=store)

    checkpoints = tuple((resume_root / "tasks").glob("*/stages/ocr/*/batch-*.json"))
    assert len(checkpoints) == 2
    saved_ordinals = sorted(
        frame["identity"]["ordinal"]
        for path in checkpoints
        for frame in json.loads(path.read_text())["frames"]
    )
    assert saved_ordinals == list(range(6))
    assert first_frames.closed

    second_ocr = TextOCR()
    result = await read_stream(
        tmp_path / "second", StreamingFrames(values, window=3), second_ocr, store=store,
    )

    assert second_ocr.calls == [(values[6], values[7])]
    assert result.transcript.segments[-1].end_seconds == 4
    assert not tuple((resume_root / "tasks").iterdir())


@pytest.mark.asyncio
async def test_stream_close_runs_when_ocr_recognition_fails(tmp_path):
    frames = StreamingFrames(tuple(f"Caption {index}" for index in range(40)))

    with pytest.raises(RuntimeError, match="interrupted OCR"):
        await read_stream(tmp_path, frames, TextOCR(fail_on_call=1))

    assert frames.closed
    assert not tuple((tmp_path / "images").glob("*.jpg"))


@pytest.mark.asyncio
async def test_cancellation_stops_blocked_ffmpeg_before_releasing_frame_files(tmp_path, monkeypatch):
    stdout = asyncio.StreamReader()
    stdout.feed_data(b"\xff\xd8\x01\x02\xff\xd9" * 32)
    stderr = asyncio.StreamReader()
    stopped = asyncio.Event()
    ocr_started = asyncio.Event()

    class Process:
        returncode = None

        async def wait(self):
            await stopped.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            stdout.feed_eof()
            stderr.feed_eof()
            stopped.set()

    process = Process()
    process.stdout, process.stderr = stdout, stderr

    async def create(*_arguments, **_options):
        return process

    class BlockedOCR:
        async def recognize_images_async(self, _paths, **_options):
            ocr_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("youtubetext.media.asyncio.create_subprocess_exec", create)
    task = asyncio.create_task(read_stream(tmp_path, FrameSampler(tmp_path / "ffmpeg"), BlockedOCR()))
    await asyncio.wait_for(ocr_started.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    assert stopped.is_set()
    assert process.returncode == -15
    assert not tuple((tmp_path / "images").glob("*.jpg"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["profile", "region"])
async def test_changed_profile_or_region_invalidates_streaming_ocr_checkpoints(tmp_path, change):
    store = LocalResumeStore(tmp_path / "resume", lock_poll_seconds=0.001)
    values = tuple(f"Distinct subtitle {index}" for index in range(5))
    original = TaskOptions(mode=ProcessingMode.OCR)
    with pytest.raises(RuntimeError, match="interrupted OCR"):
        await read_stream(
            tmp_path / "first", StreamingFrames(values, window=3), TextOCR(fail_on_call=2),
            store=store, options=original,
        )
    changed = TaskOptions(
        mode=ProcessingMode.OCR,
        ocr_profile="accurate" if change == "profile" else "balanced",
        ocr_region=(0, 0, 1, 0.5) if change == "region" else original.ocr_region,
    )
    ocr = TextOCR()

    await read_stream(
        tmp_path / "second", StreamingFrames(values, window=3), ocr,
        store=store, options=changed, store_options=original,
    )

    assert tuple(text for call in ocr.calls for text in call) == values


def ffmpeg_binary():
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is unavailable")
    return executable


def make_subtitle_video(tmp_path, *, extra_caption=None):
    """Create actual raster subtitle frames without fonts or image libraries."""
    executable = ffmpeg_binary()
    glyphs = {
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    }
    width, height = 160, 100
    for index in range(24):
        timestamp = index / 8
        pixels = bytearray(width * height * 3)
        # A colored strip at the top makes top-left ROI selection observable.
        for y in range(20):
            for x in range(width):
                offset = (y * width + x) * 3
                pixels[offset:offset + 3] = b"\xff\x00\x00"
        letter = "A" if 0.5 <= timestamp < 0.875 else "B" if 1.25 <= timestamp < 1.875 else None
        if letter:
            for row, bits in enumerate(glyphs[letter]):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(3):
                            for dx in range(3):
                                x, y = 70 + column * 3 + dx, 70 + row * 3 + dy
                                offset = (y * width + x) * 3
                                pixels[offset:offset + 3] = b"\xff\xff\xff"
        if extra_caption:
            origin_x, origin_y = (2, 2) if extra_caption == "top" else (142, 60)
            for row, bits in enumerate(glyphs["A"]):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(2):
                            for dx in range(2):
                                x, y = origin_x + column * 2 + dx, origin_y + row * 2 + dy
                                offset = (y * width + x) * 3
                                pixels[offset:offset + 3] = b"\xff\xff\xff"
        (tmp_path / f"source-{index:03d}.ppm").write_bytes(
            f"P6\n{width} {height}\n255\n".encode() + pixels,
        )
    video = tmp_path / "subtitle-windows.mkv"
    subprocess.run(
        [executable, "-y", "-nostdin", "-v", "error", "-framerate", "8", "-i", str(tmp_path / "source-%03d.ppm"), "-c:v", "ffv1", str(video)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, check=True,
    )
    for path in tmp_path.glob("source-*.ppm"):
        path.unlink()
    return video


def frame_brightness(path):
    output = subprocess.run(
        [ffmpeg_binary(), "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True,
    ).stdout
    return max(output), sum(output) / len(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile, expected_count, expected_step", [("fast", 3, 1), ("balanced", 6, 0.5), ("accurate", 12, 0.25)])
async def test_real_subtitle_windows_keep_profile_density_despite_two_hour_metadata(tmp_path, profile, expected_count, expected_step):
    video = make_subtitle_video(tmp_path)
    sampler = FrameSampler(Path(ffmpeg_binary()), max_frames=1)
    output = tmp_path / "frames"
    timestamps, lit = [], []

    async with aclosing(sampler.iter_batches(video, output, 7200, profile=profile, batch_size=2)) as batches:
        async for batch in batches:
            assert len(tuple(output.glob("*.jpg"))) <= 2
            for frame in batch:
                timestamps.append(frame.timestamp_seconds)
                lit.append(frame_brightness(frame.path)[0] > 200)

    assert timestamps == [index * expected_step for index in range(expected_count)]
    if profile in {"balanced", "accurate"}:
        assert any(lit)
        assert lit[timestamps.index(0.5)]
        assert lit[timestamps.index(1.5)]
    assert not tuple(output.glob("*.jpg"))


@pytest.mark.asyncio
async def test_real_top_left_roi_changes_crop_location(tmp_path):
    video = make_subtitle_video(tmp_path)
    sampler = FrameSampler(Path(ffmpeg_binary()))
    output = tmp_path / "frames"

    async with aclosing(sampler.iter_batches(video, output, 3, region=(0, 0, 1, 0.2), batch_size=1)) as batches:
        first = await anext(batches)
        maximum, average = frame_brightness(first[0].path)

    assert maximum > 50
    assert average > 50
    assert not tuple(output.glob("*.jpg"))


@pytest.mark.asyncio
@pytest.mark.parametrize("location, region", [("top", (0, 0, 0.3, 0.3)), ("side", (0.8, 0.5, 0.2, 0.5))])
async def test_explicit_roi_preserves_top_and_edge_caption_candidates(tmp_path, location, region):
    video = make_subtitle_video(tmp_path, extra_caption=location)

    class RasterOCR:
        async def recognize_images_async(self, paths, **_options):
            result = []
            for path in paths:
                white_pixels = frame_brightness(path)[0] > 200
                observations = (
                    OCRObservation("Visible selected subtitle", 0.95, BoundingBox(0.01, 0.9, 0.15, 0.08)),
                ) if white_pixels else ()
                result.append(OCRFrame(str(path), observations))
            return tuple(result)

    class RealFrames:
        def iter_batches(self, _video, output, duration, **options):
            return FrameSampler(Path(ffmpeg_binary())).iter_batches(video, output, duration, **options)

    result = await read_stream(
        tmp_path, RealFrames(), RasterOCR(),
        options=TaskOptions(mode=ProcessingMode.OCR, ocr_region=region),
    )

    assert result.transcript.text == "Visible selected subtitle"
    assert result.transcript.segments[-1].end_seconds == 3


@pytest.mark.asyncio
async def test_default_roi_retains_lower_center_candidate_filtering(tmp_path):
    class UpperEdgeOCR(TextOCR):
        async def recognize_images_async(self, paths, **_options):
            return tuple(
                OCRFrame(str(path), (OCRObservation("Screen label", 0.95, BoundingBox(0.01, 0.9, 0.15, 0.08)),))
                for path in paths
            )

    result = await read_stream(tmp_path, StreamingFrames(("image", "image")), UpperEdgeOCR())

    assert result.transcript.text == "No subtitle"


@pytest.mark.asyncio
async def test_streaming_sampler_crosses_old_frame_limit_with_bounded_files(tmp_path, monkeypatch):
    stdout = asyncio.StreamReader()
    stdout.feed_data(b"\xff\xd8\x01\x02\xff\xd9" * 2405)
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()

    class Process:
        returncode = 0

        async def wait(self):
            return self.returncode

    process = Process()
    process.stdout, process.stderr = stdout, stderr

    async def create(*_arguments, **_options):
        return process

    monkeypatch.setattr("youtubetext.media.asyncio.create_subprocess_exec", create)
    output = tmp_path / "frames"
    sampler = FrameSampler(tmp_path / "ffmpeg", max_frames=1)
    timestamps = []

    async with aclosing(sampler.iter_batches(tmp_path / "video.mp4", output, 7200, batch_size=999)) as batches:
        async for batch in batches:
            assert len(batch) <= 32
            assert len(tuple(output.glob("*.jpg"))) <= 32
            timestamps.extend(frame.timestamp_seconds for frame in batch)

    assert len(timestamps) == 2405
    assert timestamps[-1] == 1202
    assert all(current - previous == 0.5 for previous, current in zip(timestamps, timestamps[1:]))
    assert not tuple(output.glob("*.jpg"))


@pytest.mark.asyncio
async def test_real_sampler_close_removes_current_window_and_stops_process(tmp_path, monkeypatch):
    video = make_subtitle_video(tmp_path)
    processes = []
    create = asyncio.create_subprocess_exec

    async def capture(*arguments, **options):
        process = await create(*arguments, **options)
        processes.append(process)
        return process

    monkeypatch.setattr("youtubetext.media.asyncio.create_subprocess_exec", capture)
    output = tmp_path / "frames"
    sampler = FrameSampler(Path(ffmpeg_binary()))
    iterator = sampler.iter_batches(video, output, 3, profile="accurate", batch_size=2)
    await anext(iterator)

    await asyncio.wait_for(iterator.aclose(), 10)

    assert processes[0].returncode is not None
    assert not tuple(output.glob("*.jpg"))


@pytest.mark.asyncio
async def test_invalid_media_error_cleans_up_streaming_process(tmp_path, monkeypatch):
    media = tmp_path / "invalid.mp4"
    media.write_bytes(b"not a media file")
    output = tmp_path / "frames"

    with pytest.raises(RuntimeError, match="FFmpeg frame sampling failed"):
        async with aclosing(FrameSampler(Path(ffmpeg_binary())).iter_batches(media, output, 7200)) as batches:
            async for _batch in batches:
                pass

    assert not tuple(output.glob("*.jpg"))
