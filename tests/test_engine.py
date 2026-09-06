from pathlib import Path

import pytest

from youtubetext.domain import (
    ProcessingMode,
    SourceMetadata,
    TaskOptions,
    Transcript,
    TranscriptMethod,
    TranscriptSegment,
)
from youtubetext.engine import YouTubeTextEngine
from youtubetext.progress import Stage
from youtubetext.runtime import CapacityPlan, HostProfile


class FakeAcquirer:
    async def acquire(self, url, options, gates, progress):
        if "bad" in url:
            raise RuntimeError("source failed")
        await progress(type("E", (), {"url": url, "stage": Stage.CAPTIONS})())
        return Transcript(
            metadata=SourceMetadata(url, "youtube", "id", "Title"),
            language="en",
            method=TranscriptMethod.PLATFORM_CAPTIONS,
            segments=(TranscriptSegment(0, 1, "hello"),),
        )


@pytest.mark.asyncio
async def test_engine_exports_success_and_keeps_other_failure(tmp_path: Path):
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    engine = YouTubeTextEngine(FakeAcquirer(), CapacityPlan.for_host(host, requested_jobs=2))
    events = []

    async def collect(event):
        events.append((event.url, event.stage))

    results = await engine.process(
        ["https://youtu.be/good", "https://youtu.be/bad"],
        TaskOptions(mode=ProcessingMode.AUTO, output_dir=tmp_path),
        progress=collect,
    )
    assert results[0].succeeded
    assert results[0].output.markdown.is_file()
    assert results[1].error == "source failed"
    assert ("https://youtu.be/good", Stage.COMPLETE) in events
