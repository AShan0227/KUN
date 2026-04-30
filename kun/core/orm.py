"""SQLAlchemy ORM models.

All business tables carry `tenant_id` (ADR-007). Tenant isolation is enforced
in both application queries and Postgres RLS policies.

The `events` table is the Outbox (ADR-005).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
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
        CheckConstraint("estimated_cost_usd >= 0", name="estimated_cost_nonnegative"),
        CheckConstraint("estimated_duration_sec >= 0", name="estimated_duration_nonnegative"),
        CheckConstraint("version >= 1", name="task_version_positive"),
        CheckConstraint("length(success_criteria_short) > 0", name="success_criteria_not_empty"),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_tasks_fingerprint"),
        Index("ix_tasks_tenant_type", "tenant_id", "task_type"),
    )


# ============== MISSIONS ==============


class MissionRow(Base):
    """Long-horizon mission: one real-world goal spanning many tasks."""

    __tablename__ = "missions"

    mission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    budget_cap_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    budget_used_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    blocked_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_step_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    review_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    success_metrics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    strategy_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name="mission_status_valid",
        ),
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="mission_risk_level_valid",
        ),
        CheckConstraint("budget_cap_usd >= 0", name="mission_budget_nonnegative"),
        CheckConstraint("budget_used_usd >= 0", name="mission_budget_used_nonnegative"),
        CheckConstraint(
            "review_interval_hours > 0",
            name="mission_review_interval_positive",
        ),
        CheckConstraint("length(title) > 0", name="mission_title_not_empty"),
        CheckConstraint("length(objective) > 0", name="mission_objective_not_empty"),
        Index("ix_missions_tenant_status", "tenant_id", "status"),
        Index("ix_missions_tenant_project", "tenant_id", "project_id"),
    )


class MissionTaskRow(Base):
    """Link table between a mission and durable tasks."""

    __tablename__ = "mission_tasks"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.mission_id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="primary")
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", index=True)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    resume_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_resume_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'queued', 'running', 'paused', 'blocked', 'done', "
            "'failed', 'cancelled')",
            name="mission_task_status_valid",
        ),
        CheckConstraint("sequence_no >= 0", name="mission_task_sequence_nonnegative"),
        CheckConstraint("resume_attempts >= 0", name="mission_task_resume_attempts_nonnegative"),
        Index("ix_mission_tasks_tenant_task", "tenant_id", "task_id"),
        Index("ix_mission_tasks_tenant_status", "tenant_id", "mission_id", "status"),
    )


class MissionMilestoneRow(Base):
    """Mission-level milestone or checkpoint visible to humans and KUN."""

    __tablename__ = "mission_milestones"

    milestone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.mission_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    completed_by_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'active', 'done', 'blocked', 'cancelled')",
            name="mission_milestone_status_valid",
        ),
        CheckConstraint("sequence_no >= 0", name="mission_milestone_sequence_nonnegative"),
        CheckConstraint("length(title) > 0", name="mission_milestone_title_not_empty"),
        Index("ix_mission_milestones_tenant_mission", "tenant_id", "mission_id", "status"),
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
        CheckConstraint(
            "accumulated_cost_usd_actual >= 0",
            name="runtime_actual_cost_nonnegative",
        ),
        CheckConstraint(
            "accumulated_cost_usd_equivalent >= 0",
            name="runtime_equivalent_cost_nonnegative",
        ),
        CheckConstraint("accumulated_tokens >= 0", name="runtime_tokens_nonnegative"),
        CheckConstraint("failures_this_run >= 0", name="runtime_failures_nonnegative"),
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
        CheckConstraint("cost_usd_actual >= 0", name="task_result_actual_cost_nonnegative"),
        CheckConstraint(
            "cost_usd_equivalent >= 0",
            name="task_result_equivalent_cost_nonnegative",
        ),
        CheckConstraint("tokens_in >= 0", name="task_result_tokens_in_nonnegative"),
        CheckConstraint("tokens_out >= 0", name="task_result_tokens_out_nonnegative"),
        CheckConstraint("duration_sec >= 0", name="task_result_duration_nonnegative"),
        CheckConstraint(
            "surprise_score >= 0 AND surprise_score <= 1",
            name="task_result_surprise_score_range",
        ),
        Index("ix_task_results_tenant_task", "tenant_id", "task_id"),
    )


# ============== PENDING SIDE-EFFECT ACTIONS ==============


class PendingActionRow(Base):
    """Side-effect action queue; actions wait here before external execution."""

    __tablename__ = "pending_actions"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    task_ref: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_ref: Mapped[str] = mapped_column(String(256), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending_approval",
        index=True,
    )
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'executed', 'cancelled')",
            name="pending_action_status_valid",
        ),
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="pending_action_risk_level_valid",
        ),
        Index("ix_pending_actions_tenant_status", "tenant_id", "status"),
    )


class WorldHandlerControlRow(Base):
    """Tenant-scoped persistent control state for one WorldGateway handler."""

    __tablename__ = "world_handler_controls"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="enabled", index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="nuo")
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('enabled', 'quarantined', 'disabled')",
            name="world_handler_control_status_valid",
        ),
        CheckConstraint("length(action_type) > 0", name="world_handler_control_action_not_empty"),
        Index("ix_world_handler_controls_tenant_status", "tenant_id", "status"),
    )


class WorldActionExecutionRow(Base):
    """Durable execution ledger for approved WorldGateway side effects."""

    __tablename__ = "world_action_executions"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_ref: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_ref: Mapped[str] = mapped_column(String(256), nullable=False, default="unknown")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="claimed", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    handler_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    gateway_mode: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    capability_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    external_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_handler: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    artifact_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    compensation_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_policy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    decision_ticket_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )
    first_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('claimed', 'executed', 'blocked', 'failed', 'cancelled')",
            name="world_action_execution_status_valid",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="world_action_execution_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "length(idempotency_key) > 0",
            name="world_action_execution_idempotency_not_empty",
        ),
        Index("ix_world_action_executions_tenant_status", "tenant_id", "status"),
        Index("ix_world_action_executions_tenant_action_type", "tenant_id", "action_type"),
        Index("ix_world_action_executions_task_ref", "task_ref"),
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
        CheckConstraint(
            "entity_type IN ('role_template', 'model', 'skill', 'tool', 'human', 'external_agent')",
            name="capability_entity_type_valid",
        ),
        CheckConstraint(
            "maturity IN ('cold_start', 'warming_up', 'mature')",
            name="capability_maturity_valid",
        ),
        CheckConstraint("version >= 1", name="capability_version_positive"),
        CheckConstraint(
            "overall_reliability >= 0 AND overall_reliability <= 1",
            name="capability_reliability_range",
        ),
        UniqueConstraint("tenant_id", "entity_type", "entity_id", name="uq_capability_entity"),
    )


# ============== RESOURCE CREDIT STATS ==============


class ResourceCreditRow(Base):
    """Durable MoE credit stats for resources used during task execution."""

    __tablename__ = "resource_credit_stats"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[str] = mapped_column(String(192), nullable=False)

    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credit_total: Mapped[float] = mapped_column(nullable=False, default=0.0)

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        onupdate=_utcnow,
    )

    __table_args__ = (
        CheckConstraint("length(resource_key) > 0", name="resource_credit_key_not_empty"),
        CheckConstraint("length(resource_kind) > 0", name="resource_credit_kind_not_empty"),
        CheckConstraint("length(resource_id) > 0", name="resource_credit_id_not_empty"),
        CheckConstraint("used_count >= 0", name="resource_credit_used_nonnegative"),
        CheckConstraint("pass_count >= 0", name="resource_credit_pass_nonnegative"),
        CheckConstraint("critical_count >= 0", name="resource_credit_critical_nonnegative"),
        CheckConstraint("credit_total >= 0", name="resource_credit_total_nonnegative"),
        Index("ix_resource_credit_tenant_kind", "tenant_id", "resource_kind"),
        Index("ix_resource_credit_tenant_resource", "tenant_id", "resource_kind", "resource_id"),
    )


# ============== ENTITY RELATIONSHIPS ==============


class EntityRelationshipRow(Base):
    """Knowledge graph relationship edge between two tenant-scoped entities."""

    __tablename__ = "entity_relationships"

    relation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_entity_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_entity_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.3)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pheromone_strength: Mapped[float] = mapped_column(nullable=False, default=0.0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_reinforced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="entity_relationship_confidence_range",
        ),
        CheckConstraint(
            "evidence_count >= 0",
            name="entity_relationship_evidence_nonnegative",
        ),
        CheckConstraint(
            "pheromone_strength >= 0 AND pheromone_strength <= 1",
            name="entity_relationship_pheromone_range",
        ),
        CheckConstraint(
            "relation_type IN ("
            "'depends_on','mentions','verifies','contradicts','similar_to',"
            "'co_occurs','produced_by','transfer_confidence'"
            ")",
            name="entity_relationship_type_valid",
        ),
        Index(
            "ix_relationships_tenant_source",
            "tenant_id",
            "source_entity_kind",
            "source_entity_id",
        ),
        Index(
            "ix_relationships_tenant_target",
            "tenant_id",
            "target_entity_kind",
            "target_entity_id",
        ),
    )


# ============== PROACTIVE TOOL LEARNING ==============


class ProactiveMissRow(Base):
    __tablename__ = "proactive_misses"

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_missed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "skill_id", "pattern", name="pk_proactive_misses"),
        CheckConstraint("miss_count >= 0", name="proactive_misses_count_nonnegative"),
        Index("ix_proactive_misses_tenant_promoted", "tenant_id", "promoted_at"),
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

    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'insight', 'warn', 'error')",
            name="notification_severity_valid",
        ),
        CheckConstraint(
            "channel IN ('main', 'side', 'email', 'webhook', 'push', 'silent')",
            name="notification_channel_valid",
        ),
    )


# ============== EXPERIMENTS (ADR-009) ==============


class ExperimentRow(Base):
    __tablename__ = "experiments"

    # Composite PK (tenant_id, id) — two tenants may pick the same experiment
    # id without colliding; updates that filter on id alone become tenant-safe
    # at the DB layer because the unique constraint is per tenant.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
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


# ============== KUN-LAB EXPERIMENT LOG ==============


class LabExperimentRow(Base):
    __tablename__ = "lab_experiments"

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    prompt_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    ensemble_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )


# ============== QI PROBLEM SIGNALS ==============


class QiProblemSignalRow(Base):
    """Durable problem queue for Qi exploration inputs."""

    __tablename__ = "qi_problem_signals"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    category: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    task_type: Mapped[str] = mapped_column(String(128), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint("length(summary) > 0", name="qi_problem_summary_not_empty"),
        CheckConstraint("occurrence_count >= 1", name="qi_problem_occurrence_positive"),
        CheckConstraint(
            "status IN ('open', 'consumed', 'dismissed')",
            name="qi_problem_status_valid",
        ),
        Index("ix_qi_problem_tenant_status", "tenant_id", "status"),
        Index("ix_qi_problem_tenant_category", "tenant_id", "category"),
        Index("ix_qi_problem_tenant_last_seen", "tenant_id", "last_seen_at"),
    )


# ============== TENANT ACCOUNT REGISTRY ==============


class TenantAccountRow(Base):
    """Operator-managed tenant/org record.

    This is the first production-accounting slice: it does not replace a full
    signup/billing product, but it makes tenant ownership and plan state
    durable instead of living only in env vars or one-off onboarding JSON.
    """

    __tablename__ = "tenant_accounts"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="dev")
    billing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'closed')",
            name="tenant_account_status_valid",
        ),
        CheckConstraint(
            "billing_status IN ('manual', 'trial', 'active', 'past_due', 'cancelled')",
            name="tenant_account_billing_status_valid",
        ),
        CheckConstraint("length(display_name) > 0", name="tenant_account_display_not_empty"),
        CheckConstraint("length(owner_user_id) > 0", name="tenant_account_owner_not_empty"),
        Index("ix_tenant_accounts_org_status", "organization_id", "status"),
    )


class TenantMemberRow(Base):
    """Durable tenant member and scope ledger."""

    __tablename__ = "tenant_members"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_accounts.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    invite_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invite_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invited_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="tenant_member_role_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'invited', 'disabled')",
            name="tenant_member_status_valid",
        ),
        Index("ix_tenant_members_tenant_status", "tenant_id", "status"),
        Index("ix_tenant_members_invite_expires", "tenant_id", "invite_expires_at"),
    )


class TenantTokenIssueRow(Base):
    """Audit ledger for operator-issued bearer tokens.

    The raw token is never stored.  ``token_hash`` lets ops correlate a leaked
    token with an issuance record later without keeping the secret itself.
    """

    __tablename__ = "tenant_token_issues"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_accounts.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    token_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    audience: Mapped[str] = mapped_column(String(16), nullable=False, default="developer")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="issued", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "audience IN ('novice', 'developer', 'expert')",
            name="tenant_token_audience_valid",
        ),
        CheckConstraint(
            "status IN ('issued', 'revoked')",
            name="tenant_token_status_valid",
        ),
        Index("ix_tenant_tokens_tenant_status", "tenant_id", "status"),
        Index("ix_tenant_tokens_tenant_user", "tenant_id", "user_id"),
    )


# ============== IDEMPOTENCY KEYS ==============


class IdempotencyRow(Base):
    __tablename__ = "idempotency_keys"

    # Composite PK (tenant_id, key) — two tenants asking the same prompt
    # (same fingerprint) keep separate idempotency state; without this two
    # tenants colliding on a fingerprint would race the second INSERT into
    # an IntegrityError that the orchestrator can't recover from.
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    ttl_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    __table_args__ = (CheckConstraint("ttl_sec > 0", name="idempotency_ttl_positive"),)


# ============== SOUL FILE (V2.1 §13 / T17 / M4 持久化) ==============


class SoulFileRow(Base):
    """user 级灵魂档案持久化.

    主体字段 (audience / risk_tolerance / etc) 拍平方便查询; nested 字段
    (revision_history / evolved_traits / preferred_tools / extensions / etc)
    存 blob JSONB.

    复合 PK (tenant_id, user_id) — 同一 user 在不同 tenant 互相隔离.
    """

    __tablename__ = "soul_files"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 拍平的主体字段 (常用于查询 / NUO 显示)
    audience: Mapped[str] = mapped_column(String(16), nullable=False, default="developer")
    default_language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    risk_tolerance: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    cost_sensitivity: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    speed_sensitivity: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    interruption_tolerance: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    approval_threshold_money: Mapped[float] = mapped_column(nullable=False, default=10.0)
    professional_role: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # 整个 SoulFile pydantic dump (含 revisions / evolved_traits / preferred_tools / etc)
    blob: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "audience IN ('novice', 'developer', 'expert')",
            name="soul_file_audience_valid",
        ),
        CheckConstraint(
            "risk_tolerance IN ('low', 'medium', 'high')",
            name="soul_file_risk_valid",
        ),
        CheckConstraint(
            "cost_sensitivity IN ('low', 'medium', 'high')",
            name="soul_file_cost_valid",
        ),
        CheckConstraint(
            "speed_sensitivity IN ('low', 'medium', 'high')",
            name="soul_file_speed_valid",
        ),
        CheckConstraint(
            "interruption_tolerance IN ('low', 'medium', 'high')",
            name="soul_file_interrupt_valid",
        ),
        CheckConstraint(
            "approval_threshold_money >= 0",
            name="soul_file_approval_threshold_nonneg",
        ),
        Index("ix_soul_files_tenant_updated", "tenant_id", "last_updated"),
    )
