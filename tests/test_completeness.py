"""Coverage is a review signal; only decoded quiet audio establishes silence."""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from youtubetext.completeness import (
    AudioActivity,
    CoverageGap,
    FFmpegAudioActivity,
    assess_completeness,
)
from youtubetext.domain import TranscriptSegment


def cue(start: float, end: float, text: str = "Spoken words") -> TranscriptSegment:
    return TranscriptSegment(start, end, text)


def test_hour_video_with_only_first_twenty_four_minutes_is_flagged():
    report = assess_completeness((cue(0, 1440),), 3600)

    assert report.covered_seconds == 1440
    assert report.gaps == (CoverageGap(1440, 3600),)
    assert "00:24:00–01:00:00" in report.warning
    assert report.as_dict() == {
        "status": "needs-review",
        "duration_seconds": 3600,
        "covered_seconds": 1440,
        "coverage_ratio": 0.4,
        "long_gaps": [{"start_seconds": 1440, "end_seconds": 3600}],
    }


def test_intro_interior_and_trailing_gaps_are_all_checked():
    report = assess_completeness((cue(100, 200), cue(400, 500)), 600)

    assert report.covered_seconds == 200
    assert report.gaps == (
        CoverageGap(0, 100),
        CoverageGap(200, 400),
        CoverageGap(500, 600),
    )


def test_coverage_unions_overlaps_and_clips_to_known_duration():
    report = assess_completeness(
        (cue(80, 500), cue(20, 40), cue(0, 30), cue(35, 50), cue(60, 60)),
        100,
    )

    assert report.covered_seconds == 70
    assert report.gaps == ()
    assert report.as_dict()["coverage_ratio"] == 0.7
    assert report.as_dict()["status"] == "unverified"


@pytest.mark.parametrize("duration", [0, -1, math.nan, math.inf])
def test_unknown_duration_measures_text_intervals_without_certifying_coverage(duration):
    report = assess_completeness((cue(5, 15), cue(10, 20)), duration)

    assert report.duration_seconds == 0
    assert report.covered_seconds == 15
    assert report.gaps == ()
    assert report.as_dict()["coverage_ratio"] is None
    assert report.as_dict()["status"] == "unverified"


@pytest.mark.parametrize("duration", [0, 1, 80, 89.999])
def test_short_or_unknown_duration_does_not_judge_sparse_text(duration):
    report = assess_completeness((cue(0, 1),), duration)

    assert report.gaps == ()
    assert report.warning == ""


@pytest.mark.parametrize(
    "duration, last_end, suspicious",
    [(90, 30, True), (600, 541, False), (600, 540, True), (7200, 6900, True)],
)
def test_long_gap_threshold_is_bounded_and_inclusive(duration, last_end, suspicious):
    report = assess_completeness((cue(0, last_end),), duration)

    assert bool(report.gaps) is suspicious


def test_empty_long_transcript_flags_entire_timeline():
    report = assess_completeness((), 3600)

    assert report.gaps == (CoverageGap(0, 3600),)
    assert report.covered_seconds == 0


def test_complete_timeline_is_still_unverified():
    report = assess_completeness((cue(0, 3600),), 3600)

    assert report.warning == ""
    assert report.as_dict()["status"] == "unverified"


def test_warning_bounds_gap_listing_and_keeps_fractional_timestamps():
    report = assess_completeness(
        tuple(cue(index * 400 + 0.125, index * 400 + 1) for index in range(1, 10)),
        4000,
    )

    assert len(report.gaps) == 10
    assert "00:06:40.125" in report.warning
    assert "and 5 more" in report.warning
    assert "01:06:40" not in report.warning


def progress(duration: float) -> str:
    return f"out_time_us={round(duration * 1_000_000)}\nprogress=end\n"


def silence(start: object, end: object | None = None) -> str:
    result = f"[silencedetect @ 0x123] silence_start: {start}\n"
    if end is not None:
        result += f"[silencedetect @ 0x123] silence_end: {end} | silence_duration: 1\n"
    return result


def decoded_audio(start: float = 0, end: float = 120) -> str:
    return (
        f"[Parsed_ashowinfo_0 @ 0x123] n:0 pts_time:{start} "
        f"rate:8000 nb_samples:{round((end - start) * 8000)}\n"
    )


def scanner(monkeypatch, *, stdout=None, stderr="", returncode=0, frames=None):
    def run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=returncode,
            stdout=progress(120) if stdout is None else stdout,
            stderr=(decoded_audio() if frames is None else frames) + stderr,
        )

    monkeypatch.setattr("youtubetext.completeness.subprocess.run", run)
    return FFmpegAudioActivity(Path("ffmpeg"))


def test_successful_quiet_decoding_covers_all_requested_gaps(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0, 30) + silence(90, 120))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(1, 29), CoverageGap(91, 120))) is AudioActivity.SILENT


def test_active_audio_is_not_described_as_speech(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0, 30) + silence(90, 120))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(20, 100),)) is AudioActivity.ACTIVE


def test_short_interior_audio_burst_is_not_erased_by_boundary_tolerance(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0, 60) + silence(60.1, 120))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.ACTIVE


def test_ffmpeg_parsed_filter_log_prefix_is_supported(monkeypatch, tmp_path):
    output = silence(0, 120).replace("[silencedetect @", "[Parsed_silencedetect_1 @")
    activity = scanner(monkeypatch, stderr=output)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.SILENT


def test_unclosed_silence_event_ends_only_at_successfully_decoded_eof(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.SILENT
    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 3600),)) is AudioActivity.UNKNOWN


def test_one_gap_beyond_decoded_media_makes_entire_check_unknown(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0, 120))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 60), CoverageGap(100, 121))) is AudioActivity.UNKNOWN


def test_progress_and_silence_without_real_sample_evidence_are_unknown(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0, 120), frames="")

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.UNKNOWN


def test_decoded_sample_hole_is_unknown_despite_silence_and_progress(monkeypatch, tmp_path):
    frames = decoded_audio(0, 1) + decoded_audio(119, 120)
    activity = scanner(monkeypatch, stderr=silence(0, 120), frames=frames)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(1, 119),)) is AudioActivity.UNKNOWN
    assert activity.check(tmp_path / "private.wav", (CoverageGap(119, 120),)) is AudioActivity.SILENT


def test_repeated_small_timestamp_holes_cannot_accumulate_into_silent_coverage(monkeypatch, tmp_path):
    frames = "".join(decoded_audio(index * 0.2, index * 0.2 + 0.1) for index in range(600))
    activity = scanner(monkeypatch, stderr=silence(0, 120), frames=frames)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(1, 119),)) is AudioActivity.UNKNOWN


def test_container_timestamp_rounding_uses_cumulative_sample_duration(monkeypatch, tmp_path):
    frames = "".join(
        f"[Parsed_ashowinfo_0 @ 0x123] n:{index} "
        f"pts_time:{round(index * 1024 / 48000, 3)} rate:48000 nb_samples:1024\n"
        for index in range(5625)
    )
    activity = scanner(monkeypatch, stderr=silence(0, 120), frames=frames)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.SILENT


@pytest.mark.parametrize(
    "frames",
    [
        "[ashowinfo @ 0x123] pts_time:0 rate:0 nb_samples:960000\n",
        "[ashowinfo @ 0x123] pts_time:0 rate:8000 nb_samples:0\n",
        "[ashowinfo @ 0x123] pts_time:nan rate:8000 nb_samples:960000\n",
        "[ashowinfo @ 0x123] pts_time:-1 rate:8000 nb_samples:960000\n",
        "[ashowinfo @ 0x123] pts_time:0 rate:8000 nb_samples:bad\n",
        "[ashowinfo @ 0x123] pts_time:0 nb_samples:960000\n",
        decoded_audio(0, 121),
        decoded_audio(10, 20) + decoded_audio(0, 10),
    ],
)
def test_invalid_decoded_sample_evidence_is_unknown(monkeypatch, tmp_path, frames):
    activity = scanner(monkeypatch, stderr=silence(0, 120), frames=frames)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.UNKNOWN


def test_boundary_rounding_does_not_trigger_unnecessary_speech_recognition(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, stderr=silence(0.2, 119.8))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 120),)) is AudioActivity.SILENT


def test_boundary_tolerance_never_proves_silence_without_silent_samples(monkeypatch, tmp_path):
    activity = scanner(monkeypatch)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 0.1),)) is AudioActivity.ACTIVE


@pytest.mark.parametrize(
    "output",
    [
        "",
        "out_time_us=120000000\nprogress=continue\n",
        "out_time_us=N/A\nprogress=end\n",
        "out_time_us=nan\nprogress=end\n",
        "out_time_us=inf\nprogress=end\n",
        "out_time_us=-1\nprogress=end\n",
        "out_time_us=0\nprogress=end\n",
        "out_time_us=120000000\nout_time_us=1000000\nprogress=end\n",
    ],
)
def test_missing_or_invalid_decoded_duration_never_proves_silence(monkeypatch, tmp_path, output):
    activity = scanner(monkeypatch, stdout=output, stderr=silence(0))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.UNKNOWN


@pytest.mark.parametrize(
    "output",
    [
        silence("nan", 120),
        silence("inf", 120),
        silence("bad", 120),
        silence(-10, 120),
        silence(0, 121),
        silence(60, 30),
        silence(0) + silence(30),
        "[silencedetect @ 0x123] silence_end: 120\n",
        "[silencedetect @ 0x123] silence_start:\n",
        silence(30, 90) + silence(60, 120),
    ],
)
def test_malformed_silence_events_never_prove_silence(monkeypatch, tmp_path, output):
    activity = scanner(monkeypatch, stderr=output)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.UNKNOWN


def test_unsuccessful_decode_is_unknown_even_with_quiet_output(monkeypatch, tmp_path):
    activity = scanner(monkeypatch, returncode=1, stderr=silence(0, 120))

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.UNKNOWN


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("private path"), subprocess.TimeoutExpired("private path", 120)],
)
def test_execution_failures_remain_unknown(monkeypatch, tmp_path, error):
    def run(_command, **_kwargs):
        raise error

    monkeypatch.setattr("youtubetext.completeness.subprocess.run", run)

    assert FFmpegAudioActivity(Path("ffmpeg")).check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.UNKNOWN


def test_missing_ffmpeg_remains_unknown(monkeypatch, tmp_path):
    def missing():
        raise RuntimeError("FFmpeg was not found")

    monkeypatch.setattr("youtubetext.completeness.locate_ffmpeg", missing)

    assert FFmpegAudioActivity().check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.UNKNOWN


@pytest.mark.parametrize(
    "gaps",
    [(), (CoverageGap(-1, 10),), (CoverageGap(10, 10),), (CoverageGap(10, 5),), (CoverageGap(0, math.inf),)],
)
def test_invalid_or_empty_requests_do_not_start_ffmpeg(monkeypatch, tmp_path, gaps):
    def forbidden(_command, **_kwargs):
        raise AssertionError("FFmpeg must not start")

    monkeypatch.setattr("youtubetext.completeness.subprocess.run", forbidden)

    assert FFmpegAudioActivity(Path("ffmpeg")).check(tmp_path / "private.wav", gaps) is AudioActivity.UNKNOWN


def test_ffmpeg_command_is_noninteractive_channel_preserving_and_time_bounded(monkeypatch, tmp_path):
    seen = {}

    def run(command, **options):
        seen.update(command=command, options=options)
        return SimpleNamespace(
            returncode=0,
            stdout=progress(120),
            stderr=decoded_audio() + silence(0, 120),
        )

    monkeypatch.setattr("youtubetext.completeness.subprocess.run", run)
    activity = FFmpegAudioActivity(Path("ffmpeg"), timeout_seconds=999)

    assert activity.check(tmp_path / "private.wav", (CoverageGap(0, 60),)) is AudioActivity.SILENT
    assert "-nostdin" in seen["command"]
    assert "-xerror" in seen["command"]
    assert "-ac" not in seen["command"]
    assert seen["options"]["timeout"] == 120
    assert seen["options"]["stdout"] == subprocess.PIPE
    assert seen["options"]["stderr"] == subprocess.PIPE


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="timeout_seconds"):
        FFmpegAudioActivity(timeout_seconds=timeout)


def write_wave(path: Path, *, opposed_stereo: bool = False) -> None:
    rate = 8000
    samples = bytearray()
    for index in range(rate * 3):
        amplitude = (
            round(12000 * math.sin(2 * math.pi * 440 * index / rate))
            if rate <= index < rate * 2
            else 0
        )
        samples.extend(struct.pack("<h", amplitude))
        if opposed_stereo:
            samples.extend(struct.pack("<h", -amplitude))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2 if opposed_stereo else 1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples)


@pytest.mark.parametrize("opposed_stereo", [False, True])
def test_real_ffmpeg_distinguishes_generated_quiet_audio_from_tone(tmp_path, opposed_stereo):
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is unavailable")
    path = tmp_path / "generated.wav"
    write_wave(path, opposed_stereo=opposed_stereo)
    activity = FFmpegAudioActivity(Path(executable), timeout_seconds=10)

    assert activity.check(path, (CoverageGap(0, 0.8), CoverageGap(2.1, 3))) is AudioActivity.SILENT
    assert activity.check(path, (CoverageGap(1.1, 1.9),)) is AudioActivity.ACTIVE
    assert activity.check(path, (CoverageGap(0, 3),)) is AudioActivity.ACTIVE
    assert activity.check(path, (CoverageGap(0, 4),)) is AudioActivity.UNKNOWN


def test_real_ffmpeg_preserves_delayed_audio_alignment_with_video(tmp_path):
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is unavailable")
    audio = tmp_path / "generated.wav"
    video = tmp_path / "delayed-audio.mkv"
    write_wave(audio)
    subprocess.run(
        [
            executable,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=2:d=4",
            "-itsoffset",
            "1",
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-t",
            "4",
            str(video),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    )
    activity = FFmpegAudioActivity(Path(executable), timeout_seconds=10)

    assert activity.check(video, (CoverageGap(0, 1.8),)) is AudioActivity.UNKNOWN
    assert activity.check(video, (CoverageGap(1.1, 1.8),)) is AudioActivity.SILENT
    assert activity.check(video, (CoverageGap(2.1, 2.9),)) is AudioActivity.ACTIVE
    assert activity.check(video, (CoverageGap(3.1, 4),)) is AudioActivity.SILENT


def test_real_ffmpeg_timestamp_hole_is_missing_evidence_not_silence(tmp_path):
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is unavailable")
    media = tmp_path / "timestamp-gap.mka"
    subprocess.run(
        [
            executable,
            "-y",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono:d=3",
            "-af",
            r"asetpts=PTS+gte(T\,1)*118/TB",
            "-c:a",
            "pcm_s16le",
            str(media),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    )
    activity = FFmpegAudioActivity(Path(executable), timeout_seconds=10)

    assert activity.check(media, (CoverageGap(1, 119),)) is AudioActivity.UNKNOWN
    assert activity.check(media, (CoverageGap(0, 0.8),)) is AudioActivity.SILENT
    assert activity.check(media, (CoverageGap(119.1, 120.8),)) is AudioActivity.SILENT


def test_real_ffmpeg_repeated_short_clock_holes_are_missing_evidence(tmp_path):
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is unavailable")
    media = tmp_path / "clock-gap.mka"
    subprocess.run(
        [
            executable,
            "-y",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono:d=3",
            "-af",
            "asetpts=PTS*2",
            "-c:a",
            "pcm_s16le",
            str(media),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    )
    activity = FFmpegAudioActivity(Path(executable), timeout_seconds=10)

    assert activity.check(media, (CoverageGap(1, 5),)) is AudioActivity.UNKNOWN
