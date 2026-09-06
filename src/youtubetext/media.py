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
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping


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
    duration = max(0.0, float(duration_seconds or 0.0))
    if not duration:
        return 1.0
    return float(max(1, math.ceil(duration / max(1, max_frames))))


def locate_ffmpeg() -> Path:
    value = shutil.which("ffmpeg")
    if not value:
        raise RuntimeError("FFmpeg was not found. Install it with: brew install ffmpeg")
    return Path(value).resolve()


class MediaDownloader:
    """yt-dlp adapter that downloads exactly one temporary media artifact."""

    def __init__(self, runner: Callable[[str, Mapping], object] | None = None):
        self._runner = runner

    @staticmethod
    def options_for(purpose: MediaPurpose, output_template: Path) -> dict:
        common = {
            "outtmpl": str(output_template),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 30,
        }
        if purpose is MediaPurpose.AUDIO:
            return {**common, "format": "bestaudio/best"}
        return {
            **common,
            "format": (
                "best[height<=480][vcodec!=none][acodec!=none]/"
                "best[height<=720][vcodec!=none][acodec!=none]/"
                "bestvideo[height<=480]+bestaudio/best"
            ),
            "merge_output_format": "mp4",
        }

    def _download_sync(self, url: str, directory: Path, purpose: MediaPurpose) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"media_{uuid.uuid4().hex[:12]}"
        template = directory / f"{stem}.%(ext)s"
        options = self.options_for(purpose, template)

        try:
            if self._runner is not None:
                returned = self._runner(url, options)
                if isinstance(returned, (str, Path)):
                    path = Path(returned)
                    if path.is_file():
                        return path.resolve()
            else:
                import yt_dlp

                with yt_dlp.YoutubeDL(options) as downloader:
                    downloader.extract_info(url, download=True)
        except Exception as exc:
            raise MediaDownloadError(str(exc)) from exc

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
        return await asyncio.to_thread(self._download_sync, url, directory, purpose)


class FrameSampler:
    """Sample the subtitle region while bounding temporary disk usage."""

    def __init__(self, ffmpeg: Path | None = None, *, max_frames: int = 2400):
        self._ffmpeg = ffmpeg
        self._max_frames = max(1, int(max_frames))

    @property
    def ffmpeg(self) -> Path:
        return self._ffmpeg or locate_ffmpeg()

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
        return await asyncio.to_thread(
            self._sample_sync,
            Path(video_path),
            Path(output_dir),
            duration_seconds,
        )
