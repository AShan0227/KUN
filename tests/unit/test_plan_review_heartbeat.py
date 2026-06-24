"""L2.3 — Plan Review Heartbeat 单测.

验证:
  - step 阈值触发
  - time 阈值触发
  - 阈值重置 (mark_reviewed 后再来 N 步才再触发)
  - on_idle_tick 不推进 step
  - verdict 工程化规则 (aligned / drifting / off_track)
  - derive_action mapping
  - emitter 异步调用 + 异常吞掉不抛
  - reset / 多任务隔离
  - 非法参数拒绝
"""

from __future__ import annotations

import asyncio

import pytest
from kun.agents.supervisor.plan_review_heartbeat import (
    PlanReviewHeartbeat,
    ReviewTrigger,
    derive_action,
    evaluate_executor_self_report,
)

# --- 工程化规则评判 ---


def test_evaluate_aligned_when_self_report_clean() -> None:
    self_report = {
        "on_anchor": True,
        "recent_step_summary": "implemented authentication middleware",
        "criteria_done_count": 2,
        "criteria_done_count_prev": 2,
    }
    verdict, evidence = evaluate_executor_self_report(
        self_report, goal_tokens={"authentication", "middleware"}
    )
    assert verdict == "aligned"
    assert evidence == []


def test_evaluate_drifting_on_single_signal() -> None:
    self_report = {"scope_creep_detected": True}
    verdict, evidence = evaluate_executor_self_report(self_report)
    assert verdict == "drifting"
    assert len(evidence) == 1
    assert evidence[0]["type"] == "self_reported_scope_creep"


def test_evaluate_off_track_on_multiple_signals() -> None:
    self_report = {
        "scope_creep_detected": True,
        "on_anchor": False,
    }
    verdict, evidence = evaluate_executor_self_report(self_report)
    assert verdict == "off_track"
    assert len(evidence) == 2


def test_evaluate_no_goal_token_in_recent_step() -> None:
    self_report = {"recent_step_summary": "wrote some docs about coffee"}
    verdict, evidence = evaluate_executor_self_report(
        self_report, goal_tokens={"authentication", "middleware"}
    )
    assert verdict == "drifting"
    assert evidence[0]["type"] == "no_goal_token_in_recent_step"


def test_evaluate_criteria_regress() -> None:
    self_report = {"criteria_done_count": 1, "criteria_done_count_prev": 3}
    verdict, evidence = evaluate_executor_self_report(self_report)
    assert verdict == "drifting"
    assert evidence[0]["type"] == "criteria_regress"


def test_derive_action_mapping() -> None:
    assert derive_action("aligned") == "continue"
    assert derive_action("drifting") == "pause_for_anchor_recheck"
    assert derive_action("off_track") == "trigger_rcdh_level_0"


# --- step threshold ---


@pytest.mark.asyncio
async def test_step_threshold_triggers_after_n_steps() -> None:
    hb = PlanReviewHeartbeat(step_interval=3, time_interval_sec=600)
    self_report = {"on_anchor": True, "recent_step_summary": "wrote handler"}

    # 前 2 步: 无触发
    for _ in range(2):
        trigger = await hb.on_step_completed(
            task_id="t1",
            anchor_id="ga_1",
            executor_self_report=self_report,
        )
        assert trigger is None

    # 第 3 步: 触发
    trigger = await hb.on_step_completed(
        task_id="t1",
        anchor_id="ga_1",
        executor_self_report=self_report,
    )
    assert trigger is not None
    assert isinstance(trigger, ReviewTrigger)
    assert trigger.task_id == "t1"
    assert trigger.anchor_id == "ga_1"
    assert trigger.triggered_at_step == 3
    assert "step_threshold" in trigger.reason
    assert trigger.payload["supervisor_verdict"] == "aligned"
    assert trigger.payload["action_taken"] == "continue"


@pytest.mark.asyncio
async def test_step_threshold_resets_after_review() -> None:
    hb = PlanReviewHeartbeat(step_interval=2, time_interval_sec=600)
    self_report = {"on_anchor": True}

    # 第 2 步触发
    assert await hb.on_step_completed(task_id="t1", anchor_id="ga", executor_self_report=self_report) is None
    trigger1 = await hb.on_step_completed(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger1 is not None
    assert trigger1.triggered_at_step == 2

    # 第 3 步: 已 reset, 不触发
    assert await hb.on_step_completed(task_id="t1", anchor_id="ga", executor_self_report=self_report) is None

    # 第 4 步: 再次触发
    trigger2 = await hb.on_step_completed(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger2 is not None
    assert trigger2.triggered_at_step == 4


# --- time threshold ---


@pytest.mark.asyncio
async def test_time_threshold_triggers_after_interval() -> None:
    hb = PlanReviewHeartbeat(step_interval=1000, time_interval_sec=0.05)
    self_report = {"on_anchor": True}

    # 第 1 步: step 阈值远未到 (1000), 时间也未到
    trigger = await hb.on_step_completed(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger is None

    # 等过 time_interval
    await asyncio.sleep(0.07)

    # 第 2 步: time_threshold 命中
    trigger = await hb.on_step_completed(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger is not None
    assert "time_threshold" in trigger.reason


@pytest.mark.asyncio
async def test_idle_tick_only_checks_time_not_step() -> None:
    hb = PlanReviewHeartbeat(step_interval=3, time_interval_sec=0.05)
    self_report = {"on_anchor": True}

    # 跑 1 步 — step 不到, time 不到
    await hb.on_step_completed(task_id="t1", anchor_id="ga", executor_self_report=self_report)

    await asyncio.sleep(0.07)

    # idle_tick: 只查 time, 不推进 step
    trigger = await hb.on_idle_tick(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger is not None
    assert "time_threshold" in trigger.reason
    assert "step_threshold" not in trigger.reason
    assert trigger.payload["idle_tick"] is True
    # total_steps 应该还是 1 (idle 不推进)
    assert trigger.triggered_at_step == 1


# --- emitter + drift verdict ---


@pytest.mark.asyncio
async def test_emitter_called_with_trigger() -> None:
    received: list[ReviewTrigger] = []

    async def fake_emitter(trigger: ReviewTrigger) -> None:
        received.append(trigger)

    hb = PlanReviewHeartbeat(step_interval=1, time_interval_sec=600, emitter=fake_emitter)
    await hb.on_step_completed(
        task_id="t1",
        anchor_id="ga",
        executor_self_report={"scope_creep_detected": True},
    )
    assert len(received) == 1
    assert received[0].payload["supervisor_verdict"] == "drifting"
    assert received[0].payload["action_taken"] == "pause_for_anchor_recheck"


@pytest.mark.asyncio
async def test_emitter_exception_does_not_propagate() -> None:
    async def bad_emitter(trigger: ReviewTrigger) -> None:
        raise RuntimeError("emit failure")

    hb = PlanReviewHeartbeat(step_interval=1, time_interval_sec=600, emitter=bad_emitter)
    # 不抛 — emitter 异常被吞 + log.warning
    trigger = await hb.on_step_completed(
        task_id="t1",
        anchor_id="ga",
        executor_self_report={"on_anchor": True},
    )
    assert trigger is not None


@pytest.mark.asyncio
async def test_off_track_verdict_triggers_rcdh() -> None:
    hb = PlanReviewHeartbeat(step_interval=1, time_interval_sec=600)
    trigger = await hb.on_step_completed(
        task_id="t1",
        anchor_id="ga",
        executor_self_report={
            "scope_creep_detected": True,
            "on_anchor": False,
            "criteria_done_count": 0,
            "criteria_done_count_prev": 3,
        },
    )
    assert trigger is not None
    assert trigger.payload["supervisor_verdict"] == "off_track"
    assert trigger.payload["action_taken"] == "trigger_rcdh_level_0"
    assert len(trigger.payload["drift_evidence"]) >= 3


# --- multi-task isolation + reset ---


@pytest.mark.asyncio
async def test_multiple_tasks_isolated() -> None:
    hb = PlanReviewHeartbeat(step_interval=3, time_interval_sec=600)
    self_report = {"on_anchor": True}

    # t1 跑 2 步, t2 跑 3 步
    await hb.on_step_completed(task_id="t1", anchor_id="a1", executor_self_report=self_report)
    await hb.on_step_completed(task_id="t1", anchor_id="a1", executor_self_report=self_report)
    await hb.on_step_completed(task_id="t2", anchor_id="a2", executor_self_report=self_report)
    await hb.on_step_completed(task_id="t2", anchor_id="a2", executor_self_report=self_report)
    trigger_t2 = await hb.on_step_completed(
        task_id="t2", anchor_id="a2", executor_self_report=self_report
    )
    # t2 命中阈值, t1 仍只跑 2 步未触发
    assert trigger_t2 is not None
    assert trigger_t2.task_id == "t2"


@pytest.mark.asyncio
async def test_reset_clears_counter() -> None:
    hb = PlanReviewHeartbeat(step_interval=2, time_interval_sec=600)
    self_report = {"on_anchor": True}
    await hb.on_step_completed(task_id="t1", anchor_id="ga", executor_self_report=self_report)
    hb.reset("t1")
    # reset 后再跑 1 步, 计数器从 0 开始
    trigger = await hb.on_step_completed(
        task_id="t1", anchor_id="ga", executor_self_report=self_report
    )
    assert trigger is None  # 只 1 步, 未到 2


# --- 参数校验 ---


def test_invalid_step_interval_rejected() -> None:
    with pytest.raises(ValueError, match="step_interval"):
        PlanReviewHeartbeat(step_interval=0)


def test_invalid_time_interval_rejected() -> None:
    with pytest.raises(ValueError, match="time_interval_sec"):
        PlanReviewHeartbeat(time_interval_sec=0)


# --- 异步并发 ---


@pytest.mark.asyncio
async def test_concurrent_step_completed_serialized() -> None:
    """多 coroutine 并发调用 on_step_completed 必须线程安全 (asyncio.Lock)."""
    hb = PlanReviewHeartbeat(step_interval=5, time_interval_sec=600)
    self_report = {"on_anchor": True}

    async def step() -> None:
        await hb.on_step_completed(
            task_id="t1", anchor_id="ga", executor_self_report=self_report
        )

    await asyncio.gather(*(step() for _ in range(5)))
    counter = hb.counter_for("t1", "ga")
    # 5 步并发, 应在第 5 步触发并 reset
    assert counter.total_steps == 5
    assert counter.steps_since_last_review == 0
    assert counter.total_reviews == 1
