"""A visibly truncated caption track must not prevent transcript fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from youtubetext.acquisition import TranscriptPipeline
from youtubetext.asr import ASRResult
from youtubetext.domain import ProcessingMode, TaskOptions, TranscriptMethod, TranscriptSegment
from youtubetext.media import MediaPurpose
from youtubetext.runtime import CapacityPlan, HostProfile, ResourceGates
from youtubetext.sources import SourceClient
from youtubetext.completeness import AudioActivity


VIDEO_URL = "https://www.youtube.com/watch?v=partial123"
EARLY_CAPTION = """WEBVTT

00:00:00.000 --> 00:00:02.000
Only the opening words
"""


class CaptionRunner:
    def __init__(self, *, duration: float, caption: str = EARLY_CAPTION) -> None:
        self.info = {
            "id": "partial123",
            "title": "A long video",
            "duration": duration,
            "webpage_url": VIDEO_URL,
            "subtitles": {"en": [{"ext": "vtt"}]},
        }
        self.caption = caption

    def run(
        self,
        url: str,
        options: Mapping[str, Any],
        *,
        download: bool,
    ) -> Mapping[str, Any]:
        if download:
            path = Path(str(options["outtmpl"]).replace("%(ext)s", "vtt"))
            path.write_text(self.caption, encoding="utf-8")
        return self.info


def test_source_preserves_early_caption_for_audio_aware_pipeline_verification():
    result = SourceClient(CaptionRunner(duration=120)).fetch(
        VIDEO_URL,
        strict_subtitles=False,
    )

    # Timing alone cannot distinguish missing words from a spoken intro
    # followed by a long quiet interval.
    assert result.subtitle is not None
    assert result.subtitle.text == "Only the opening words"


@pytest.mark.asyncio
async def test_auto_pipeline_preserves_early_caption_and_recovers_later_speech(tmp_path):
    class Media:
        async def download(self, _url, directory, purpose):
            assert purpose is MediaPurpose.AUDIO
            path = directory / "video.mp4"
            path.write_bytes(b"fake media")
            return path

    class Frames:
        async def sample(self, _video_path, _output_dir, _duration_seconds):
            return ()

    class OCR:
        async def recognize_images_async(self, _paths, **_options):
            raise AssertionError("no frames should reach OCR")

    class Speech:
        def transcribe(self, _path, *, language="auto"):
            assert language == "auto"
            return ASRResult(
                language="en",
                model="base",
                segments=(TranscriptSegment(20, 23, "Actual spoken content"),),
            )

    class Activity:
        def check(self, _path, _gaps):
            return AudioActivity.ACTIVE

    pipeline = TranscriptPipeline(
        sources=SourceClient(CaptionRunner(duration=120)),
        media=Media(),
        frames=Frames(),
        ocr=OCR(),
        asr_factory=lambda _model: Speech(),
        audio_activity=Activity(),
        temp_root=tmp_path,
    )
    gates = ResourceGates(
        CapacityPlan.for_host(HostProfile("Darwin", "arm64", 8 * 1024**3, 8))
    )

    async def progress(_event):
        return None

    transcript = await pipeline.acquire(
        VIDEO_URL,
        TaskOptions(mode=ProcessingMode.AUTO, whisper_model="base"),
        gates,
        progress,
    )

    assert transcript.method is TranscriptMethod.PLATFORM_CAPTIONS_WHISPER
    assert transcript.text == "Only the opening words\nActual spoken content"
    assert any("long" in warning.lower() for warning in transcript.warnings)


@pytest.mark.parametrize("duration", [0, 80])
def test_unknown_or_short_duration_keeps_sparse_caption(duration):
    result = SourceClient(CaptionRunner(duration=duration)).fetch(
        VIDEO_URL,
        strict_subtitles=False,
    )

    assert result.subtitle is not None
    assert result.subtitle.text == "Only the opening words"


def test_sparse_caption_does_not_fail_source_validation_without_audio_evidence():
    result = SourceClient(CaptionRunner(duration=120)).fetch(VIDEO_URL)
    assert result.subtitle is not None
