"""Add mission operating loop fields.

Revision ID: 0018_mission_operating_loop
Revises: 0017_missions
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_mission_operating_loop"
down_revision: str | None = "0017_missions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "missions",
        sa.Column("budget_used_usd", sa.Float(), nullable=False, server_default="0.0"),
    )
    op.add_column(
        "missions",
        sa.Column("blocked_reason", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "missions",
        sa.Column(
            "next_step_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "missions",
        sa.Column("review_interval_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.add_column(
        "missions",
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "mission_budget_used_nonnegative",
        "missions",
        "budget_used_usd >= 0",
    )
    op.create_check_constraint(
        "mission_review_interval_positive",
        "missions",
        "review_interval_hours > 0",
    )
    op.add_column(
        "mission_milestones",
        sa.Column("completed_by_task_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_mission_milestones_completed_by_task",
        "mission_milestones",
        ["completed_by_task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_milestones_completed_by_task", table_name="mission_milestones")
    op.drop_column("mission_milestones", "completed_by_task_id")
    op.drop_constraint("mission_review_interval_positive", "missions", type_="check")
    op.drop_constraint("mission_budget_used_nonnegative", "missions", type_="check")
    op.drop_column("missions", "last_reviewed_at")
    op.drop_column("missions", "review_interval_hours")
    op.drop_column("missions", "next_step_json")
    op.drop_column("missions", "blocked_reason")
    op.drop_column("missions", "budget_used_usd")
