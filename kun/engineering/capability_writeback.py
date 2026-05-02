"""Capability card writeback — 把任务结果回写到能力卡.

ADR-018 §16.4 KnowledgePrecipitation 的一个 step:
每次任务完成 → 更新实体 (角色模板 / 模型) 在对应 task_type 上的统计.

运行时是即时 upsert; 置信区间、衰减权重等重算放 idle-batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import CapabilityCardRow
from kun.datamodel.capability import (
    Boundaries,
    Capability,
    CapabilityCard,
    DecayModel,
    EntityRef,
    EntityType,
    FailureMode,
    QualityMetrics,
    Stats,
)

log = get_logger("kun.engineering.capability_writeback")

Outcome = Literal["pass", "partial", "fail"]


@dataclass
class TaskOutcome:
    entity_type: EntityType
    entity_id: str
    task_type: str
    outcome: Outcome
    cost_usd: float
    duration_sec: float
    rubric_score: float | None = None
    consistency_score: float | None = None
    surprise_score: float | None = None
    failure_name: str | None = None
    failure_root_cause: str | None = None


async def record_outcome(tenant_id: str, outcome: TaskOutcome) -> None:
    """Upsert a capability card with a new task outcome."""
    for attempt in range(2):
        try:
            async with session_scope(tenant_id=tenant_id) as s:
                await _record_outcome_in_txn(s, tenant_id, outcome)
            break
        except IntegrityError:
            if attempt >= 1:
                raise
            log.info(
                "capability.writeback.retry_after_conflict",
                entity=f"{outcome.entity_type}:{outcome.entity_id}",
                task_type=outcome.task_type,
            )

    log.info(
        "capability.writeback",
        entity=f"{outcome.entity_type}:{outcome.entity_id}",
        task_type=outcome.task_type,
        outcome=outcome.outcome,
    )
    try:
        from kun.engineering.capability_cache import get_capability_card_cache

        get_capability_card_cache().invalidate(
            tenant_id=tenant_id,
            entity_type=outcome.entity_type,
            entity_id=outcome.entity_id,
        )
    except Exception:
        log.debug("capability.cache_invalidate_skipped", exc_info=True)


async def _record_outcome_in_txn(
    s: AsyncSession,
    tenant_id: str,
    outcome: TaskOutcome,
) -> None:
    """Apply one outcome inside a transaction.

    Existing rows are locked so concurrent writers serialize. First-write races
    are handled by the caller retrying after the unique constraint trips.
    """
    row = (
        await s.execute(
            _select_card_for_update(
                tenant_id=tenant_id,
                entity_type=outcome.entity_type,
                entity_id=outcome.entity_id,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        card = _new_card(outcome)
        s.add(_card_to_row(tenant_id, card))
        await s.flush()
        return

    card = _row_to_card(row)
    _apply_outcome(card, outcome)
    row.version = (row.version or 0) + 1
    card.version = row.version
    card.recompute_summary()
    row.maturity = card.maturity
    row.overall_reliability = card.overall_reliability
    row.primary_strength = card.primary_strength
    row.primary_weakness = card.primary_weakness
    row.card_json = card.model_dump(mode="json")
    row.last_updated = datetime.now(UTC)


def _select_card_for_update(
    *,
    tenant_id: str,
    entity_type: EntityType,
    entity_id: str,
) -> Any:
    """Build the capability-card row lock query."""
    return (
        select(CapabilityCardRow)
        .where(
            CapabilityCardRow.tenant_id == tenant_id,
            CapabilityCardRow.entity_type == entity_type,
            CapabilityCardRow.entity_id == entity_id,
        )
        .with_for_update()
    )


def _new_card(outcome: TaskOutcome) -> CapabilityCard:
    card = CapabilityCard(
        entity_ref=EntityRef(entity_type=outcome.entity_type, entity_id=outcome.entity_id),
        capabilities=[],
    )
    _apply_outcome(card, outcome)
    card.recompute_summary()
    return card


def _apply_outcome(card: CapabilityCard, outcome: TaskOutcome) -> None:
    cap = card.find(outcome.task_type)
    if cap is None:
        cap = Capability(
            task_type=outcome.task_type,
            stats=Stats(),
            quality=QualityMetrics(),
            decay=DecayModel(half_life_days=30),
            boundaries=Boundaries(),
        )
        card.capabilities.append(cap)

    # Update counts
    cap.stats.total_invocations += 1
    if outcome.outcome == "pass":
        cap.stats.success_count += 1
    elif outcome.outcome == "partial":
        cap.stats.partial_success_count += 1
    else:
        cap.stats.failure_count += 1
    cap.stats.recompute_rate()

    # Rolling average cost + duration (simple incremental mean)
    n = cap.stats.total_invocations
    cap.stats.avg_cost_usd = ((n - 1) * cap.stats.avg_cost_usd + outcome.cost_usd) / n
    cap.stats.avg_duration_sec = ((n - 1) * cap.stats.avg_duration_sec + outcome.duration_sec) / n
    cap.stats.duration_p95 = max(cap.stats.duration_p95, outcome.duration_sec)
    cap.stats.duration_p99 = max(cap.stats.duration_p99, outcome.duration_sec)

    # Quality
    if outcome.rubric_score is not None:
        cap.quality.avg_rubric_score = (
            (n - 1) * cap.quality.avg_rubric_score + outcome.rubric_score
        ) / n
    if outcome.consistency_score is not None:
        cap.quality.consistency_score = outcome.consistency_score
    if outcome.surprise_score is not None:
        # surprise_rate = EMA over last k=20
        alpha = 1.0 / 20
        cap.quality.surprise_rate = (1 - alpha) * cap.quality.surprise_rate + alpha * float(
            outcome.surprise_score > 0.6
        )

    # Failure mode
    if outcome.outcome == "fail" and outcome.failure_name:
        existing = next((f for f in cap.failure_modes if f.name == outcome.failure_name), None)
        if existing is None:
            cap.failure_modes.append(
                FailureMode(
                    name=outcome.failure_name,
                    frequency=1,
                    last_occurred=datetime.now(UTC),
                    typical_root_cause=outcome.failure_root_cause or "",
                )
            )
        else:
            existing.frequency += 1
            existing.last_occurred = datetime.now(UTC)
            if outcome.failure_root_cause:
                existing.typical_root_cause = outcome.failure_root_cause

    # Effective sample size (fresh write → +1 at max weight)
    cap.decay.effective_sample_size += 1.0


def _card_to_row(tenant_id: str, card: CapabilityCard) -> CapabilityCardRow:
    return CapabilityCardRow(
        card_id=card.card_id,
        tenant_id=tenant_id,
        entity_type=card.entity_ref.entity_type,
        entity_id=card.entity_ref.entity_id,
        version=card.version,
        maturity=card.maturity,
        overall_reliability=card.overall_reliability,
        primary_strength=card.primary_strength,
        primary_weakness=card.primary_weakness,
        card_json=card.model_dump(mode="json"),
        created_at=card.created_at,
        last_updated=card.last_updated,
    )


def _row_to_card(row: CapabilityCardRow) -> CapabilityCard:
    data = dict(row.card_json or {})
    # Best-effort reconstruction; missing fields get defaults from Pydantic
    data.setdefault(
        "entity_ref",
        {"entity_type": row.entity_type, "entity_id": row.entity_id},
    )
    data["version"] = row.version
    data["maturity"] = row.maturity
    data["overall_reliability"] = row.overall_reliability
    return CapabilityCard.model_validate(data)
