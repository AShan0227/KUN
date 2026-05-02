"""Add token usage audit columns.

Revision ID: 0026_token_usage_audit
Revises: 0025_tenant_member_invite_tokens
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_token_usage_audit"
down_revision: str | None = "0025_tenant_member_invite_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_token_issues",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_token_issues",
        sa.Column("last_ip_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tenant_token_issues",
        sa.Column("last_user_agent", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "tenant_token_issues",
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_tenant_tokens_tenant_last_used",
        "tenant_token_issues",
        ["tenant_id", "last_used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_tokens_tenant_last_used", table_name="tenant_token_issues")
    op.drop_column("tenant_token_issues", "use_count")
    op.drop_column("tenant_token_issues", "last_user_agent")
    op.drop_column("tenant_token_issues", "last_ip_hash")
    op.drop_column("tenant_token_issues", "last_used_at")
