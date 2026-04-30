"""Add WorldGateway handler controls.

Revision ID: 0022_world_handler_controls
Revises: 0021_tenant_account_registry
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_world_handler_controls"
down_revision: str | None = "0021_tenant_account_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "world_handler_controls",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="enabled"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="nuo"),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata_json",
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
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('enabled', 'quarantined', 'disabled')",
            name="world_handler_control_status_valid",
        ),
        sa.CheckConstraint(
            "length(action_type) > 0",
            name="world_handler_control_action_not_empty",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "action_type",
            name="pk_world_handler_controls",
        ),
    )
    op.create_index("ix_world_handler_controls_status", "world_handler_controls", ["status"])
    op.create_index(
        "ix_world_handler_controls_tenant_status",
        "world_handler_controls",
        ["tenant_id", "status"],
    )
    op.execute("ALTER TABLE world_handler_controls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE world_handler_controls FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON world_handler_controls
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON world_handler_controls")
    op.execute("ALTER TABLE world_handler_controls NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE world_handler_controls DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_world_handler_controls_tenant_status", table_name="world_handler_controls")
    op.drop_index("ix_world_handler_controls_status", table_name="world_handler_controls")
    op.drop_table("world_handler_controls")
