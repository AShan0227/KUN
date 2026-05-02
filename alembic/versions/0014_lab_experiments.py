"""Persist KUN-Lab ExperimentLog.

Revision ID: 0014
Revises: 0013_lab_recipe_registry
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013_lab_recipe_registry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lab_experiments",
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=128), nullable=False),
        sa.Column("prompt_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("ensemble_result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("experiment_id", name="pk_lab_experiments"),
    )
    op.create_index("ix_lab_experiments_task_type", "lab_experiments", ["task_type"])
    op.create_index("ix_lab_experiments_created_at", "lab_experiments", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_lab_experiments_created_at", table_name="lab_experiments")
    op.drop_index("ix_lab_experiments_task_type", table_name="lab_experiments")
    op.drop_table("lab_experiments")
