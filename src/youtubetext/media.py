"""Temporary media acquisition and frame sampling.

The acquisition pipeline asks this module for either a compact audio stream or one
low-resolution analysis video.  Download details, file discovery and FFmpeg command
construction stay behind that small interface.
"""
from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
import time
from dataclasses import dataclass
from collections.abc import AsyncIterator, Sequence
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .runtime import run_blocking
from .sources._adapter import resolve_adapter, run_with_platform_retries
from .sources._yt_dlp import QUIET_YT_DLP_LOGGER, YtDlpAuth

FRAME_SAMPLING_REVISION = "roi-fixed-fps-imagepipe-jpegq3-v2"
DEFAULT_OCR_REGION = (0.0, 0.55, 1.0, 0.45)
OCR_PROFILE_INTERVALS = {"fast": 1.0, "balanced": 0.5, "accurate": 0.25}


class MediaPurpose(str, Enum):
    AUDIO = "audio"
    ANALYSIS_VIDEO = "analysis-video"


class MediaDownloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SampledFrame:
    path: Path
    timestamp_seconds: float


def sampling_interval(duration_seconds: float, *, max_frames: int = 2400) -> float:
    """Legacy whole-video sampling; production OCR uses fixed profile intervals."""
    duration = max(0.0, float(duration_seconds or 0.0))
    if not duration:
        return 1.0
    return max(1.0, duration / max(1, max_frames))


def ocr_sampling_interval(profile: str = "balanced") -> float:
    try:
        return OCR_PROFILE_INTERVALS[profile]
    except (KeyError, TypeError) as exc:
        raise ValueError("OCR profile must be fast, balanced, or accurate") from exc


def normalize_ocr_region(region: Sequence[float]) -> tuple[float, float, float, float]:
    """Validate a normalized top-left (x, y, width, height) subtitle region."""

    try:
        values = tuple(float(value) for value in region)
    except (TypeError, ValueError) as exc:
        raise ValueError("OCR region must contain four finite numbers") from exc
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("OCR region must contain four finite numbers")
    x, y, width, height = values
    if (
        x < 0 or y < 0 or x >= 1 or y >= 1 or width <= 0 or height <= 0
        or x + width > 1.0 + 1e-9 or y + height > 1.0 + 1e-9
    ):
        raise ValueError("OCR region must fit inside the normalized video frame")
    return x, y, min(width, 1.0 - x), min(height, 1.0 - y)


def locate_ffmpeg() -> Path:
    value = shutil.which("ffmpeg")
    if not value:
        raise RuntimeError(
            "FFmpeg was not found on PATH. Install FFmpeg, add it to PATH, "
            "then reopen the terminal."
        )
    return Path(value).resolve()


class MediaDownloader:
    """yt-dlp adapter that downloads exactly one temporary media artifact."""

    def __init__(
        self,
        runner: Callable[[str, Mapping], object] | None = None,
        *,
        auth: YtDlpAuth | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._runner = runner
        self._auth = auth or YtDlpAuth()
        self._sleeper = sleeper

    @staticmethod
    def options_for(purpose: MediaPurpose, output_template: Path) -> dict:
        common = {
            "outtmpl": str(output_template),
            "noplaylist": True,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "logger": QUIET_YT_DLP_LOGGER,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
            "continuedl": True,
            "nopart": False,
        }
        if purpose is MediaPurpose.AUDIO:
            return {**common, "format": "bestaudio/best"}
        return {
            **common,
            "format": (
                "best[height<=480][vcodec!=none][acodec!=none]/"
                "best[height<=720][vcodec!=none][acodec!=none]/"
                "bestvideo[height<=480]+bestaudio/"
                "bestvideo[height<=720]+bestaudio"
            ),
            "merge_output_format": "mp4",
        }

    def _download_sync(self, url: str, directory: Path, purpose: MediaPurpose) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stem = purpose.value
        template = directory / f"{stem}.%(ext)s"
        adapter = resolve_adapter(url)
        request_url = adapter.request_url(url)

        def run_download() -> object:
            options = self.options_for(purpose, template)
            options.update(adapter.yt_dlp_options())
            options.update(self._auth.yt_dlp_options())
            if self._runner is not None:
                return self._runner(request_url, options)

            import yt_dlp

            with yt_dlp.YoutubeDL(options) as downloader:
                return downloader.extract_info(request_url, download=True)

        try:
            returned = run_with_platform_retries(
                adapter,
                run_download,
                sleep=self._sleeper,
            )
        except Exception as exc:
            raise MediaDownloadError(str(exc)) from exc

        if isinstance(returned, (str, Path)):
            path = Path(returned)
            if path.is_file():
                return path.resolve()

        candidates = [
            path
            for path in directory.glob(f"{stem}.*")
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ]
        if not candidates:
            raise MediaDownloadError("yt-dlp finished without producing a media file")
        candidates.sort(key=lambda path: path.stat().st_size, reverse=True)
        return candidates[0].resolve()

    async def download(self, url: str, directory: Path, purpose: MediaPurpose) -> Path:
        # asyncio cannot stop a running thread. Wait for yt-dlp to leave stable
        # output/.part files before a resume lock may be released.
        return await run_blocking(self._download_sync, url, directory, purpose)


class FrameSampler:
    """Stream fixed-density subtitle crops in windows of at most 32 images.

    ``sample`` remains a legacy compatibility helper. Production acquisition
    uses ``iter_batches`` so a long video never lowers temporal density or
    retains its entire frame collection on disk.
    """

    def __init__(self, ffmpeg: Path | None = None, *, max_frames: int = 2400):
        self._ffmpeg = ffmpeg
        self._max_frames = max(1, int(max_frames))

    @property
    def ffmpeg(self) -> Path:
        return self._ffmpeg or locate_ffmpeg()

    async def iter_batches(
        self,
        video_path: Path,
        output_dir: Path,
        duration_seconds: float,
        *,
        profile: str = "balanced",
        region: Sequence[float] = DEFAULT_OCR_REGION,
        batch_size: int = 32,
    ) -> AsyncIterator[tuple[SampledFrame, ...]]:
        """Yield cropped JPEG windows whose files live until the next window.

        Decoding runs once and is backpressured through a bounded pipe. Frame
        positions use a global ordinal, independent of duration estimates,
        window size, duplicate image reuse, and checkpoint reuse.
        """

        interval = ocr_sampling_interval(profile)
        x, y, width, height = normalize_ocr_region(region)
        if batch_size < 1:
            raise ValueError("frame batch_size must be at least one")
        window_size = min(32, int(batch_size))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filters = (
            # Round input PTS up to the output grid so each grid position uses
            # the latest source frame at or before that timestamp, rather than
            # a frame from the end of its sampling window.
            f"fps=1/{interval:g}:start_time=0:round=up,"
            f"crop=iw*{width:.12g}:ih*{height:.12g}:iw*{x:.12g}:ih*{y:.12g},"
            "scale='min(1280,iw)':-2"
        )
        process = await asyncio.create_subprocess_exec(
            str(self.ffmpeg),
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(Path(video_path).resolve()),
            "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", filters,
            "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        async def drain_errors() -> None:
            # FFmpeg errors can contain local paths. Drain to avoid deadlocks;
            # expose only a generic failure from this media boundary.
            while await process.stderr.read(4096):
                pass

        errors = asyncio.create_task(drain_errors())
        batch: list[SampledFrame] = []
        buffer = bytearray()
        ordinal = 0
        try:
            while True:
                chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), 60)
                if not chunk:
                    break
                buffer.extend(chunk)
                while len(buffer) >= 2:
                    if buffer[:2] != b"\xff\xd8":
                        raise RuntimeError("FFmpeg returned an invalid sampled image stream")
                    end = buffer.find(b"\xff\xd9", 2)
                    if end < 0:
                        if len(buffer) > 32 * 1024 * 1024:
                            raise RuntimeError("FFmpeg sampled image exceeded the size limit")
                        break
                    encoded = bytes(buffer[: end + 2])
                    del buffer[: end + 2]
                    path = output_dir / f"frame_{ordinal:012d}.jpg"
                    path.write_bytes(encoded)
                    batch.append(SampledFrame(path, ordinal * interval))
                    ordinal += 1
                    if len(batch) == window_size:
                        try:
                            yield tuple(batch)
                        finally:
                            for sampled in batch:
                                sampled.path.unlink(missing_ok=True)
                            batch.clear()
            if buffer:
                raise RuntimeError("FFmpeg returned an incomplete sampled image")
            if await asyncio.wait_for(process.wait(), 60) != 0:
                raise RuntimeError("FFmpeg frame sampling failed")
            if batch:
                try:
                    yield tuple(batch)
                finally:
                    for sampled in batch:
                        sampled.path.unlink(missing_ok=True)
                    batch.clear()
        finally:
            for sampled in batch:
                sampled.path.unlink(missing_ok=True)

            async def stop() -> None:
                async def discard_output() -> None:
                    while await process.stdout.read(64 * 1024):
                        pass

                discarded = asyncio.create_task(discard_output())
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
                await asyncio.gather(discarded, errors)

            cleanup = asyncio.create_task(stop())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated cancellation cannot release the media directory
                    # or task lock while FFmpeg still uses those inputs.
                    continue
            cleanup.result()

    def _sample_sync(
        self,
        video_path: Path,
        output_dir: Path,
        duration_seconds: float,
    ) -> tuple[SampledFrame, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        interval = sampling_interval(duration_seconds, max_frames=self._max_frames)
        output = output_dir / "frame_%08d.jpg"
        # Crop to the bottom 45%, where burned-in captions normally appear, before
        # scaling. This both improves OCR and avoids retaining unrelated frame data.
        filters = (
            f"fps=1/{interval:g},"
            "crop=iw:trunc(ih*0.45/2)*2:0:ih-trunc(ih*0.45/2)*2,"
            "scale=1280:-2"
        )
        result = subprocess.run(
            [
                str(self.ffmpeg),
                "-y",
                "-nostdin",
                "-i",
                str(video_path.resolve()),
                "-an",
                "-vf",
                filters,
                "-frames:v",
                str(self._max_frames),
                "-q:v",
                "3",
                str(output.resolve()),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3600,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "FFmpeg frame sampling failed")
        paths = sorted(output_dir.glob("frame_*.jpg"))
        return tuple(
            SampledFrame(path=path, timestamp_seconds=index * interval)
            for index, path in enumerate(paths)
        )

    async def sample(
        self,
        video_path: Path,
        output_dir: Path,
        duration_seconds: float,
    ) -> tuple[SampledFrame, ...]:
        return await run_blocking(
            self._sample_sync,
            Path(video_path),
            Path(output_dir),
            duration_seconds,
        )
