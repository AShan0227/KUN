"""task_checkpoints — 长任务 checkpoint 持久化 (LT.C, ADR-022).

每个 Executor step 后落 checkpoint, 进程挂掉时按 sequence 取 latest active
row, resume conversation_snapshot + working_state + artifact_refs.

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "task_checkpoints",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "conversation_snapshot",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "working_state",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "artifact_refs",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("goal_anchor_id", sa.String(length=64), nullable=True),
        sa.Column("last_self_report", JSONB(), nullable=True),
        sa.Column("cost_usd_so_far", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tokens_used_so_far", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "rationale",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active','final','failed_resume')",
            name="task_checkpoint_status_valid",
        ),
        sa.CheckConstraint(
            "sequence >= 0",
            name="task_checkpoint_sequence_nonneg",
        ),
        sa.CheckConstraint(
            "step_idx >= 0",
            name="task_checkpoint_step_idx_nonneg",
        ),
        sa.CheckConstraint(
            "cost_usd_so_far >= 0",
            name="task_checkpoint_cost_nonneg",
        ),
        sa.CheckConstraint(
            "tokens_used_so_far >= 0",
            name="task_checkpoint_tokens_nonneg",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "checkpoint_id", name="pk_task_checkpoints"
        ),
    )
    # resume 时按 (tenant, task, sequence DESC) 取 latest active
    op.create_index(
        "ix_task_checkpoints_task_sequence",
        "task_checkpoints",
        ["tenant_id", "task_id", "sequence"],
    )
    # 通用 status 查询 (e.g. 全活跃任务 dashboard)
    op.create_index(
        "ix_task_checkpoints_status",
        "task_checkpoints",
        ["tenant_id", "status"],
    )

    # RLS (与 0011 风格一致)
    op.execute("ALTER TABLE task_checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_checkpoints FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON task_checkpoints "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_checkpoints")
    op.execute("ALTER TABLE task_checkpoints NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_checkpoints DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_task_checkpoints_status", table_name="task_checkpoints"
    )
    op.drop_index(
        "ix_task_checkpoints_task_sequence", table_name="task_checkpoints"
    )
    op.drop_table("task_checkpoints")
