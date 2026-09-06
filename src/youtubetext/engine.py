"""Top-level YouTubeText module: acquire, export, isolate per-URL failures."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from .domain import TaskOptions, TaskResult, Transcript
from .export import export_transcript
from .progress import ProgressEvent, ProgressSink, Stage, discard_progress
from .runtime import CapacityPlan, ResourceGates, TaskScheduler


class TranscriptAcquirer(Protocol):
    async def acquire(
        self,
        url: str,
        options: TaskOptions,
        gates: ResourceGates,
        progress: ProgressSink,
    ) -> Transcript: ...


class YouTubeTextEngine:
    """The single interface used by the CLI for one or many URLs."""

    def __init__(self, acquirer: TranscriptAcquirer, plan: CapacityPlan):
        self._acquirer = acquirer
        self._plan = plan
        self._gates = ResourceGates(plan)

    async def process(
        self,
        urls: list[str],
        options: TaskOptions,
        *,
        progress: ProgressSink = discard_progress,
    ) -> list[TaskResult]:
        if not urls:
            raise ValueError("at least one URL is required")
        normalized = [url.strip() for url in urls]
        if any(not url for url in normalized):
            raise ValueError("URLs must not be empty")

        async def run_one(url: str) -> TaskResult:
            transcript = await self._acquirer.acquire(url, options, self._gates, progress)
            await progress(ProgressEvent(url, Stage.EXPORT, "Writing transcript files"))
            output = await asyncio.to_thread(export_transcript, transcript, Path(options.output_dir))
            await progress(ProgressEvent(url, Stage.COMPLETE, "Complete", 1.0))
            return TaskResult(url=url, transcript=transcript, output=output)

        return await TaskScheduler(self._plan).run(normalized, run_one)
