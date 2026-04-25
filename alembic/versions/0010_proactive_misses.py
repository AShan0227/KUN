"""Add proactive_misses for learned tool triggers.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "proactive_misses",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=128), nullable=False),
        sa.Column("pattern", sa.String(length=512), nullable=False),
        sa.Column("miss_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reason", sa.Text(), nullable=True),
        sa.Column("trigger_source", sa.String(length=64), nullable=True),
        sa.Column(
            "last_missed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("miss_count >= 0", name="proactive_misses_count_nonnegative"),
        sa.PrimaryKeyConstraint("tenant_id", "skill_id", "pattern", name="pk_proactive_misses"),
    )
    op.create_index(
        "ix_proactive_misses_tenant_promoted",
        "proactive_misses",
        ["tenant_id", "promoted_at"],
    )
    op.execute("ALTER TABLE proactive_misses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE proactive_misses FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON proactive_misses
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON proactive_misses")
    op.execute("ALTER TABLE proactive_misses NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE proactive_misses DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_proactive_misses_tenant_promoted", table_name="proactive_misses")
    op.drop_table("proactive_misses")
