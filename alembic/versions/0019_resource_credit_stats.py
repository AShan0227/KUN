"""Add durable resource credit stats.

Revision ID: 0019_resource_credit_stats
Revises: 0018_mission_operating_loop
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_resource_credit_stats"
down_revision: str | None = "0018_mission_operating_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "resource_credit_stats",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("resource_key", sa.String(length=256), nullable=False),
        sa.Column("resource_kind", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=192), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("critical_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credit_total", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "last_seen_at",
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
        sa.CheckConstraint("length(resource_key) > 0", name="resource_credit_key_not_empty"),
        sa.CheckConstraint("length(resource_kind) > 0", name="resource_credit_kind_not_empty"),
        sa.CheckConstraint("length(resource_id) > 0", name="resource_credit_id_not_empty"),
        sa.CheckConstraint("used_count >= 0", name="resource_credit_used_nonnegative"),
        sa.CheckConstraint("pass_count >= 0", name="resource_credit_pass_nonnegative"),
        sa.CheckConstraint("critical_count >= 0", name="resource_credit_critical_nonnegative"),
        sa.CheckConstraint("credit_total >= 0", name="resource_credit_total_nonnegative"),
        sa.PrimaryKeyConstraint("tenant_id", "resource_key", name="pk_resource_credit_stats"),
    )
    op.create_index(
        "ix_resource_credit_tenant_kind",
        "resource_credit_stats",
        ["tenant_id", "resource_kind"],
    )
    op.create_index(
        "ix_resource_credit_tenant_resource",
        "resource_credit_stats",
        ["tenant_id", "resource_kind", "resource_id"],
    )
    op.execute("ALTER TABLE resource_credit_stats ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE resource_credit_stats FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON resource_credit_stats
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON resource_credit_stats")
    op.execute("ALTER TABLE resource_credit_stats NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE resource_credit_stats DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_resource_credit_tenant_resource", table_name="resource_credit_stats")
    op.drop_index("ix_resource_credit_tenant_kind", table_name="resource_credit_stats")
    op.drop_table("resource_credit_stats")
