from pathlib import Path

import pytest

from youtubetext.media import MediaDownloader, MediaPurpose, sampling_interval


def test_sampling_interval_bounds_long_video_frame_count():
    assert sampling_interval(60) == 1
    assert sampling_interval(7200) == 3
    assert 7200 / sampling_interval(7200) <= 2400


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
