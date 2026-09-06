from pathlib import Path

import pytest

from youtubetext.media import FrameSampler, MediaDownloader, MediaPurpose, sampling_interval


def test_sampling_interval_bounds_long_video_frame_count():
    assert sampling_interval(60) == 1
    assert sampling_interval(7200) == 3
    assert 7200 / sampling_interval(7200) <= 2400
    assert sampling_interval(2401) == pytest.approx(2401 / 2400)


def test_download_options_choose_audio_or_low_resolution_video(tmp_path):
    audio = MediaDownloader.options_for(MediaPurpose.AUDIO, tmp_path / "a.%(ext)s")
    video = MediaDownloader.options_for(MediaPurpose.ANALYSIS_VIDEO, tmp_path / "v.%(ext)s")
    assert audio["format"] == "bestaudio/best"
    assert "height<=480" in video["format"]
    assert video["merge_output_format"] == "mp4"


@pytest.mark.asyncio
async def test_injected_download_runner_can_return_file(tmp_path):
    artifact = tmp_path / "input.webm"
    artifact.write_bytes(b"media")
    seen = {}

    def runner(url, options):
        seen.update(url=url, options=options)
        return artifact

    path = await MediaDownloader(runner).download(
        "https://youtu.be/id", tmp_path / "work", MediaPurpose.AUDIO
    )
    assert path == artifact.resolve()
    assert seen["url"] == "https://youtu.be/id"
    assert seen["options"]["extractor_retries"] == 3
    assert seen["options"]["noprogress"] is True


@pytest.mark.asyncio
async def test_frame_sampler_enforces_a_hard_frame_limit(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    command = []

    def run(arguments, **_kwargs):
        command.extend(arguments)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("youtubetext.media.subprocess.run", run)
    sampler = FrameSampler(tmp_path / "ffmpeg", max_frames=17)

    assert await sampler.sample(video, tmp_path / "frames", 0) == ()
    limit_index = command.index("-frames:v")
    assert command[limit_index + 1] == "17"
