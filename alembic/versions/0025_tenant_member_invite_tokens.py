"""Add tenant member invite token audit columns.

Revision ID: 0025_tenant_member_invite_tokens
Revises: 0024_world_action_idem
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_tenant_member_invite_tokens"
down_revision: str | None = "0024_world_action_idem"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_members",
        sa.Column("invite_token_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tenant_members",
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_members",
        sa.Column("invite_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_members",
        sa.Column("invited_by_user_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_tenant_members_invite_expires",
        "tenant_members",
        ["tenant_id", "invite_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_members_invite_expires", table_name="tenant_members")
    op.drop_column("tenant_members", "invited_by_user_id")
    op.drop_column("tenant_members", "invite_accepted_at")
    op.drop_column("tenant_members", "invite_expires_at")
    op.drop_column("tenant_members", "invite_token_hash")
