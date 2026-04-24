"""Add task result cache.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_results",
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("answer", sa.Text, nullable=False, server_default=""),
        sa.Column("cost_usd_actual", sa.Float, nullable=False, server_default="0"),
        sa.Column("cost_usd_equivalent", sa.Float, nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("duration_sec", sa.Float, nullable=False, server_default="0"),
        sa.Column("surprise_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("notifications_json", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("result_json", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name="ck_task_results_task_result_status_valid",
        ),
    )
    op.create_index("ix_task_results_tenant_id", "task_results", ["tenant_id"])
    op.create_index("ix_task_results_status", "task_results", ["status"])
    op.create_index("ix_task_results_tenant_task", "task_results", ["tenant_id", "task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_results_tenant_task", table_name="task_results")
    op.drop_index("ix_task_results_status", table_name="task_results")
    op.drop_index("ix_task_results_tenant_id", table_name="task_results")
    op.drop_table("task_results")
