"""Tests for precipitation (V2.1 §16.12 / ADR-025)."""

from __future__ import annotations

import pytest
from kun.engineering.precipitation import (
    AssetUpdate,
    KnowledgePrecipitation,
    NarrativeDistillStep,
    PrecipitationEvent,
    RuleEmergeStep,
    StatsWritebackStep,
    WeightTuneStep,
)


@pytest.mark.asyncio
async def test_stats_writeback_realtime() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(StatsWritebackStep())
    event = PrecipitationEvent(
        event_id="ev-1",
        event_type="task.completed",
        payload={
            "entity_id": "rt-coder",
            "task_type": "coding.py",
            "outcome": 0.85,
            "cost_usd": 0.05,
            "latency_sec": 12.0,
        },
    )
    updates = await kp.dispatch(event)
    assert len(updates) == 1
    assert updates[0].asset_kind == "capability_card"
    assert updates[0].asset_ref == "rt-coder"


@pytest.mark.asyncio
async def test_weight_tune_queued_for_weekly() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(WeightTuneStep())
    event = PrecipitationEvent(
        event_id="ev-2",
        event_type="decision.completed",
        payload={"decision_kind": "model_select"},
    )
    # weekly schedule → 入队不立即跑
    updates = await kp.dispatch(event)
    assert len(updates) == 0  # realtime 没跑

    # 周期跑
    weekly_updates = await kp.run_scheduled("weekly")
    assert len(weekly_updates) == 1
    assert weekly_updates[0].asset_kind == "weight_table"
    assert weekly_updates[0].requires_approval is True


@pytest.mark.asyncio
async def test_rule_emerge_queued_weekly() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(RuleEmergeStep())
    event = PrecipitationEvent(
        event_id="ev-3",
        event_type="task.replan",
        payload={"reason": "step_count_exceeded"},
    )
    await kp.dispatch(event)
    updates = await kp.run_scheduled("weekly")
    assert len(updates) == 1
    assert updates[0].asset_kind == "rule"
    assert updates[0].payload["status"] == "shadow"


@pytest.mark.asyncio
async def test_narrative_distill_high_surprise_only() -> None:
    """surprise_score < 0.6 → 不蒸馏."""
    kp = KnowledgePrecipitation()
    kp.register_step(NarrativeDistillStep())

    low_event = PrecipitationEvent(
        event_id="ev-low",
        event_type="task.completed",
        payload={"surprise_score": 0.3, "task_id": "tk-low"},
    )
    await kp.dispatch(low_event)
    updates_daily = await kp.run_scheduled("daily")
    assert len(updates_daily) == 0  # surprise < 0.6 跳

    high_event = PrecipitationEvent(
        event_id="ev-high",
        event_type="task.completed",
        payload={
            "surprise_score": 0.8,
            "task_id": "tk-high",
            "lesson_text": "学到了 X",
        },
    )
    await kp.dispatch(high_event)
    updates_daily = await kp.run_scheduled("daily")
    assert len(updates_daily) == 1
    assert updates_daily[0].asset_kind == "methodology"


@pytest.mark.asyncio
async def test_apply_hook_called_for_realtime() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(StatsWritebackStep())

    captured: list[AssetUpdate] = []

    async def hook(u: AssetUpdate) -> None:
        captured.append(u)

    kp.register_asset_apply_hook(hook)
    event = PrecipitationEvent(
        event_id="ev-x",
        event_type="task.completed",
        payload={"entity_id": "x", "outcome": 0.5},
    )
    await kp.dispatch(event)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_audit_log_records_all() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(StatsWritebackStep())
    event = PrecipitationEvent(
        event_id="ev-a",
        event_type="task.completed",
        payload={"entity_id": "x"},
    )
    await kp.dispatch(event)
    log = kp.get_audit_log()
    assert len(log) == 1
    assert log[0][0] == "stats_writeback"


@pytest.mark.asyncio
async def test_step_failure_non_fatal() -> None:
    """单 step 失败不阻塞其他."""

    class FailingStep:
        source_event_type = "task.completed"
        step_kind = "stats_writeback"
        schedule = "realtime"

        async def precipitate(self, event, context=None):
            raise RuntimeError("boom")

    kp = KnowledgePrecipitation()
    kp.register_step(FailingStep())  # type: ignore[arg-type]
    kp.register_step(StatsWritebackStep())  # 后注册的应仍能跑
    event = PrecipitationEvent(
        event_id="ev-f",
        event_type="task.completed",
        payload={"entity_id": "y"},
    )
    updates = await kp.dispatch(event)
    # 1 step 成功 (FailingStep 失败但不阻塞 StatsWritebackStep)
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_unmatched_event_type_ignored() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(StatsWritebackStep())  # source: task.completed
    event = PrecipitationEvent(
        event_id="ev-other",
        event_type="task.replan",  # 不匹配
        payload={},
    )
    updates = await kp.dispatch(event)
    assert len(updates) == 0
