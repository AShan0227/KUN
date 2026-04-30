"""Add durable state ledger snapshots.

Revision ID: 0027_state_ledger_entries
Revises: 0026_token_usage_audit
Create Date: 2026-05-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_state_ledger_entries"
down_revision: str | None = "0026_token_usage_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "state_ledger_entries",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name=op.f("ck_state_ledger_entries_state_ledger_status_valid"),
        ),
        sa.CheckConstraint(
            "length(task_id) > 0",
            name=op.f("ck_state_ledger_entries_state_ledger_task_id_not_empty"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "task_id", name=op.f("pk_state_ledger_entries")),
    )
    op.create_index(
        op.f("ix_state_ledger_entries_user_id"),
        "state_ledger_entries",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_state_ledger_entries_status"),
        "state_ledger_entries",
        ["status"],
    )
    op.create_index(
        "ix_state_ledger_tenant_status_updated",
        "state_ledger_entries",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_state_ledger_tenant_user_updated",
        "state_ledger_entries",
        ["tenant_id", "user_id", "updated_at"],
    )
    op.execute("ALTER TABLE state_ledger_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE state_ledger_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON state_ledger_entries
        USING ({TENANT_POLICY_EXPR})
        WITH CHECK ({TENANT_POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON state_ledger_entries")
    op.execute("ALTER TABLE state_ledger_entries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE state_ledger_entries DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_state_ledger_tenant_user_updated", table_name="state_ledger_entries")
    op.drop_index("ix_state_ledger_tenant_status_updated", table_name="state_ledger_entries")
    op.drop_index(op.f("ix_state_ledger_entries_status"), table_name="state_ledger_entries")
    op.drop_index(op.f("ix_state_ledger_entries_user_id"), table_name="state_ledger_entries")
    op.drop_table("state_ledger_entries")
