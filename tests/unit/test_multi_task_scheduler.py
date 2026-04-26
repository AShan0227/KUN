"""C18 multi-task scheduler tests."""

from __future__ import annotations

import asyncio

import pytest
from kun.core.multi_task_scheduler import MultiTaskScheduler
from kun.datamodel.task import Owner, TaskMeta, TaskRef


def _task(task_id: str, user_id: str = "u1") -> TaskRef:
    owner = Owner(tenant_id="tenant", user_id=user_id)
    meta = TaskMeta(
        task_id=task_id,
        fingerprint=TaskMeta.compute_fingerprint(task_id, owner),
        task_type="coding.python",
        owner=owner,
        success_criteria_short=f"run {task_id}",
    )
    return TaskRef(meta=meta)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_runs_default_runner() -> None:
    scheduler = MultiTaskScheduler(max_concurrent_per_user=1, max_concurrent_global=1)

    task_id = await scheduler.submit(_task("tk-1"))
    result = await scheduler.wait_done(task_id, timeout_sec=2)

    assert result.status == "done"
    assert result.output["task_id"] == "tk-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_concurrency_limit_queues_extra_tasks() -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def runner(task: TaskRef) -> str:
        started.append(task.meta.task_id)
        await gate.wait()
        return task.meta.task_id

    scheduler = MultiTaskScheduler(
        max_concurrent_per_user=3, max_concurrent_global=1, runner=runner
    )
    first = await scheduler.submit(_task("tk-1"))
    second = await scheduler.submit(_task("tk-2", user_id="u2"))

    await asyncio.sleep(0)
    assert started == ["tk-1"]
    assert scheduler.get_status(second).status == "queued"

    gate.set()
    assert (await scheduler.wait_done(first, 2)).status == "done"
    assert (await scheduler.wait_done(second, 2)).status == "done"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_user_concurrency_limit_does_not_block_other_user() -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def runner(task: TaskRef) -> str:
        started.append(task.meta.task_id)
        await gate.wait()
        return task.meta.task_id

    scheduler = MultiTaskScheduler(
        max_concurrent_per_user=1, max_concurrent_global=2, runner=runner
    )
    await scheduler.submit(_task("tk-1", "u1"))
    second = await scheduler.submit(_task("tk-2", "u1"))
    await scheduler.submit(_task("tk-3", "u2"))

    await asyncio.sleep(0)
    assert started == ["tk-1", "tk-3"]
    assert scheduler.get_status(second).status == "queued"
    gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_queued_task() -> None:
    gate = asyncio.Event()

    async def runner(task: TaskRef) -> str:
        await gate.wait()
        return task.meta.task_id

    scheduler = MultiTaskScheduler(
        max_concurrent_per_user=1, max_concurrent_global=1, runner=runner
    )
    await scheduler.submit(_task("tk-1"))
    queued = await scheduler.submit(_task("tk-2"))

    assert scheduler.cancel(queued, "no longer needed") is True
    result = await scheduler.wait_done(queued, timeout_sec=2)

    assert result.status == "cancelled"
    assert "no longer needed" in result.error
    gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_running_task() -> None:
    gate = asyncio.Event()

    async def runner(_task_ref: TaskRef) -> str:
        await gate.wait()
        return "done"

    scheduler = MultiTaskScheduler(
        max_concurrent_per_user=1, max_concurrent_global=1, runner=runner
    )
    task_id = await scheduler.submit(_task("tk-1"))
    await asyncio.sleep(0)

    assert scheduler.cancel(task_id, "user stop") is True
    result = await scheduler.wait_done(task_id, timeout_sec=2)

    assert result.status == "cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runner_failure_marks_failed() -> None:
    async def runner(_task_ref: TaskRef) -> str:
        raise RuntimeError("boom")

    scheduler = MultiTaskScheduler(runner=runner)
    task_id = await scheduler.submit(_task("tk-1"))
    result = await scheduler.wait_done(task_id, timeout_sec=2)

    assert result.status == "failed"
    assert "RuntimeError" in result.error
    assert scheduler.get_status(task_id).status == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_done_unknown_task_raises() -> None:
    scheduler = MultiTaskScheduler()

    with pytest.raises(KeyError):
        await scheduler.wait_done("missing", timeout_sec=1)


@pytest.mark.unit
def test_get_status_unknown_task_raises() -> None:
    scheduler = MultiTaskScheduler()

    with pytest.raises(KeyError):
        scheduler.get_status("missing")


@pytest.mark.unit
def test_scheduler_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="max_concurrent_per_user"):
        MultiTaskScheduler(max_concurrent_per_user=0)
    with pytest.raises(ValueError, match="max_concurrent_global"):
        MultiTaskScheduler(max_concurrent_global=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_submit_rejected() -> None:
    scheduler = MultiTaskScheduler()
    await scheduler.submit(_task("tk-1"))

    with pytest.raises(ValueError, match="already submitted"):
        await scheduler.submit(_task("tk-1"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_user_tasks_filters_by_owner() -> None:
    scheduler = MultiTaskScheduler()
    await scheduler.submit(_task("tk-1", "u1"))
    await scheduler.submit(_task("tk-2", "u2"))
    await asyncio.sleep(0)

    assert {item.task_id for item in scheduler.list_user_tasks("u1")} == {"tk-1"}
