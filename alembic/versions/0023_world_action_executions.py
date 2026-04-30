"""Add durable WorldGateway action execution ledger.

Revision ID: 0023_world_action_executions
Revises: 0022_world_handler_controls
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_world_action_executions"
down_revision: str | None = "0022_world_handler_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "world_action_executions",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=False),
        sa.Column("task_ref", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target_ref", sa.String(length=256), nullable=False, server_default="unknown"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="claimed"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("handler_id", sa.String(length=128), nullable=True),
        sa.Column("gateway_mode", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("capability_status", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("external_dispatched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requires_handler", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("artifact_ref", sa.Text(), nullable=True),
        sa.Column("compensation_strategy", sa.Text(), nullable=False, server_default=""),
        sa.Column("retry_policy", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "audit_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "decision_ticket_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('claimed', 'executed', 'blocked', 'failed', 'cancelled')",
            name="world_action_execution_status_valid",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="world_action_execution_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) > 0",
            name="world_action_execution_idempotency_not_empty",
        ),
        sa.ForeignKeyConstraint(["task_ref"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "action_id", name="pk_world_action_executions"),
    )
    op.create_index(
        "ix_world_action_executions_action_type",
        "world_action_executions",
        ["action_type"],
    )
    op.create_index(
        "ix_world_action_executions_handler_id",
        "world_action_executions",
        ["handler_id"],
    )
    op.create_index(
        "ix_world_action_executions_status",
        "world_action_executions",
        ["status"],
    )
    op.create_index(
        "ix_world_action_executions_task_ref",
        "world_action_executions",
        ["task_ref"],
    )
    op.create_index(
        "ix_world_action_executions_tenant_action_type",
        "world_action_executions",
        ["tenant_id", "action_type"],
    )
    op.create_index(
        "ix_world_action_executions_tenant_status",
        "world_action_executions",
        ["tenant_id", "status"],
    )
    op.execute("ALTER TABLE world_action_executions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE world_action_executions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON world_action_executions
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON world_action_executions")
    op.execute("ALTER TABLE world_action_executions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE world_action_executions DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_world_action_executions_tenant_status", table_name="world_action_executions")
    op.drop_index(
        "ix_world_action_executions_tenant_action_type",
        table_name="world_action_executions",
    )
    op.drop_index("ix_world_action_executions_task_ref", table_name="world_action_executions")
    op.drop_index("ix_world_action_executions_status", table_name="world_action_executions")
    op.drop_index("ix_world_action_executions_handler_id", table_name="world_action_executions")
    op.drop_index("ix_world_action_executions_action_type", table_name="world_action_executions")
    op.drop_table("world_action_executions")
