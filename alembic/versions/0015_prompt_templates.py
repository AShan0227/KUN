"""Add tenant-scoped prompt template version table.

Revision ID: 0015_prompt_templates
Revises: 0014
Create Date: 2026-04-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_prompt_templates"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=128), nullable=False),
        sa.Column("target_module", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="kun_lab"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "metadata",
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
        sa.CheckConstraint("version >= 1", name="prompt_template_version_positive"),
        sa.CheckConstraint("length(content) > 0", name="prompt_template_content_not_empty"),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "template_id",
            "version",
            name="pk_prompt_templates",
        ),
    )
    op.create_index(
        "ix_prompt_templates_tenant_task",
        "prompt_templates",
        ["tenant_id", "task_type", "target_module", "strategy"],
    )
    op.create_index(
        "ix_prompt_templates_tenant_active",
        "prompt_templates",
        ["tenant_id", "target_module", "active"],
    )
    op.execute("ALTER TABLE prompt_templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prompt_templates FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON prompt_templates
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON prompt_templates")
    op.execute("ALTER TABLE prompt_templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prompt_templates DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_prompt_templates_tenant_active", table_name="prompt_templates")
    op.drop_index("ix_prompt_templates_tenant_task", table_name="prompt_templates")
    op.drop_table("prompt_templates")
