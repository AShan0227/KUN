"""Add pending side-effect action queue.

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "task_ref",
            sa.String(64),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("target_ref", sa.String(256), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_approval"),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'approved', 'rejected', 'executed', 'cancelled')",
            name="ck_pending_actions_pending_action_status_valid",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="ck_pending_actions_pending_action_risk_level_valid",
        ),
    )
    op.create_index("ix_pending_actions_tenant_id", "pending_actions", ["tenant_id"])
    op.create_index("ix_pending_actions_task_ref", "pending_actions", ["task_ref"])
    op.create_index("ix_pending_actions_action_type", "pending_actions", ["action_type"])
    op.create_index("ix_pending_actions_status", "pending_actions", ["status"])
    op.create_index(
        "ix_pending_actions_tenant_status",
        "pending_actions",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_actions_tenant_status", table_name="pending_actions")
    op.drop_index("ix_pending_actions_status", table_name="pending_actions")
    op.drop_index("ix_pending_actions_action_type", table_name="pending_actions")
    op.drop_index("ix_pending_actions_task_ref", table_name="pending_actions")
    op.drop_index("ix_pending_actions_tenant_id", table_name="pending_actions")
    op.drop_table("pending_actions")
