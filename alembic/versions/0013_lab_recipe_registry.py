"""Add persistent lab recipe registry table.

Revision ID: 0013_lab_recipe_registry
Revises: 0012
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_lab_recipe_registry"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "lab_recipe_registry",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=128), nullable=False),
        sa.Column("target_module", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=128), nullable=False),
        sa.Column("win_rate", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("promotion_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "extras",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("win_rate >= 0 AND win_rate <= 1", name="lab_recipe_win_rate_range"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="lab_recipe_confidence_range",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "task_type",
            "target_module",
            name="pk_lab_recipe_registry",
        ),
    )
    op.create_index(
        "ix_lab_recipe_registry_tenant_task",
        "lab_recipe_registry",
        ["tenant_id", "task_type"],
    )
    op.execute("ALTER TABLE lab_recipe_registry ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lab_recipe_registry FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON lab_recipe_registry
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lab_recipe_registry")
    op.execute("ALTER TABLE lab_recipe_registry NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lab_recipe_registry DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_lab_recipe_registry_tenant_task", table_name="lab_recipe_registry")
    op.drop_table("lab_recipe_registry")
