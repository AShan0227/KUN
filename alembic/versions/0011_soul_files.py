"""Add soul_files for SoulFile DB persistence (V2.1 §13 / T17 / M4).

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "soul_files",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "audience",
            sa.String(length=16),
            nullable=False,
            server_default="developer",
        ),
        sa.Column(
            "default_language",
            sa.String(length=16),
            nullable=False,
            server_default="zh-CN",
        ),
        sa.Column(
            "risk_tolerance",
            sa.String(length=8),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "cost_sensitivity",
            sa.String(length=8),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "speed_sensitivity",
            sa.String(length=8),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "interruption_tolerance",
            sa.String(length=8),
            nullable=False,
            server_default="medium",
        ),
        sa.Column(
            "approval_threshold_money",
            sa.Float(),
            nullable=False,
            server_default="10.0",
        ),
        sa.Column(
            "professional_role",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "blob",
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
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "audience IN ('novice', 'developer', 'expert')",
            name="soul_file_audience_valid",
        ),
        sa.CheckConstraint(
            "risk_tolerance IN ('low', 'medium', 'high')",
            name="soul_file_risk_valid",
        ),
        sa.CheckConstraint(
            "cost_sensitivity IN ('low', 'medium', 'high')",
            name="soul_file_cost_valid",
        ),
        sa.CheckConstraint(
            "speed_sensitivity IN ('low', 'medium', 'high')",
            name="soul_file_speed_valid",
        ),
        sa.CheckConstraint(
            "interruption_tolerance IN ('low', 'medium', 'high')",
            name="soul_file_interrupt_valid",
        ),
        sa.CheckConstraint(
            "approval_threshold_money >= 0",
            name="soul_file_approval_threshold_nonneg",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_soul_files"),
    )
    op.create_index(
        "ix_soul_files_tenant_updated",
        "soul_files",
        ["tenant_id", "last_updated"],
    )
    op.execute("ALTER TABLE soul_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE soul_files FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON soul_files
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON soul_files")
    op.execute("ALTER TABLE soul_files NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE soul_files DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_soul_files_tenant_updated", table_name="soul_files")
    op.drop_table("soul_files")
