"""Add tenant password credentials.

Revision ID: 0028_tenant_password_credentials
Revises: 0027_state_ledger_entries
Create Date: 2026-05-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_tenant_password_credentials"
down_revision: str | None = "0027_state_ledger_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "tenant_password_credentials",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column(
            "algorithm",
            sa.String(length=32),
            nullable=False,
            server_default="pbkdf2_sha256",
        ),
        sa.Column("iterations", sa.Integer(), nullable=False, server_default="260000"),
        sa.Column("salt_b64", sa.String(length=128), nullable=False),
        sa.Column("password_hash_b64", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
            "algorithm IN ('pbkdf2_sha256')",
            name="tenant_password_algorithm_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="tenant_password_status_valid",
        ),
        sa.CheckConstraint("iterations >= 200000", name="tenant_password_iterations_min"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_members.tenant_id", "tenant_members.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_password_credentials"),
    )
    op.create_index(
        "ix_tenant_password_status",
        "tenant_password_credentials",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_tenant_password_last_login",
        "tenant_password_credentials",
        ["tenant_id", "last_login_at"],
    )
    op.execute("ALTER TABLE tenant_password_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_password_credentials FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON tenant_password_credentials
        USING ({POLICY_EXPR})
        WITH CHECK ({POLICY_EXPR})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_password_credentials")
    op.execute("ALTER TABLE tenant_password_credentials NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_password_credentials DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tenant_password_last_login", table_name="tenant_password_credentials")
    op.drop_index("ix_tenant_password_status", table_name="tenant_password_credentials")
    op.drop_table("tenant_password_credentials")
