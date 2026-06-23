"""Add TaskMeta L1 Director fields to tasks (audit F135/F096).

TaskMeta carries complexity / priority_profile / estimated_steps (L1.7, ADR-020/
ADR-022 Director outputs), but the tasks table had no columns for them, so the
orchestrator silently dropped them on persist. Add the three columns with
server defaults matching the Pydantic defaults so existing rows backfill.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tasks"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "estimated_steps",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "complexity",
            sa.String(length=16),
            nullable=False,
            server_default="simple",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "priority_profile",
            sa.String(length=16),
            nullable=False,
            server_default="cost_first",
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "priority_profile")
    op.drop_column(_TABLE, "complexity")
    op.drop_column(_TABLE, "estimated_steps")
