"""KnowledgePrecipitation — 自我进化统一架构 (V2.1 §16.12 / ADR-025).

合并 5 个机制 (V1 §16.4 已有抽象, V2.1 强化):
- §17.9 策略自我进化 (候选库 / 权重表 / 规则库)
- §6.4 idle-batch 7 step
- §17.6 反馈回写
- §8.9 评估闭环决策层
- §20.2 内反馈闭环

4 类 PrecipitationStep:
- stats_writeback (realtime): 实时统计写入
- weight_tune (weekly): 权重表 / 阈值微调
- rule_emerge (weekly): 新规则涌现 / 旧规则归档
- narrative_distill (daily): 经验蒸馏

所有进化走同一管道, 同一审计回滚链路 (§16.6 GuardPolicy).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


PrecipitationKind = Literal[
    "stats_writeback",  # realtime
    "weight_tune",  # weekly
    "rule_emerge",  # weekly
    "narrative_distill",  # daily
]

PrecipitationSchedule = Literal["realtime", "hourly", "daily", "weekly"]


@dataclass
class AssetUpdate:
    """进化产出的资产更新."""

    update_id: str
    asset_kind: str  # capability_card / playbook / rule / methodology
    asset_ref: str
    update_kind: Literal["create", "update", "delete", "promote", "rollback"]
    payload: dict[str, Any]
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    requires_approval: bool = False


@dataclass
class PrecipitationEvent:
    """触发进化的事件 (任务完成 / surprise 高 / 反馈到达 / etc)."""

    event_id: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PrecipitationStep(Protocol):
    """单步进化协议."""

    source_event_type: str
    step_kind: PrecipitationKind
    schedule: PrecipitationSchedule

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        """从一个事件产出资产更新."""
        ...


class KnowledgePrecipitation:
    """统一进化管道 (§16.12).

    用法:
        kp = KnowledgePrecipitation()
        kp.register_step(MyStep())
        await kp.dispatch(event)
        # 同时 idle-batch 周期跑:
        await kp.run_scheduled("daily")
    """

    def __init__(self) -> None:
        self._steps: list[PrecipitationStep] = []
        self._asset_apply_hook: Callable[[AssetUpdate], Awaitable[None]] | None = None
        self._audit_log: list[tuple[str, AssetUpdate]] = []  # (step_kind, update)
        self._scheduled_queue: dict[PrecipitationSchedule, list[PrecipitationEvent]] = {
            "realtime": [],
            "hourly": [],
            "daily": [],
            "weekly": [],
        }

    def register_step(self, step: PrecipitationStep) -> None:
        self._steps.append(step)

    def register_asset_apply_hook(self, hook: Callable[[AssetUpdate], Awaitable[None]]) -> None:
        """资产更新真正应用的钩子 (走 §16.6 GuardPolicy 审计回滚)."""
        self._asset_apply_hook = hook

    async def dispatch(
        self,
        event: PrecipitationEvent,
        context: dict[str, Any] | None = None,
    ) -> list[AssetUpdate]:
        """事件来时分发到匹配的 step.

        - realtime: 立即跑
        - hourly/daily/weekly: 入队, 等 run_scheduled 跑
        """
        all_updates: list[AssetUpdate] = []
        for step in self._steps:
            if step.source_event_type != event.event_type:
                continue
            if step.schedule == "realtime":
                try:
                    updates = await step.precipitate(event, context)
                    for u in updates:
                        await self._apply(u, step.step_kind)
                        all_updates.append(u)
                except Exception:
                    logger.exception("step %s failed (non-fatal)", step.step_kind)
            else:
                self._scheduled_queue[step.schedule].append(event)
        return all_updates

    async def run_scheduled(
        self,
        schedule: PrecipitationSchedule,
        context: dict[str, Any] | None = None,
    ) -> list[AssetUpdate]:
        """idle-batch 周期跑 (hourly / daily / weekly)."""
        all_updates: list[AssetUpdate] = []
        events = list(self._scheduled_queue[schedule])
        self._scheduled_queue[schedule] = []
        matching_steps = [s for s in self._steps if s.schedule == schedule]
        for event in events:
            for step in matching_steps:
                if step.source_event_type != event.event_type:
                    continue
                try:
                    updates = await step.precipitate(event, context)
                    for u in updates:
                        await self._apply(u, step.step_kind)
                        all_updates.append(u)
                except Exception:
                    logger.exception("scheduled step %s failed (non-fatal)", step.step_kind)
        return all_updates

    async def _apply(self, update: AssetUpdate, step_kind: str) -> None:
        """走 GuardPolicy 审计回滚."""
        self._audit_log.append((step_kind, update))
        if self._asset_apply_hook is not None:
            try:
                await self._asset_apply_hook(update)
            except Exception:
                logger.exception("asset_apply_hook failed for %s", update.update_id)

    def get_audit_log(self) -> list[tuple[str, AssetUpdate]]:
        return list(self._audit_log)


# ============================================================================
# 4 个内置 PrecipitationStep (示例 + 默认接入)
# ============================================================================


class StatsWritebackStep:
    """stats_writeback (realtime): 任务完成 → 写 capability_card 实时统计."""

    source_event_type = "task.completed"
    step_kind: PrecipitationKind = "stats_writeback"
    schedule: PrecipitationSchedule = "realtime"

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        from kun.core.ids import new_id

        payload = event.payload
        return [
            AssetUpdate(
                update_id=new_id("score"),
                asset_kind="capability_card",
                asset_ref=str(payload.get("entity_id", "unknown")),
                update_kind="update",
                payload={
                    "task_type": payload.get("task_type"),
                    "outcome": payload.get("outcome"),
                    "cost_usd": payload.get("cost_usd"),
                    "latency_sec": payload.get("latency_sec"),
                },
            )
        ]


class WeightTuneStep:
    """weight_tune (weekly): idle-batch 回归分析微调权重表."""

    source_event_type = "decision.completed"
    step_kind: PrecipitationKind = "weight_tune"
    schedule: PrecipitationSchedule = "weekly"

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        from kun.core.ids import new_id

        # 实际实现: 跑回归分析, 产出权重微调建议
        # 这里 stub: 只发 audit, 不真改权重 (避免破坏稳定)
        payload = event.payload
        return [
            AssetUpdate(
                update_id=new_id("score"),
                asset_kind="weight_table",
                asset_ref="strategy_score_weights",
                update_kind="update",
                payload={
                    "decision_kind": payload.get("decision_kind"),
                    "weight_adjustment_suggestion": payload.get("weight_delta", {}),
                },
                requires_approval=True,  # 权重改动需审批
            )
        ]


class RuleEmergeStep:
    """rule_emerge (weekly): 涌现新规则 / 归档旧规则."""

    source_event_type = "task.replan"
    step_kind: PrecipitationKind = "rule_emerge"
    schedule: PrecipitationSchedule = "weekly"

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        from kun.core.ids import new_id

        # 实际实现: 聚类分析 N 次 replan 找共同模式 → 提议新规则
        # 这里 stub: 只发 audit, 新规则进 shadow 模式
        return [
            AssetUpdate(
                update_id=new_id("rule"),
                asset_kind="rule",
                asset_ref="learned_rules",
                update_kind="create",
                payload={
                    "rule_kind": "guard",
                    "status": "shadow",  # 走 §8.3 渐进部署
                    "source_events": [event.event_id],
                },
                requires_approval=True,
            )
        ]


class NarrativeDistillStep:
    """narrative_distill (daily): 经验蒸馏 (情节→语义→方法论)."""

    source_event_type = "task.completed"
    step_kind: PrecipitationKind = "narrative_distill"
    schedule: PrecipitationSchedule = "daily"

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        from kun.core.ids import new_id

        # surprise_score 高的任务才进方法论
        surprise = float(event.payload.get("surprise_score", 0.0))
        if surprise < 0.6:
            return []
        return [
            AssetUpdate(
                update_id=new_id("memory"),
                asset_kind="methodology",
                asset_ref=f"distilled-{event.event_id}",
                update_kind="create",
                payload={
                    "source_task_id": event.payload.get("task_id"),
                    "surprise_score": surprise,
                    "lesson_text": event.payload.get("lesson_text", ""),
                },
            )
        ]


__all__ = [
    "AssetUpdate",
    "KnowledgePrecipitation",
    "NarrativeDistillStep",
    "PrecipitationEvent",
    "PrecipitationKind",
    "PrecipitationSchedule",
    "PrecipitationStep",
    "RuleEmergeStep",
    "StatsWritebackStep",
    "WeightTuneStep",
]
