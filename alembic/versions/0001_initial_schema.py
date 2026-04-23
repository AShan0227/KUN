"""Initial schema — events outbox, tasks, runtime_states, capability_cards, handoffs, notifications, experiments, idempotency_keys.

ADR-005: events append-only.
ADR-007: tenant_id on every table; Row Level Security stubs to be enabled in 0002.

Revision ID: 0001
Revises:
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- events (Outbox) ----
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("span_id", sa.String(64), nullable=True),
        sa.Column("causation_event_id", sa.String(64), nullable=True),
        sa.Column("task_ref", sa.String(64), nullable=True),
    )
    op.create_index("ix_events_tenant_id", "events", ["tenant_id"])
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"])
    op.create_index("ix_events_task_ref", "events", ["task_ref"])
    op.create_index(
        "ix_events_unpublished",
        "events",
        ["event_id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index("ix_events_tenant_time", "events", ["tenant_id", "occurred_at"])

    # ---- tasks ----
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("task_type", sa.String(128), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("complexity_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("estimated_duration_sec", sa.Float, nullable=False, server_default="0"),
        sa.Column("deadline_iso", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success_criteria_short", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("spec_json", postgresql.JSONB, nullable=True),
        sa.Column("layer3_ref", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_tasks_fingerprint", "tasks", ["tenant_id", "fingerprint"])
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])
    op.create_index("ix_tasks_task_type", "tasks", ["task_type"])
    op.create_index("ix_tasks_tenant_type", "tasks", ["tenant_id", "task_type"])

    # ---- runtime_states ----
    op.create_table(
        "runtime_states",
        sa.Column("state_id", sa.String(64), primary_key=True),
        sa.Column(
            "task_ref",
            sa.String(64),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("current_step", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_planned_steps", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("accumulated_cost_usd_actual", sa.Float, nullable=False, server_default="0"),
        sa.Column("accumulated_cost_usd_equivalent", sa.Float, nullable=False, server_default="0"),
        sa.Column("accumulated_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("failures_this_run", sa.Integer, nullable=False, server_default="0"),
        sa.Column("blob", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_states_task_ref", "runtime_states", ["task_ref"])
    op.create_index("ix_runtime_states_tenant_id", "runtime_states", ["tenant_id"])
    op.create_index("ix_runtime_states_status", "runtime_states", ["status"])

    # ---- capability_cards ----
    op.create_table(
        "capability_cards",
        sa.Column("card_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("maturity", sa.String(16), nullable=False, server_default="cold_start"),
        sa.Column("overall_reliability", sa.Float, nullable=False, server_default="0"),
        sa.Column("primary_strength", sa.String(128), nullable=True),
        sa.Column("primary_weakness", sa.String(128), nullable=True),
        sa.Column("card_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_capability_entity",
        "capability_cards",
        ["tenant_id", "entity_type", "entity_id"],
    )
    op.create_index("ix_capability_cards_tenant", "capability_cards", ["tenant_id"])

    # ---- handoffs ----
    op.create_table(
        "handoffs",
        sa.Column("packet_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "task_ref",
            sa.String(64),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_entity", sa.String(128), nullable=False),
        sa.Column("to_entity", sa.String(128), nullable=False),
        sa.Column("l1_json", postgresql.JSONB, nullable=False),
        sa.Column("l2_json", postgresql.JSONB, nullable=True),
        sa.Column("l3_ref", sa.String(256), nullable=True),
        sa.Column("l4_refs", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_handoffs_tenant", "handoffs", ["tenant_id"])
    op.create_index("ix_handoffs_task", "handoffs", ["task_ref"])

    # ---- notifications ----
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("channel", sa.String(16), nullable=False, server_default="side"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("render_hint", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("task_ref", sa.String(64), nullable=True),
        sa.Column("causation_event_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notifications_tenant", "notifications", ["tenant_id"])
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.create_index("ix_notifications_task", "notifications", ["task_ref"])
    op.create_index("ix_notifications_created", "notifications", ["created_at"])

    # ---- experiments (ADR-009) ----
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("rollout_percent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("control_variant", postgresql.JSONB, nullable=True),
        sa.Column("treatment_variant", postgresql.JSONB, nullable=True),
        sa.Column("guardrails", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("metrics", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_experiments_tenant", "experiments", ["tenant_id"])
    op.create_index("ix_experiments_status", "experiments", ["status"])

    # ---- idempotency_keys ----
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("result_ref", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ttl_sec", sa.Integer, nullable=False, server_default="300"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("experiments")
    op.drop_table("notifications")
    op.drop_table("handoffs")
    op.drop_table("capability_cards")
    op.drop_table("runtime_states")
    op.drop_table("tasks")
    op.drop_table("events")
