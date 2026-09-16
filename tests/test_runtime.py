import asyncio
import threading

import pytest

from youtubetext.domain import TaskResult
import youtubetext.runtime as runtime_module
from youtubetext.runtime import (
    CapacityPlan,
    HostKind,
    HostProfile,
    TaskScheduler,
    detect_host,
    run_blocking,
)


def host(memory_gib: int) -> HostProfile:
    return HostProfile("Darwin", "arm64", memory_gib * 1024**3, 10)


def windows_host(memory_gib: int, cpu_count: int = 8) -> HostProfile:
    return HostProfile("Windows", "AMD64", memory_gib * 1024**3, cpu_count)


def test_capacity_plan_uses_small_model_on_low_memory_mac():
    plan = CapacityPlan.for_host(host(8))
    assert plan.task_slots == 1
    assert plan.asr_slots == 1
    assert plan.whisper_model == "small"


def test_capacity_plan_uses_turbo_and_bounded_manual_jobs():
    plan = CapacityPlan.for_host(host(48), requested_jobs=99)
    assert plan.task_slots == 8
    assert plan.whisper_model == "large-v3-turbo"
    assert plan.asr_slots == 1


def test_windows_x64_uses_conservative_local_model_capacity():
    profile = windows_host(32, cpu_count=8)

    plan = CapacityPlan.for_host(profile)

    assert profile.kind is HostKind.WINDOWS_X64
    assert profile.supported
    assert plan.task_slots == 2
    assert plan.network_slots == 2
    assert plan.ocr_slots == 1
    assert plan.asr_slots == 1
    assert plan.whisper_model == "small"


def test_windows_manual_jobs_remain_bounded():
    plan = CapacityPlan.for_host(windows_host(8, cpu_count=2), requested_jobs=99)

    assert plan.task_slots == 8
    assert plan.ocr_slots == 1
    assert plan.asr_slots == 1


def test_detect_host_reads_windows_memory_and_machine(monkeypatch):
    monkeypatch.setattr(runtime_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(runtime_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(runtime_module, "_windows_physical_memory", lambda: 24 * 1024**3)
    monkeypatch.setattr(runtime_module.os, "cpu_count", lambda: 12)

    profile = detect_host()

    assert profile == HostProfile("Windows", "AMD64", 24 * 1024**3, 12)
    assert profile.kind is HostKind.WINDOWS_X64


def test_unsupported_host_is_rejected():
    with pytest.raises(RuntimeError, match="64-bit Windows"):
        CapacityPlan.for_host(HostProfile("Linux", "x86_64", 16 * 1024**3, 8))


def test_windows_arm_is_not_claimed_as_supported():
    profile = HostProfile("Windows", "ARM64", 16 * 1024**3, 8)

    assert profile.kind is HostKind.UNSUPPORTED
    assert not profile.supported


@pytest.mark.asyncio
async def test_scheduler_preserves_order_and_isolates_failures():
    scheduler = TaskScheduler(CapacityPlan.for_host(host(48), requested_jobs=2))
    active = 0
    peak = 0

    async def worker(url: str) -> TaskResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if url == "bad":
            raise ValueError("broken")
        return TaskResult(url=url, error="not exported in this unit test")

    results = await scheduler.run(["first", "bad", "third"], worker)
    assert [result.url for result in results] == ["first", "bad", "third"]
    assert results[1].error == "broken"
    assert peak == 2


@pytest.mark.asyncio
async def test_scheduler_generic_map_preserves_order_and_maps_errors():
    scheduler = TaskScheduler(CapacityPlan.for_host(host(16), requested_jobs=2))

    async def worker(value: int) -> str:
        await asyncio.sleep(0.001 * (3 - value))
        if value == 2:
            raise ValueError("two failed")
        return f"ok:{value}"

    results = await scheduler.map(
        [1, 2, 3],
        worker,
        lambda value, exc: f"error:{value}:{exc}",
    )

    assert results == ["ok:1", "error:2:two failed", "ok:3"]


@pytest.mark.asyncio
async def test_blocking_thread_survives_repeated_cancellation_until_it_stops():
    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def blocking_work() -> None:
        started.set()
        release.wait(timeout=2)
        stopped.set()

    task = asyncio.create_task(run_blocking(blocking_work))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)

    assert not task.done()
    assert not stopped.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
