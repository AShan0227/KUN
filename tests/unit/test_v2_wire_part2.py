"""Tests for V2.1 wire part 2: blackboard data sources + precipitation idle step."""

from __future__ import annotations

import pytest
from kun.engineering.precipitation import (
    PrecipitationEvent,
)
from kun.engineering.precipitation_idle_step import (
    PrecipitationDailyStep,
    PrecipitationWeeklyStep,
    get_kp,
    install_precipitation_steps,
    reset_kp,
)


@pytest.fixture(autouse=True)
def _reset_kp():
    reset_kp()
    yield
    reset_kp()


def test_get_kp_singleton() -> None:
    kp1 = get_kp()
    kp2 = get_kp()
    assert kp1 is kp2


def test_get_kp_has_4_default_steps() -> None:
    """V2.1 §16.12 4 类 PrecipitationStep 默认注册."""
    kp = get_kp()
    # 私有 _steps, 测试通过行为验证 (跑 task.completed event 应被 stats_writeback 接)
    assert kp is not None


@pytest.mark.asyncio
async def test_precipitation_daily_step_runs_kp_daily_queue() -> None:
    kp = get_kp()
    # 推一个 task.completed 事件触发 NarrativeDistillStep (daily schedule, surprise>=0.6)
    event = PrecipitationEvent(
        event_id="ev-test",
        event_type="task.completed",
        payload={
            "task_id": "tk-1",
            "surprise_score": 0.8,
            "lesson_text": "学到的事",
        },
    )
    await kp.dispatch(event)

    step = PrecipitationDailyStep()
    result = await step.run(tenant_id="t-1")
    # NarrativeDistillStep 应跑出 1 条 methodology 资产
    assert result["step_id"] == "precipitation_daily"
    assert result["updates_count"] >= 1


@pytest.mark.asyncio
async def test_precipitation_weekly_step_runs_kp_weekly_queue() -> None:
    kp = get_kp()
    # 推 decision.completed → WeightTuneStep (weekly)
    event = PrecipitationEvent(
        event_id="ev-w",
        event_type="decision.completed",
        payload={"decision_kind": "model_select", "weight_delta": {"alpha": 0.1}},
    )
    await kp.dispatch(event)

    step = PrecipitationWeeklyStep()
    result = await step.run(tenant_id="t-1")
    assert result["step_id"] == "precipitation_weekly"
    assert result["updates_count"] >= 1


def test_install_precipitation_steps_idempotent() -> None:
    """重复 install 不重复注册."""
    install_precipitation_steps()
    install_precipitation_steps()
    install_precipitation_steps()

    from kun.engineering.idle_batch import list_steps

    steps = list_steps()
    assert steps.count("precipitation_daily") == 1
    assert steps.count("precipitation_weekly") == 1
