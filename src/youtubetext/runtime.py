"""Cross-platform host profiling and resource-aware task scheduling."""
from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, ParamSpec, Sequence, TypeVar

from .domain import TaskResult

T = TypeVar("T")
P = ParamSpec("P")
ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


class HostKind(str, Enum):
    APPLE_SILICON = "macos-apple-silicon"
    WINDOWS_X64 = "windows-x64"
    UNSUPPORTED = "unsupported"


async def run_blocking(
    call: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run a thread call without letting cancellation outlive its side effects."""

    worker = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancelled:
        # Repeated cancellation must not let callers release locks or remove
        # inputs while the thread can still mutate them.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if worker.done():
            try:
                worker.result()
            except BaseException:
                pass
        raise cancelled


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
    def kind(self) -> HostKind:
        system = self.system.casefold()
        machine = self.machine.casefold()
        if system == "darwin" and machine == "arm64":
            return HostKind.APPLE_SILICON
        if system == "windows" and machine in {"amd64", "x86_64"}:
            return HostKind.WINDOWS_X64
        return HostKind.UNSUPPORTED

    @property
    def supported(self) -> bool:
        return self.kind is not HostKind.UNSUPPORTED


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
            raise RuntimeError(
                "YouTubeText requires Apple Silicon macOS or 64-bit Windows"
            )
        memory = host.memory_gib
        memory_slots = (
            1 if memory < 12 else 2 if memory < 24 else 3 if memory < 40 else 4
        )
        if host.kind is HostKind.WINDOWS_X64:
            cpu_slots = max(1, min(4, host.cpu_count // 4))
            automatic = min(memory_slots, cpu_slots)
        else:
            automatic = memory_slots
        task_slots = min(8, requested_jobs) if requested_jobs > 0 else automatic
        task_slots = max(1, task_slots)
        model = (
            "small"
            if host.kind is HostKind.WINDOWS_X64 or memory < 16
            else "large-v3-turbo"
        )
        return cls(
            task_slots=task_slots,
            network_slots=min(4, max(2, task_slots)),
            # RapidOCR shares one ONNX session on Windows. Apple Vision uses
            # isolated helper processes and safely allows two OCR stages.
            ocr_slots=(
                1
                if host.kind is HostKind.WINDOWS_X64
                else min(2, task_slots)
            ),
            # One cached model is shared. Serial ASR prevents memory spikes and
            # compute contention even while URL tasks overlap at other stages.
            asr_slots=1,
            whisper_model=model,
        )


def detect_host() -> HostProfile:
    system = platform.system()
    memory = 0
    if system == "Darwin":
        try:
            raw = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True)
            memory = int(raw.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            memory = 0
    elif system == "Windows":
        memory = _windows_physical_memory()
    if not memory:
        try:
            memory = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (AttributeError, OSError, ValueError):
            memory = 8 * 1024**3
    return HostProfile(
        system=system,
        machine=platform.machine(),
        memory_bytes=memory,
        cpu_count=os.cpu_count() or 1,
    )


def _windows_physical_memory() -> int:
    """Read total physical memory through the Windows kernel API."""

    class _MemoryStatus(ctypes.Structure):
        _fields_ = (
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        )

    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    except (AttributeError, OSError):
        pass
    return 0


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

    async def map(
        self,
        items: Sequence[ItemT],
        worker: Callable[[ItemT], Awaitable[ResultT]],
        on_error: Callable[[ItemT, Exception], ResultT],
    ) -> list[ResultT]:
        """Map independent work with bounded concurrency and isolated errors."""

        semaphore = asyncio.Semaphore(self.plan.task_slots)

        async def guarded(item: ItemT) -> ResultT:
            async with semaphore:
                try:
                    return await worker(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one item must not cancel its siblings
                    return on_error(item, exc)

        return list(await asyncio.gather(*(guarded(item) for item in items)))

    async def run(
        self,
        urls: Sequence[str],
        worker: Callable[[str], Awaitable[TaskResult]],
    ) -> list[TaskResult]:
        return await self.map(
            urls,
            worker,
            lambda url, exc: TaskResult(url=url, error=str(exc)),
        )
