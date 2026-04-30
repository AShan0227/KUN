"""Add durable Qi problem signals.

Revision ID: 0020_qi_problem_signals
Revises: 0019_resource_credit_stats
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_qi_problem_signals"
down_revision: str | None = "0019_resource_credit_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "qi_problem_signals",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("task_type", sa.String(length=128), nullable=False, server_default="general"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
        sa.CheckConstraint("length(summary) > 0", name="qi_problem_summary_not_empty"),
        sa.CheckConstraint("occurrence_count >= 1", name="qi_problem_occurrence_positive"),
        sa.CheckConstraint(
            "status IN ('open', 'consumed', 'dismissed')",
            name="qi_problem_status_valid",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "signal_id", name="pk_qi_problem_signals"),
    )
    op.create_index(
        "ix_qi_problem_tenant_status",
        "qi_problem_signals",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_qi_problem_tenant_category",
        "qi_problem_signals",
        ["tenant_id", "category"],
    )
    op.create_index(
        "ix_qi_problem_tenant_last_seen",
        "qi_problem_signals",
        ["tenant_id", "last_seen_at"],
    )
    op.execute("ALTER TABLE qi_problem_signals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE qi_problem_signals FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON qi_problem_signals
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON qi_problem_signals")
    op.execute("ALTER TABLE qi_problem_signals NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE qi_problem_signals DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_qi_problem_tenant_last_seen", table_name="qi_problem_signals")
    op.drop_index("ix_qi_problem_tenant_category", table_name="qi_problem_signals")
    op.drop_index("ix_qi_problem_tenant_status", table_name="qi_problem_signals")
    op.drop_table("qi_problem_signals")
