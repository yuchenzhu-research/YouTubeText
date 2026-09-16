import asyncio
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
from youtubetext.planning import PlanResult, build_processing_plan
from youtubetext.progress import Stage
from youtubetext.runtime import CapacityPlan, HostProfile
from youtubetext.sources import SourceInspection


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
    assert results[0].output.clean_markdown.is_file()
    assert results[1].error == "source failed"
    assert ("https://youtu.be/good", Stage.COMPLETE) in events


@pytest.mark.asyncio
async def test_engine_processes_repeated_url_only_once(tmp_path: Path):
    class CountingAcquirer(FakeAcquirer):
        def __init__(self):
            self.calls = []

        async def acquire(self, url, options, gates, progress):
            self.calls.append(url)
            return await super().acquire(url, options, gates, progress)

    acquirer = CountingAcquirer()
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    engine = YouTubeTextEngine(acquirer, CapacityPlan.for_host(host, requested_jobs=2))
    results = await engine.process(
        [" https://youtu.be/good ", "https://youtu.be/good"],
        TaskOptions(output_dir=tmp_path),
    )

    assert [result.url for result in results] == ["https://youtu.be/good"]
    assert acquirer.calls == ["https://youtu.be/good"]
    assert results[0].output.markdown.is_file()


class FakePlanningAcquirer:
    def __init__(self) -> None:
        self.acquire_calls = 0
        self.active = 0
        self.peak = 0

    async def plan(self, url, options, gates):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02 if "slow" in url else 0.001)
            if "bad" in url:
                raise RuntimeError("metadata failed")
            metadata = SourceMetadata(url, "youtube", url.rsplit("/", 1)[-1], "Title")
            return build_processing_plan(SourceInspection(metadata), options.mode)
        finally:
            self.active -= 1

    async def acquire(self, *_args, **_kwargs):
        self.acquire_calls += 1
        raise AssertionError("engine planning must not acquire a transcript")


@pytest.mark.asyncio
async def test_engine_plans_in_order_and_isolates_metadata_errors(tmp_path: Path):
    host = HostProfile("Darwin", "arm64", 48 * 1024**3, 8)
    acquirer = FakePlanningAcquirer()
    engine = YouTubeTextEngine(
        acquirer,
        CapacityPlan.for_host(host, requested_jobs=2),
    )
    urls = [
        "https://youtu.be/slow",
        "https://youtu.be/bad",
        "https://youtu.be/fast",
    ]

    results = await engine.plan(
        urls,
        TaskOptions(mode=ProcessingMode.AUTO, output_dir=tmp_path),
    )

    assert all(isinstance(result, PlanResult) for result in results)
    assert [result.url for result in results] == urls
    assert results[0].succeeded
    assert results[1].plan is None
    assert results[1].error == "metadata failed"
    assert results[2].succeeded
    assert acquirer.peak == 2
    assert acquirer.acquire_calls == 0


@pytest.mark.asyncio
async def test_engine_plans_repeated_url_only_once(tmp_path: Path):
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    acquirer = FakePlanningAcquirer()
    engine = YouTubeTextEngine(acquirer, CapacityPlan.for_host(host, requested_jobs=2))
    results = await engine.plan(
        [" https://youtu.be/same ", "https://youtu.be/same"],
        TaskOptions(output_dir=tmp_path),
    )

    assert [result.url for result in results] == ["https://youtu.be/same"]


@pytest.mark.asyncio
async def test_engine_plan_validates_urls_like_process(tmp_path: Path):
    host = HostProfile("Darwin", "arm64", 16 * 1024**3, 8)
    engine = YouTubeTextEngine(FakePlanningAcquirer(), CapacityPlan.for_host(host))

    with pytest.raises(ValueError, match="at least one"):
        await engine.plan([], TaskOptions(output_dir=tmp_path))
    with pytest.raises(ValueError, match="must not be empty"):
        await engine.plan(["  "], TaskOptions(output_dir=tmp_path))
