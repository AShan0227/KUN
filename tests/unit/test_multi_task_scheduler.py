"""C18 multi-task scheduler tests."""

from __future__ import annotations

import asyncio

import pytest
from kun.core.multi_task_scheduler import MultiTaskScheduler, route_task_lane
from kun.datamodel.task import Owner, Risk, TaskMeta, TaskRef, TaskSpec


def _task(
    task_id: str,
    user_id: str = "u1",
    *,
    task_type: str = "coding.python",
    risk_level: Risk = "low",
    required_skills: list[str] | None = None,
) -> TaskRef:
    owner = Owner(tenant_id="tenant", user_id=user_id)
    meta = TaskMeta(
        task_id=task_id,
        fingerprint=TaskMeta.compute_fingerprint(task_id, owner),
        task_type=task_type,
        risk_level=risk_level,
        owner=owner,
        success_criteria_short=f"run {task_id}",
    )
    spec = None
    if required_skills is not None:
        spec = TaskSpec(goal_detail=f"run {task_id}", required_skills=required_skills)
    return TaskRef(meta=meta, spec=spec)


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
async def test_runner_receives_task_tenant_context() -> None:
    from kun.core.tenancy import current_tenant

    async def runner(_task_ref: TaskRef) -> dict[str, str | None]:
        ctx = current_tenant()
        return {"tenant_id": ctx.tenant_id, "user_id": ctx.user_id}

    scheduler = MultiTaskScheduler(runner=runner)

    task_id = await scheduler.submit(_task("tk-context", user_id="u-context"))
    result = await scheduler.wait_done(task_id, timeout_sec=2)

    assert result.output == {"tenant_id": "tenant", "user_id": "u-context"}


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


@pytest.mark.unit
def test_route_task_lane_defaults_to_fast() -> None:
    assert route_task_lane(_task("tk-fast")) == "fast"


@pytest.mark.unit
def test_route_task_lane_detects_special_lanes() -> None:
    assert route_task_lane(_task("tk-mission", task_type="mission.product_ops")) == "mission"
    assert route_task_lane(_task("tk-qi", task_type="qi.experiment")) == "qi"
    assert route_task_lane(_task("tk-nuo", task_type="nuo.maintenance")) == "nuo"
    assert route_task_lane(_task("tk-world", required_skills=["world_request"])) == "world"
    assert route_task_lane(_task("tk-risk", risk_level="high")) == "high_risk"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lane_limit_queues_only_same_lane() -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def runner(task: TaskRef) -> str:
        started.append(task.meta.task_id)
        await gate.wait()
        return task.meta.task_id

    scheduler = MultiTaskScheduler(
        max_concurrent_per_user=10,
        max_concurrent_global=10,
        max_concurrent_per_lane={"qi": 1, "fast": 10},
        runner=runner,
    )
    qi_1 = await scheduler.submit(_task("tk-qi-1", task_type="qi.experiment"))
    qi_2 = await scheduler.submit(_task("tk-qi-2", task_type="qi.experiment"))
    fast = await scheduler.submit(_task("tk-fast"))

    await asyncio.sleep(0)

    assert started == ["tk-qi-1", "tk-fast"]
    assert scheduler.get_status(qi_1).lane == "qi"
    assert scheduler.get_status(qi_2).status == "queued"
    assert scheduler.get_status(fast).lane == "fast"

    gate.set()
    assert (await scheduler.wait_done(qi_2, 2)).status == "done"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_allows_lane_override() -> None:
    scheduler = MultiTaskScheduler()

    task_id = await scheduler.submit(_task("tk-override"), lane="world")
    await asyncio.sleep(0)

    assert scheduler.get_status(task_id).lane == "world"
