"""SQLAlchemy ORM models.

All business tables carry `tenant_id` (ADR-007). Tenant isolation is currently
enforced by application queries and tests; database RLS policies are planned
but not installed yet.

The `events` table is the Outbox (ADR-005).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kun.core.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ============== EVENTS OUTBOX (ADR-005) ==============


class EventRow(Base):
    """Outbox 表 — 唯一真理源."""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        index=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    causation_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    __table_args__ = (
        # Partial index for the outbox poller
        Index("ix_events_unpublished", "event_id", postgresql_where="published_at IS NULL"),
        Index("ix_events_tenant_time", "tenant_id", "occurred_at"),
    )


# ============== TASKS ==============


class TaskRow(Base):
    """TASK.md L1 + serialized L2 JSONB."""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    complexity_score: Mapped[float] = mapped_column(nullable=False, default=0.0)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Layer 1 fields
    estimated_cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    estimated_duration_sec: Mapped[float] = mapped_column(nullable=False, default=0.0)
    deadline_iso: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success_criteria_short: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Serialized Layer 2
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    layer3_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        # Idempotency: within time_window_min same fingerprint + tenant = same task
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="risk_level_valid",
        ),
        CheckConstraint(
            "complexity_score >= 0 AND complexity_score <= 1",
            name="complexity_score_range",
        ),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_tasks_fingerprint"),
        Index("ix_tasks_tenant_type", "tenant_id", "task_type"),
    )


# ============== RUNTIME STATE ==============


class RuntimeStateRow(Base):
    __tablename__ = "runtime_states"

    state_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_ref: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_planned_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)

    accumulated_cost_usd_actual: Mapped[float] = mapped_column(nullable=False, default=0.0)
    accumulated_cost_usd_equivalent: Mapped[float] = mapped_column(nullable=False, default=0.0)
    accumulated_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    failures_this_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Serialize the rest as JSONB — low-churn fields.
    blob: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        onupdate=_utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name="runtime_status_valid",
        ),
        CheckConstraint("current_step >= 0", name="runtime_current_step_nonnegative"),
        CheckConstraint("total_planned_steps >= 0", name="runtime_total_steps_nonnegative"),
    )


# ============== TASK RESULTS ==============


class TaskResultRow(Base):
    """Final task result cache for idempotent API/WebSocket replies."""

    __tablename__ = "task_results"

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cost_usd_actual: Mapped[float] = mapped_column(nullable=False, default=0.0)
    cost_usd_equivalent: Mapped[float] = mapped_column(nullable=False, default=0.0)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_sec: Mapped[float] = mapped_column(nullable=False, default=0.0)
    surprise_score: Mapped[float] = mapped_column(nullable=False, default=0.0)

    notifications_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    result_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name="task_result_status_valid",
        ),
        Index("ix_task_results_tenant_task", "tenant_id", "task_id"),
    )


# ============== CAPABILITY CARDS ==============


class CapabilityCardRow(Base):
    __tablename__ = "capability_cards"

    card_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    maturity: Mapped[str] = mapped_column(String(16), nullable=False, default="cold_start")
    overall_reliability: Mapped[float] = mapped_column(nullable=False, default=0.0)
    primary_strength: Mapped[str | None] = mapped_column(String(128), nullable=True)
    primary_weakness: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # capabilities serialized
    card_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_type", "entity_id", name="uq_capability_entity"),
    )


# ============== HANDOFF PACKETS ==============


class HandoffRow(Base):
    __tablename__ = "handoffs"

    packet_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_entity: Mapped[str] = mapped_column(String(128), nullable=False)
    to_entity: Mapped[str] = mapped_column(String(128), nullable=False)

    # L1+L2 inline; L3/L4 as refs
    l1_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    l2_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    l3_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    l4_refs: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


# ============== NOTIFICATIONS ==============


class NotificationRow(Base):
    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="side")
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    render_hint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    task_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    causation_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ============== EXPERIMENTS (ADR-009) ==============


class ExperimentRow(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    rollout_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    control_variant: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    treatment_variant: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'shadow', 'canary', 'rollout', 'stable', 'rolled_back')",
            name="experiment_status_valid",
        ),
        CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="experiment_rollout_percent_range",
        ),
    )


# ============== IDEMPOTENCY KEYS ==============


class IdempotencyRow(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
