import asyncio

import pytest

from youtubetext.domain import TaskResult
from youtubetext.runtime import CapacityPlan, HostProfile, TaskScheduler


def host(memory_gib: int) -> HostProfile:
    return HostProfile("Darwin", "arm64", memory_gib * 1024**3, 10)


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


def test_unsupported_host_is_rejected():
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        CapacityPlan.for_host(HostProfile("Linux", "x86_64", 16 * 1024**3, 8))


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
