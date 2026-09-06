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
async def test_bilibili_download_uses_non_redirecting_video_url(tmp_path):
    artifact = tmp_path / "input.mp4"
    artifact.write_bytes(b"media")
    seen = {}

    def runner(url, options):
        seen.update(url=url, options=options)
        return artifact

    await MediaDownloader(runner).download(
        "https://www.bilibili.com/video/BV1abc?p=2",
        tmp_path / "work",
        MediaPurpose.ANALYSIS_VIDEO,
    )

    assert seen["url"] == "https://www.bilibili.com/video/BV1abc/?p=2"
    assert "http_headers" not in seen["options"]


@pytest.mark.asyncio
async def test_bilibili_media_download_retries_http_412(tmp_path):
    artifact = tmp_path / "input.mp4"
    artifact.write_bytes(b"media")
    calls = 0
    delays: list[float] = []

    def runner(_url, _options):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("HTTP Error 412: Precondition Failed")
        return artifact

    path = await MediaDownloader(runner, sleeper=delays.append).download(
        "https://www.bilibili.com/video/BV1abc",
        tmp_path / "work",
        MediaPurpose.ANALYSIS_VIDEO,
    )

    assert path == artifact.resolve()
    assert calls == 3
    assert delays == [0.5, 1.5]


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
