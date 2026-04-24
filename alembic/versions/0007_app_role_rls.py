"""Create app DB role and tighten RLS policies.

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from kun.core.config import settings
from sqlalchemy import text
from sqlalchemy.engine import make_url

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = (
    "events",
    "tasks",
    "runtime_states",
    "task_results",
    "pending_actions",
    "capability_cards",
    "handoffs",
    "notifications",
    "experiments",
    "idempotency_keys",
)

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def _app_role() -> tuple[str, str | None]:
    app_url = make_url(settings().pg_dsn)
    return app_url.username or "kun_app", app_url.password


def _quote_role(role: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _quote_password(password: str | None) -> str:
    if password is None:
        return ""
    quoted = op.get_bind().execute(
        text("SELECT quote_literal(:password)"),
        {"password": password},
    )
    return f" PASSWORD {quoted.scalar_one()}"


def upgrade() -> None:
    app_role, app_password = _app_role()
    role_sql = _quote_role(app_role)
    password_sql = _quote_password(app_password)
    db_sql = _quote_role(op.get_bind().execute(text("SELECT current_database()")).scalar_one())
    role_exists = op.get_bind().execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": app_role},
    )
    if role_exists.scalar_one_or_none() is None:
        op.execute(
            f"""
            CREATE ROLE {role_sql}
                LOGIN
                {password_sql}
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                NOBYPASSRLS
            """
        )
    else:
        op.execute(
            f"""
            ALTER ROLE {role_sql}
                NOSUPERUSER
                NOCREATEDB
                NOCREATEROLE
                NOBYPASSRLS
            """
        )
        if password_sql:
            op.execute(f"ALTER ROLE {role_sql}{password_sql}")

    op.execute(f"GRANT CONNECT ON DATABASE {db_sql} TO {role_sql}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role_sql}")
    op.execute(f"GRANT USAGE ON SCHEMA nuo TO {role_sql}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role_sql}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role_sql}")
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role_sql}
        """
    )
    op.execute(
        f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO {role_sql}
        """
    )

    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({TENANT_POLICY_EXPR})
            WITH CHECK ({TENANT_POLICY_EXPR})
            """
        )


def downgrade() -> None:
    app_role, _ = _app_role()
    role_sql = _quote_role(app_role)
    db_sql = _quote_role(op.get_bind().execute(text("SELECT current_database()")).scalar_one())
    bypass_expr = (
        "current_setting('app.bypass_rls', true) = 'on' "
        "OR tenant_id = current_setting('app.tenant_id', true)"
    )
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({bypass_expr})
            WITH CHECK ({bypass_expr})
            """
        )

    op.execute(f"REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM {role_sql}")
    op.execute(
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {role_sql}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA nuo FROM {role_sql}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role_sql}")
    op.execute(f"REVOKE CONNECT ON DATABASE {db_sql} FROM {role_sql}")
