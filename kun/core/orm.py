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
    Numeric,
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


# ============== RSI DATA SPINE (ADR-024, alembic 0011) ==============


class RuntimeCapabilityRow(Base):
    """已合入但默认未启用的候选能力. Gate 写, Executor 读.

    晋级状态机: merged → in_replay → in_shadow → in_canary → ready → enabled.
    超时未晋级 → expired (idle-batch 扫到后重审).
    """

    __tablename__ = "runtime_capabilities"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_module: Mapped[str] = mapped_column(String(256), nullable=False)
    change_summary: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    promotion_state: Mapped[str] = mapped_column(String(32), nullable=False, default="merged")
    promotion_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    promotion_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rollback_on: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    sampling_rate: Mapped[float] = mapped_column(
        Numeric(precision=5, scale=4), nullable=False, default=0
    )
    capability_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )


class RuntimeExperimentRow(Base):
    """Strategist 写入的候选实验. Executor 任务前读 → 应用 change_spec override.

    实验跑完 → Tester 出 TestReport → Gate 决定是否进 promotion_queue.
    """

    __tablename__ = "runtime_experiments"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_module: Mapped[str] = mapped_column(String(256), nullable=False)
    target_level: Mapped[int] = mapped_column(Integer, nullable=False)  # RCDH 层 0-3
    change_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rollout_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    sampling_rate: Mapped[float] = mapped_column(
        Numeric(precision=5, scale=4), nullable=False, default=0
    )
    success_metric: Mapped[str] = mapped_column(String(128), nullable=False)
    acceptance_threshold: Mapped[float] = mapped_column(Numeric, nullable=False)
    rollback_on: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class StrategySearchRequestRow(Base):
    """监督线写, Strategist 读. 异常信号 → 触发策略搜索.

    工程层做 1 小时 dedup_key 去重 (索引: tenant_id, dedup_key, created_at WHERE status='open').
    """

    __tablename__ = "strategy_search_requests"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    target_module: Mapped[str] = mapped_column(String(256), nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    dedup_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class DiagnosticRecordRow(Base):
    """RCDH 4 级诊断结果 (ADR-021). Supervisor 走 RCDH 时写; Gate 验诊断报告时读.

    scope_modules ≤ 5 (DB 层 check constraint 强制). 重复 ≥ 3 次同症状强制升 L0/L1.
    """

    __tablename__ = "diagnostic_records"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    diagnostic_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    triggered_by_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symptom_summary: Mapped[str] = mapped_column(Text, nullable=False)
    repeat_history_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    level_0_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    level_1_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    level_2_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    level_3_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    root_cause_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_modules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class GoalAnchorRow(Base):
    """长任务 GoalAnchor (ADR-022). Director 写, Executor 每次 LLM call 顶部 pin.

    immutable=True 默认; 不允许新指令覆盖. goal_statement ≤ 200 字符 (强制).
    """

    __tablename__ = "goal_anchors"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    anchor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    goal_statement: Mapped[str] = mapped_column(Text, nullable=False)
    success_criteria: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    out_of_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    invariants: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlanReviewRow(Base):
    """Anti-drift 长任务 Plan Review (ADR-022 Layer 4).

    Supervisor 每 3 步 / 5 分钟注入 → Executor 自评 → External Supervisor 独立 verify.
    """

    __tablename__ = "plan_reviews"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    triggered_at_step: Mapped[int] = mapped_column(Integer, nullable=False)
    triggered_at_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    executor_self_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    supervisor_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    drift_evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    external_supervisor_verify: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    action_taken: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class EvidenceLedgerRow(Base):
    """全链路证据账本 (ADR-024). Append-only.

    每条 entry 一种 kind: artifact / test_report / diagnostic / debrief / decision.
    可选关联 diagnostic_id + external_supervisor_debrief_id + diagnostic_level_reached.
    """

    __tablename__ = "evidence_ledger"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diagnostic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_supervisor_debrief_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diagnostic_level_reached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class BugRootCaseRow(Base):
    """Bug 根因案例库 (alembic 0013) — RCDH fast-path lookup.

    RCDH 4 级诊断从头分析每个 trace 慢. 这张表挂"案例库" — 同 trace_signature
    见过就直接返 fix_pattern, 没命中再走完整诊断. 命中即 O(1) 走捷径.

    Signature 生成: error_type + top 3 内部 frame (kun.* 优先 over site-packages)
    + SHA256 prefix. 详见 kun.governance.bug_case_library.trace_signature.

    工程语义:
      - (tenant_id, trace_signature) UNIQUE — 同 tenant 同 signature 只一条
      - hit_count + last_hit_at 在 lookup 命中后递增
      - evidence_dx_id 关联 diagnostic_records (如有完整诊断证据)
    """

    __tablename__ = "bug_root_cause_cases"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_signature: Mapped[str] = mapped_column(String(256), nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    root_cause_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    fix_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_dx_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_hit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("hit_count >= 1", name="bug_case_hit_count_positive"),
        CheckConstraint(
            "length(trace_signature) > 0",
            name="bug_case_signature_not_empty",
        ),
        CheckConstraint(
            "length(error_type) > 0",
            name="bug_case_error_type_not_empty",
        ),
        CheckConstraint(
            "length(root_cause_kind) > 0",
            name="bug_case_root_cause_kind_not_empty",
        ),
        CheckConstraint(
            "length(fix_pattern) > 0",
            name="bug_case_fix_pattern_not_empty",
        ),
        UniqueConstraint(
            "tenant_id", "trace_signature", name="ix_bug_cases_signature"
        ),
        Index("ix_bug_cases_hit_count", "tenant_id", "hit_count"),
    )


class TaskCheckpointRow(Base):
    """长任务执行 checkpoint (LT.C, ADR-022 持久化层).

    每个 Executor step 后落一条, 进程挂掉时按 sequence 取 latest active row,
    resume conversation_snapshot + working_state + artifact_refs.

    status:
      active        — 任务在跑, 可 resume
      final         — 任务正常完成, 最终 snapshot
      failed_resume — resume 时发现 snapshot 不一致 / 已 stale (e.g. anchor 已变)
    """

    __tablename__ = "task_checkpoints"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    step_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    """每个 task 内部单调递增 — 跨 step 也唯一. resume 取最大值."""

    # Snapshot data
    conversation_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    """LLM messages list (role/content). 用 list[dict] 而非 frozen — JSONB 落库."""
    working_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    """任意 caller 自定义的中间状态 (tool 已用过的、未完成 sub-task list 等)."""
    artifact_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    """已产 artifact 的 MinIO key / file path."""

    # Verification / resume help
    goal_anchor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_self_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cost_usd_so_far: Mapped[float] = mapped_column(nullable=False, default=0.0)
    tokens_used_so_far: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Status
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
