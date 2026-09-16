import asyncio
import threading
from pathlib import Path

import pytest

from youtubetext.media import (
    FrameSampler,
    MediaDownloader,
    MediaDownloadError,
    MediaPurpose,
    locate_ffmpeg,
    sampling_interval,
)
from youtubetext.sources import YtDlpAuth


def test_sampling_interval_bounds_long_video_frame_count():
    assert sampling_interval(60) == 1
    assert sampling_interval(7200) == 3
    assert 7200 / sampling_interval(7200) <= 2400
    assert sampling_interval(2401) == pytest.approx(2401 / 2400)


def test_missing_ffmpeg_uses_a_cross_platform_install_hint(monkeypatch):
    monkeypatch.setattr("youtubetext.media.shutil.which", lambda _name: None)

    with pytest.raises(RuntimeError, match="add it to PATH") as error:
        locate_ffmpeg()

    assert "brew" not in str(error.value)


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
    assert seen["options"]["logger"] is not None
    assert seen["options"]["continuedl"] is True
    assert seen["options"]["nopart"] is False
    assert seen["options"]["outtmpl"].endswith("/audio.%(ext)s")


@pytest.mark.asyncio
async def test_failed_download_can_continue_with_the_same_part_file(tmp_path):
    directory = tmp_path / "work"
    templates: list[str] = []

    def fail_once(_url, options):
        template = str(options["outtmpl"])
        templates.append(template)
        part = Path(template.replace("%(ext)s", "mp4") + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"partial media")
        raise RuntimeError("network interrupted")

    with pytest.raises(MediaDownloadError, match="network interrupted"):
        await MediaDownloader(fail_once).download(
            "https://youtu.be/id",
            directory,
            MediaPurpose.ANALYSIS_VIDEO,
        )

    def continue_download(_url, options):
        template = str(options["outtmpl"])
        templates.append(template)
        final = Path(template.replace("%(ext)s", "mp4"))
        part = Path(f"{final}.part")
        assert part.read_bytes() == b"partial media"
        part.replace(final)
        return {}

    result = await MediaDownloader(continue_download).download(
        "https://youtu.be/id",
        directory,
        MediaPurpose.ANALYSIS_VIDEO,
    )

    assert templates[0] == templates[1]
    assert result == (directory / "analysis-video.mp4").resolve()


@pytest.mark.asyncio
async def test_cancellation_waits_until_the_download_thread_stops(tmp_path):
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def runner(_url, _options):
        started.set()
        release.wait(timeout=5)
        stopped.set()
        raise RuntimeError("stopped")

    task = asyncio.create_task(
        MediaDownloader(runner).download(
            "https://youtu.be/id",
            tmp_path / "work",
            MediaPurpose.AUDIO,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_media_download_uses_the_same_browser_cookies(tmp_path):
    artifact = tmp_path / "input.webm"
    artifact.write_bytes(b"media")
    seen = {}

    def runner(url, options):
        seen.update(url=url, options=options)
        return artifact

    await MediaDownloader(runner, auth=YtDlpAuth(browser="chrome")).download(
        "https://youtu.be/id", tmp_path / "work", MediaPurpose.AUDIO
    )

    assert seen["options"]["cookiesfrombrowser"] == (
        "chrome",
        None,
        None,
        None,
    )


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
async def test_media_retries_receive_fresh_in_memory_cookie_files(tmp_path):
    artifact = tmp_path / "input.mp4"
    artifact.write_bytes(b"media")
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("cookie data", encoding="utf-8")
    streams: list[object] = []

    def runner(_url, options):
        streams.append(options["cookiefile"])
        if len(streams) < 3:
            raise RuntimeError("HTTP Error 412: Precondition Failed")
        return artifact

    await MediaDownloader(
        runner,
        auth=YtDlpAuth(cookie_file=cookie_file),
        sleeper=lambda _delay: None,
    ).download(
        "https://www.bilibili.com/video/BV1abc",
        tmp_path / "work",
        MediaPurpose.ANALYSIS_VIDEO,
    )

    assert len(streams) == 3
    assert len({id(stream) for stream in streams}) == 3


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
