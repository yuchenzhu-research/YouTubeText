"""macOS host profiling and resource-aware task scheduling."""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Sequence, TypeVar

from .domain import TaskResult

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class HostProfile:
    system: str
    machine: str
    memory_bytes: int
    cpu_count: int

    @property
    def memory_gib(self) -> float:
        return self.memory_bytes / (1024**3)

    @property
    def supported(self) -> bool:
        return self.system == "Darwin" and self.machine == "arm64"


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    task_slots: int
    network_slots: int
    ocr_slots: int
    asr_slots: int
    whisper_model: str

    @classmethod
    def for_host(cls, host: HostProfile, requested_jobs: int = 0) -> "CapacityPlan":
        if not host.supported:
            raise RuntimeError("YouTubeText currently requires macOS on Apple Silicon")
        memory = host.memory_gib
        automatic = 1 if memory < 12 else 2 if memory < 24 else 3 if memory < 40 else 4
        task_slots = min(8, requested_jobs) if requested_jobs > 0 else automatic
        task_slots = max(1, task_slots)
        model = "small" if memory < 16 else "large-v3-turbo"
        return cls(
            task_slots=task_slots,
            network_slots=min(4, max(2, task_slots)),
            ocr_slots=min(2, task_slots),
            # A single cached MLX model is shared. Serial ASR avoids unified-memory
            # spikes and Metal contention even when URL tasks overlap elsewhere.
            asr_slots=1,
            whisper_model=model,
        )


def detect_host() -> HostProfile:
    memory = 0
    if platform.system() == "Darwin":
        try:
            raw = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True)
            memory = int(raw.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            memory = 0
    if not memory:
        try:
            memory = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (AttributeError, OSError, ValueError):
            memory = 8 * 1024**3
    return HostProfile(
        system=platform.system(),
        machine=platform.machine(),
        memory_bytes=memory,
        cpu_count=os.cpu_count() or 1,
    )


class ResourceGates:
    """Stage-specific semaphores shared by all URL tasks."""

    def __init__(self, plan: CapacityPlan):
        self.network = asyncio.Semaphore(plan.network_slots)
        self.ocr = asyncio.Semaphore(plan.ocr_slots)
        self.asr = asyncio.Semaphore(plan.asr_slots)


class TaskScheduler:
    """Run independent URL jobs concurrently while preserving input order."""

    def __init__(self, plan: CapacityPlan):
        self.plan = plan

    async def run(
        self,
        urls: Sequence[str],
        worker: Callable[[str], Awaitable[TaskResult]],
    ) -> list[TaskResult]:
        semaphore = asyncio.Semaphore(self.plan.task_slots)

        async def guarded(url: str) -> TaskResult:
            async with semaphore:
                try:
                    return await worker(url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one URL must not cancel its siblings
                    return TaskResult(url=url, error=str(exc))

        return list(await asyncio.gather(*(guarded(url) for url in urls)))
