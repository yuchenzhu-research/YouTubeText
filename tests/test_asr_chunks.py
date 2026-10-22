from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import struct
import subprocess
import threading
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from youtubetext.asr import ASRResult
from youtubetext.asr import chunks
from youtubetext.domain import TranscriptSegment
from youtubetext.resume import ASRRecipe, _ResumeSession


RECIPE = ASRRecipe("small", "en", "test-engine-v1", 3, 1)


async def no_progress(_completed: int, _total: int) -> None:
    pass


def result(*segments: tuple[float, float, str]) -> ASRResult:
    return ASRResult("en", "small", tuple(TranscriptSegment(*segment) for segment in segments))


@pytest.fixture
def fake_media(monkeypatch, tmp_path):
    source = tmp_path / "private-source-token.wav"
    source.write_bytes(b"complete source media")
    monkeypatch.setattr(chunks, "_probe_duration", lambda _path: 7.0)
    extracted: list[tuple[float, float]] = []

    def extract(_source: Path, target: Path, start: float, end: float) -> None:
        assert list(target.parent.parent.glob("asr-chunk-*/*.wav")) == []
        extracted.append((start, end))
        target.write_text(str(start), encoding="utf-8")

    monkeypatch.setattr(chunks, "_extract_chunk", extract)
    return source, extracted


@pytest.mark.parametrize("changes", [
    {"model": ""}, {"language": " "}, {"engine_revision": ""},
    {"chunk_seconds": 0}, {"chunk_seconds": float("nan")},
    {"overlap_seconds": -1}, {"overlap_seconds": 3}, {"overlap_seconds": float("inf")},
])
def test_recipe_rejects_unsafe_boundaries(changes) -> None:
    with pytest.raises(ValueError):
        replace(RECIPE, **changes)


@pytest.mark.asyncio
async def test_unknown_duration_uses_original_input_without_checkpoint_or_slicing(
    monkeypatch, tmp_path,
) -> None:
    source = tmp_path / "fake-audio"
    source.write_bytes(b"fake")
    monkeypatch.setattr(chunks, "_probe_duration", lambda _path: None)
    monkeypatch.setattr(chunks, "_extract_chunk", lambda *_args: pytest.fail("unexpected slicing"))
    expected = result((0, 42, "direct engine result"))
    calls = []
    progress = []

    def transcribe(path):
        calls.append(path)
        return expected

    async def on_progress(done, total):
        progress.append((done, total))

    session = _ResumeSession(tmp_path / "task", persistent=True)
    assert await session.asr(source, RECIPE, transcribe, on_progress) is expected
    assert calls == [source]
    assert progress == [(0, 1), (1, 1)]
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_completed_chunks_survive_interruption_and_resume_without_source_paths(
    fake_media, tmp_path,
) -> None:
    source, extracted = fake_media
    root = tmp_path / "task"
    calls: list[float] = []
    fail = True

    def transcribe(path):
        start = float(path.read_text())
        calls.append(start)
        assert len(list(root.glob("asr-chunk-*/*.wav"))) == 1
        if start == 2 and fail:
            raise RuntimeError("engine interrupted")
        return result((0.5, 1.5, f"speech at {start}"))

    with pytest.raises(RuntimeError, match="engine interrupted"):
        await _ResumeSession(root, persistent=True).asr(source, RECIPE, transcribe, no_progress)
    checkpoints = list(root.rglob("*.json"))
    assert len(checkpoints) == 1
    checkpoint_text = checkpoints[0].read_text()
    assert str(source) not in checkpoint_text
    assert source.name not in checkpoint_text
    assert str(root) not in checkpoint_text
    assert not list(root.glob("asr-chunk-*"))
    if os.name != "nt":
        assert stat.S_IMODE(checkpoints[0].stat().st_mode) == 0o600
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in [
            root, root / "stages", root / "stages" / "asr", checkpoints[0].parent,
        ])

    fail = False
    progress = []

    async def on_progress(done, total):
        progress.append((done, total))

    recovered = await _ResumeSession(root, persistent=True).asr(source, RECIPE, transcribe, on_progress)
    assert calls == [0, 2, 2, 4]
    assert extracted == [(0, 3), (2, 5), (2, 5), (4, 7)]
    assert [segment.start_seconds for segment in recovered.segments] == [0.5, 2.5, 4.5]
    assert [segment.end_seconds for segment in recovered.segments] == [1.5, 3.5, 5.5]
    assert progress == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert not list(root.glob("asr-chunk-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["media", "model", "language", "engine_revision", "chunk_seconds", "overlap_seconds"])
async def test_recipe_and_full_source_content_invalidate_completed_chunks(
    fake_media, tmp_path, invalidation,
) -> None:
    source, _extracted = fake_media
    root = tmp_path / "task"
    calls = []

    def transcribe(path):
        calls.append(float(path.read_text()))
        return result((0.1, 0.2, "speech"))

    await _ResumeSession(root, persistent=True).asr(source, RECIPE, transcribe, no_progress)
    calls.clear()
    recipe = RECIPE
    if invalidation == "media":
        original_stat = source.stat()
        source.write_bytes(b"different source data")  # Same size; mtime is not identity.
        assert source.stat().st_size == original_stat.st_size
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    else:
        value = {"model": "base", "language": "es", "engine_revision": "test-engine-v2",
                 "chunk_seconds": 4, "overlap_seconds": 0.5}[invalidation]
        recipe = replace(recipe, **{invalidation: value})
    await _ResumeSession(root, persistent=True).asr(source, recipe, transcribe, no_progress)
    assert len(calls) >= 2
    assert calls[0] == 0


@pytest.mark.asyncio
async def test_corrupt_checkpoint_reruns_only_its_chunk(fake_media, tmp_path) -> None:
    source, _extracted = fake_media
    session = _ResumeSession(tmp_path / "task", persistent=True)
    calls = []

    def transcribe(path):
        start = float(path.read_text())
        calls.append(start)
        return result((0, 1, f"speech {start}"))

    await session.asr(source, RECIPE, transcribe, no_progress)
    paths = sorted((tmp_path / "task").rglob("chunk-*.json"))
    paths[1].write_text("{broken-json", encoding="utf-8")
    calls.clear()
    await session.asr(source, RECIPE, transcribe, no_progress)
    assert calls == [2]


@pytest.mark.asyncio
async def test_boundary_dedup_preserves_negation_and_genuine_repeated_speech(
    fake_media, tmp_path,
) -> None:
    source, _extracted = fake_media
    returned = {
        0: result((1.8, 2.05, "no"), (2.1, 2.3, "no"), (2.4, 2.9, "I agree")),
        2: result((0.1, 0.3, "no"), (0.4, 0.9, "I agree"), (0.4, 0.9, "I do not agree"),
                  (1.2, 1.5, "I agree")),
        4: result((0, 1, "last statement")),
    }

    def transcribe(path):
        return returned[float(path.read_text())]

    recovered = await _ResumeSession(tmp_path / "task", persistent=False).asr(
        source, RECIPE, transcribe, no_progress,
    )
    assert [segment.text for segment in recovered.segments] == [
        "no", "no", "I agree", "I do not agree", "I agree", "last statement",
    ]
    assert [segment.start_seconds for segment in recovered.segments] == sorted(
        segment.start_seconds for segment in recovered.segments
    )


@pytest.mark.asyncio
async def test_empty_recognized_chunks_are_checkpointed_without_masking_failures(
    fake_media, tmp_path,
) -> None:
    source, _extracted = fake_media
    calls = []

    def transcribe(path):
        start = float(path.read_text())
        calls.append(start)
        if start != 2:
            raise RuntimeError("faster-whisper returned no recognizable speech")
        return result((0.2, 0.4, "only speech"))

    session = _ResumeSession(tmp_path / "task", persistent=True)
    first = await session.asr(source, RECIPE, transcribe, no_progress)
    assert first.text == "only speech"
    assert first.segments[0].start_seconds == 2.2
    calls.clear()
    assert await session.asr(source, RECIPE, transcribe, no_progress) == first
    assert calls == []


@pytest.mark.asyncio
async def test_cancellation_waits_for_inference_before_removing_its_audio(
    fake_media, tmp_path,
) -> None:
    source, _extracted = fake_media
    root = tmp_path / "task"
    started, release = threading.Event(), threading.Event()
    observed = []

    def transcribe(path):
        started.set()
        assert release.wait(5)
        observed.append(path.exists())
        return result((0, 1, "speech"))

    task = asyncio.create_task(_ResumeSession(root, persistent=True).asr(
        source, RECIPE, transcribe, no_progress,
    ))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert list(root.glob("asr-chunk-*/*.wav"))
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == [True]
    assert not list(root.glob("asr-chunk-*"))
    assert not list(root.rglob("chunk-*.json"))


@pytest.mark.asyncio
async def test_real_ffmpeg_chunks_have_bounded_duration_and_absolute_offsets(tmp_path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and ffprobe are required")
    source = tmp_path / "tone.wav"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=7:sample_rate=16000",
        "-c:a", "pcm_s16le", str(source),
    ], check=True, capture_output=True, timeout=30)
    lengths = []
    seen_paths = []

    def transcribe(path):
        seen_paths.append(path)
        with wave.open(str(path), "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getframerate() == 16000
            assert audio.getsampwidth() == 2
            lengths.append(audio.getnframes() / audio.getframerate())
            assert any(audio.readframes(16000))
        return result((0.5, 1.5, f"chunk {len(lengths)}"))

    recovered = await _ResumeSession(tmp_path / "task", persistent=True).asr(
        source, RECIPE, transcribe, no_progress,
    )
    assert lengths == pytest.approx([3, 3, 3], abs=1 / 16000)
    assert [segment.start_seconds for segment in recovered.segments] == [0.5, 2.5, 4.5]
    assert [segment.end_seconds for segment in recovered.segments] == [1.5, 3.5, 5.5]
    assert all(not path.exists() for path in seen_paths)


@pytest.mark.asyncio
async def test_known_short_media_prepares_private_audio_and_keeps_source_unchanged(monkeypatch, tmp_path) -> None:
    source = tmp_path / "short.wav"
    original = b"valid pretend short input"
    source.write_bytes(original)
    monkeypatch.setattr(chunks, "_probe_duration", lambda _path: 2.0)
    prepared = []

    def extract(path, target, start, end):
        assert path == source
        assert (start, end) == (0, 2)
        assert path.read_bytes() == original
        prepared.append(target)
        target.write_bytes(b"timeline-preserving prepared audio")

    monkeypatch.setattr(chunks, "_extract_chunk", extract)
    paths = []

    def transcribe(path):
        assert path != source
        assert path.read_bytes() == b"timeline-preserving prepared audio"
        if os.name != "nt":
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        paths.append(path)
        return result((0.2, 1.9, "short speech"))

    session = _ResumeSession(tmp_path / "task", persistent=True)
    assert (await session.asr(source, RECIPE, transcribe, no_progress)).text == "short speech"
    assert (await session.asr(source, RECIPE, transcribe, no_progress)).text == "short speech"
    assert paths == prepared
    assert len(paths) == 1
    assert not paths[0].exists()
    assert source.read_bytes() == original
    assert not list((tmp_path / "task").glob("asr-chunk-*"))
    payload = json.loads(next((tmp_path / "task").rglob("chunk-*.json")).read_text())
    assert payload["segments"][0]["start_seconds"] == 0.2


def test_real_ffmpeg_keeps_delayed_audio_on_the_video_timeline(tmp_path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is required")
    source = tmp_path / "delayed.mkv"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=16x16:d=7:r=1", "-itsoffset", "2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=16000",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source),
    ], check=True, capture_output=True, timeout=30)
    first_chunk = tmp_path / "chunk-0.wav"
    chunks._extract_chunk(source, first_chunk, 0, 3)
    with wave.open(str(first_chunk), "rb") as audio:
        assert audio.getnframes() == 3 * 16000
        samples = struct.unpack("<" + "h" * audio.getnframes(), audio.readframes(audio.getnframes()))
    first_sound = next(index for index, sample in enumerate(samples) if abs(sample) > 100)
    assert first_sound / 16000 == pytest.approx(2, abs=1 / 16000)

    second_chunk = tmp_path / "chunk-2.wav"
    chunks._extract_chunk(source, second_chunk, 2, 5)
    with wave.open(str(second_chunk), "rb") as audio:
        assert audio.getnframes() == 3 * 16000
        samples = struct.unpack("<" + "h" * audio.getnframes(), audio.readframes(audio.getnframes()))
    assert next(index for index, sample in enumerate(samples) if abs(sample) > 100) < 2


@pytest.mark.asyncio
async def test_default_single_window_session_keeps_short_delayed_video_timeline(tmp_path) -> None:
    executable = shutil.which("ffmpeg")
    if executable is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and ffprobe are required")
    source = tmp_path / "delayed-short.mkv"
    subprocess.run([
        executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=16x16:d=7:r=1", "-itsoffset", "2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=16000",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source),
    ], check=True, capture_output=True, timeout=30)
    original = source.read_bytes()
    original_mtime = source.stat().st_mtime_ns
    paths = []
    lengths = []

    def decode_like_mlx(path):
        paths.append(path)
        assert path != source
        assert path.suffix == ".wav"
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        # The production MLX audio loader decodes container audio to a raw
        # mono PCM sample array. It does not retain a separate stream offset.
        decoded = subprocess.run([
            executable, "-nostdin", "-threads", "0", "-i", str(path),
            "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", "16000", "-",
        ], check=True, capture_output=True, timeout=30).stdout
        samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
        lengths.append(len(samples) / 16000)
        first_sound = next(index for index, sample in enumerate(samples) if abs(sample) > 100) / 16000
        return result((first_sound, first_sound + 1, "Speech begins on the source timeline"))

    recipe = ASRRecipe("small", "en", "test-default-recipe-decoder")
    assert recipe.chunk_seconds == 300
    session = _ResumeSession(tmp_path / "task", persistent=True)

    recovered = await session.asr(source, recipe, decode_like_mlx, no_progress)
    cached = await session.asr(source, recipe, decode_like_mlx, no_progress)

    assert recovered.segments[0].start_seconds == pytest.approx(2, abs=1 / 16000)
    assert recovered.segments[0].end_seconds == pytest.approx(3, abs=1 / 16000)
    assert cached == recovered
    assert lengths == [7]
    assert len(paths) == 1
    assert not paths[0].exists()
    assert not list((tmp_path / "task").glob("asr-chunk-*"))
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == original_mtime
