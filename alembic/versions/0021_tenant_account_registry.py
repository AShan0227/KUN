"""Add tenant account registry.

Revision ID: 0021_tenant_account_registry
Revises: 0020_qi_problem_signals
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_tenant_account_registry"
down_revision: str | None = "0020_qi_problem_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "tenant_accounts",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("plan", sa.String(length=32), nullable=False, server_default="dev"),
        sa.Column("billing_status", sa.String(length=32), nullable=False, server_default="manual"),
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
            "status IN ('active', 'suspended', 'closed')",
            name="tenant_account_status_valid",
        ),
        sa.CheckConstraint(
            "billing_status IN ('manual', 'trial', 'active', 'past_due', 'cancelled')",
            name="tenant_account_billing_status_valid",
        ),
        sa.CheckConstraint("length(display_name) > 0", name="tenant_account_display_not_empty"),
        sa.CheckConstraint("length(owner_user_id) > 0", name="tenant_account_owner_not_empty"),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_accounts"),
    )
    op.create_index("ix_tenant_accounts_organization_id", "tenant_accounts", ["organization_id"])
    op.create_index("ix_tenant_accounts_status", "tenant_accounts", ["status"])
    op.create_index(
        "ix_tenant_accounts_org_status",
        "tenant_accounts",
        ["organization_id", "status"],
    )

    op.create_table(
        "tenant_members",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="owner"),
        sa.Column(
            "scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
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
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="tenant_member_role_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'invited', 'disabled')",
            name="tenant_member_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_accounts.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", name="pk_tenant_members"),
    )
    op.create_index("ix_tenant_members_status", "tenant_members", ["status"])
    op.create_index(
        "ix_tenant_members_tenant_status",
        "tenant_members",
        ["tenant_id", "status"],
    )

    op.create_table(
        "tenant_token_issues",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("audience", sa.String(length=16), nullable=False, server_default="developer"),
        sa.Column(
            "scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="issued"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "audience IN ('novice', 'developer', 'expert')",
            name="tenant_token_audience_valid",
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'revoked')",
            name="tenant_token_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_accounts.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "token_id", name="pk_tenant_token_issues"),
        sa.UniqueConstraint("token_hash", name="uq_tenant_token_issues_token_hash"),
    )
    op.create_index("ix_tenant_token_issues_user_id", "tenant_token_issues", ["user_id"])
    op.create_index("ix_tenant_token_issues_status", "tenant_token_issues", ["status"])
    op.create_index(
        "ix_tenant_tokens_tenant_status",
        "tenant_token_issues",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_tenant_tokens_tenant_user",
        "tenant_token_issues",
        ["tenant_id", "user_id"],
    )

    for table in ("tenant_accounts", "tenant_members", "tenant_token_issues"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({POLICY_EXPR})
            WITH CHECK ({POLICY_EXPR})
            """
        )


def downgrade() -> None:
    for table in ("tenant_token_issues", "tenant_members", "tenant_accounts"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_tenant_tokens_tenant_user", table_name="tenant_token_issues")
    op.drop_index("ix_tenant_tokens_tenant_status", table_name="tenant_token_issues")
    op.drop_index("ix_tenant_token_issues_status", table_name="tenant_token_issues")
    op.drop_index("ix_tenant_token_issues_user_id", table_name="tenant_token_issues")
    op.drop_table("tenant_token_issues")
    op.drop_index("ix_tenant_members_tenant_status", table_name="tenant_members")
    op.drop_index("ix_tenant_members_status", table_name="tenant_members")
    op.drop_table("tenant_members")
    op.drop_index("ix_tenant_accounts_org_status", table_name="tenant_accounts")
    op.drop_index("ix_tenant_accounts_status", table_name="tenant_accounts")
    op.drop_index("ix_tenant_accounts_organization_id", table_name="tenant_accounts")
    op.drop_table("tenant_accounts")
