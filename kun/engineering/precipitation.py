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
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


PrecipitationKind = Literal[
    "stats_writeback",  # realtime
    "weight_tune",  # weekly
    "rule_emerge",  # weekly
    "narrative_distill",  # daily
    "relationship_mine",  # daily
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
        from kun.datamodel.relationship import EntityRelationship

        payload = event.payload
        updates = [
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

        tenant_id = str(payload.get("tenant_id") or (context or {}).get("tenant_id") or "")
        source_strategy = payload.get("source_strategy_id") or payload.get("anchor_strategy_id")
        target_strategy = payload.get("target_strategy_id") or payload.get("winner_strategy")
        confidence_raw = payload.get("transfer_confidence")
        if tenant_id and isinstance(source_strategy, str) and isinstance(target_strategy, str):
            confidence = _clamp_confidence(confidence_raw, default=0.3)
            evidence_count = _positive_int(payload.get("evidence_count"), default=1)
            relationship = EntityRelationship(
                tenant_id=tenant_id,
                source_entity_kind="strategy",
                source_entity_id=source_strategy,
                target_entity_kind="strategy",
                target_entity_id=target_strategy,
                relation_type="transfer_confidence",
                confidence=confidence,
                evidence_count=evidence_count,
                metadata={
                    "source": "WeightTuneStep",
                    "decision_kind": payload.get("decision_kind"),
                    "source_task_type": payload.get("source_task_type"),
                    "target_task_type": payload.get("target_task_type"),
                },
            )
            await _upsert_mined_relationship(relationship)
            updates.append(
                AssetUpdate(
                    update_id=new_id("memory"),
                    asset_kind="entity_relationship",
                    asset_ref=relationship.relation_id,
                    update_kind="create",
                    payload=relationship.model_dump(mode="json"),
                    confidence=relationship.confidence,
                )
            )
        return updates


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


class RelationshipMineStep:
    """relationship_mine (daily): scan recent events for knowledge graph edges."""

    source_event_type = "task.completed"
    step_kind: PrecipitationKind = "relationship_mine"
    schedule: PrecipitationSchedule = "daily"

    async def precipitate(
        self, event: PrecipitationEvent, context: dict[str, Any] | None = None
    ) -> list[AssetUpdate]:
        from kun.core.ids import new_id
        from kun.datamodel.relationship import EntityRelationship, confidence_for_evidence

        tenant_id = str(event.payload.get("tenant_id") or (context or {}).get("tenant_id") or "")
        if not tenant_id:
            return []

        events = await self._load_recent_events(event, tenant_id, context or {})
        if not events:
            return []

        co_occurrence_counts: Counter[tuple[tuple[str, str], tuple[str, str]]] = Counter()
        temporal_counts: Counter[tuple[tuple[str, str], tuple[str, str]]] = Counter()

        event_entities: list[tuple[datetime, list[tuple[str, str]]]] = []
        for recent in events:
            entities = sorted(set(_extract_entity_refs(recent.payload)))
            if entities:
                event_entities.append((recent.occurred_at, entities))
            for idx, source in enumerate(entities):
                for target in entities[idx + 1 :]:
                    co_occurrence_counts[(source, target)] += 1
                    co_occurrence_counts[(target, source)] += 1

        sorted_event_entities = sorted(event_entities, key=lambda item: item[0])
        for idx, (source_time, sources) in enumerate(sorted_event_entities):
            for target_time, targets in sorted_event_entities[idx + 1 :]:
                if target_time - source_time > timedelta(hours=1):
                    break
                for source in sources:
                    for target in targets:
                        if source != target:
                            temporal_counts[(source, target)] += 1

        updates: list[AssetUpdate] = []
        emitted_relationships: list[EntityRelationship] = []
        for (source, target), count in co_occurrence_counts.items():
            if count < 2:
                continue
            relationship = EntityRelationship(
                tenant_id=tenant_id,
                source_entity_kind=source[0],
                source_entity_id=source[1],
                target_entity_kind=target[0],
                target_entity_id=target[1],
                relation_type="co_occurs",
                confidence=confidence_for_evidence(count),
                evidence_count=count,
                metadata={"source": "RelationshipMineStep", "window_hours": 24},
            )
            await _upsert_mined_relationship(relationship)
            emitted_relationships.append(relationship)
            updates.append(
                AssetUpdate(
                    update_id=new_id("memory"),
                    asset_kind="entity_relationship",
                    asset_ref=relationship.relation_id,
                    update_kind="create",
                    payload=relationship.model_dump(mode="json"),
                    confidence=relationship.confidence,
                )
            )

        for (source, target), count in temporal_counts.items():
            if count < 2:
                continue
            relationship = EntityRelationship(
                tenant_id=tenant_id,
                source_entity_kind=source[0],
                source_entity_id=source[1],
                target_entity_kind=target[0],
                target_entity_id=target[1],
                relation_type="produced_by",
                confidence=confidence_for_evidence(count),
                evidence_count=count,
                metadata={"source": "RelationshipMineStep", "window_hours": 24, "lag_hours": 1},
            )
            await _upsert_mined_relationship(relationship)
            emitted_relationships.append(relationship)
            updates.append(
                AssetUpdate(
                    update_id=new_id("memory"),
                    asset_kind="entity_relationship",
                    asset_ref=relationship.relation_id,
                    update_kind="create",
                    payload=relationship.model_dump(mode="json"),
                    confidence=relationship.confidence,
                )
            )

        if emitted_relationships:
            try:
                from kun.context.graph_metrics import emit_relationship_mine_metrics

                emit_relationship_mine_metrics(emitted_relationships)
            except Exception:
                logger.debug("relationship_mine.metrics_emit_skipped", exc_info=True)

        return updates

    async def _load_recent_events(
        self,
        event: PrecipitationEvent,
        tenant_id: str,
        context: dict[str, Any],
    ) -> list[PrecipitationEvent]:
        injected_events = context.get("events")
        if injected_events is not None:
            recent_events: list[PrecipitationEvent] = []
            for item in injected_events:
                recent = _coerce_precipitation_event(item)
                if recent.occurred_at < event.occurred_at - timedelta(hours=24):
                    continue
                if str(recent.payload.get("tenant_id", tenant_id)) != tenant_id:
                    continue
                recent_events.append(recent)
            return recent_events

        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import EventRow

        since = event.occurred_at - timedelta(hours=24)
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(EventRow)
                .where(
                    EventRow.tenant_id == tenant_id,
                    EventRow.occurred_at >= since,
                )
                .order_by(EventRow.occurred_at)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [
            PrecipitationEvent(
                event_id=row.event_id,
                event_type=row.event_type,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]


def _coerce_precipitation_event(item: Any) -> PrecipitationEvent:
    if isinstance(item, PrecipitationEvent):
        return item
    payload = dict(item.get("payload", {}))
    return PrecipitationEvent(
        event_id=str(item.get("event_id", "event-inline")),
        event_type=str(item.get("event_type", "task.completed")),
        payload=payload,
        occurred_at=item.get("occurred_at", datetime.now(UTC)),
    )


def _extract_entity_refs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    raw_entities = payload.get("entities") or payload.get("entity_refs") or []
    entities: list[tuple[str, str]] = []
    for item in raw_entities:
        if isinstance(item, dict):
            kind = item.get("kind") or item.get("entity_kind") or item.get("entity_type")
            entity_id = item.get("id") or item.get("entity_id")
            if kind and entity_id:
                entities.append((str(kind), str(entity_id)))
        elif isinstance(item, str) and ":" in item:
            kind, entity_id = item.split(":", 1)
            if kind and entity_id:
                entities.append((kind, entity_id))
    for prefix in ("source", "target", "entity"):
        kind = payload.get(f"{prefix}_entity_kind") or payload.get(f"{prefix}_entity_type")
        entity_id = payload.get(f"{prefix}_entity_id")
        if kind and entity_id:
            entities.append((str(kind), str(entity_id)))
    return entities


def _clamp_confidence(value: Any, *, default: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(1.0, confidence))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


async def _upsert_mined_relationship(relationship: Any) -> None:
    from kun.datamodel.relationship import (
        add_relationship,
        get_relationships_from,
        reinforce_relationship,
    )

    existing = await get_relationships_from(
        relationship.source_entity_kind,
        relationship.source_entity_id,
        relationship.tenant_id,
        relation_types=[relationship.relation_type],
        min_confidence=0.0,
    )
    match = next(
        (
            rel
            for rel in existing
            if rel.target_entity_kind == relationship.target_entity_kind
            and rel.target_entity_id == relationship.target_entity_id
        ),
        None,
    )
    if match is None:
        await add_relationship(relationship)
        return
    await reinforce_relationship(
        match.relation_id,
        match.tenant_id,
        evidence_delta=relationship.evidence_count,
        confidence=relationship.confidence,
        metadata=relationship.metadata,
    )


__all__ = [
    "AssetUpdate",
    "KnowledgePrecipitation",
    "NarrativeDistillStep",
    "PrecipitationEvent",
    "PrecipitationKind",
    "PrecipitationSchedule",
    "PrecipitationStep",
    "RelationshipMineStep",
    "RuleEmergeStep",
    "StatsWritebackStep",
    "WeightTuneStep",
]
