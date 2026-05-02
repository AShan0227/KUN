"""Manage lab_adoption_cursor with alembic.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lab_adoption_cursor",
        sa.Column("cursor_name", sa.String(length=64), nullable=False),
        sa.Column("last_adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "adopted_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("cursor_name", name="pk_lab_adoption_cursor"),
    )
    op.create_index(
        "ix_lab_adoption_cursor_updated_at",
        "lab_adoption_cursor",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_lab_adoption_cursor_updated_at", table_name="lab_adoption_cursor")
    op.drop_table("lab_adoption_cursor")
