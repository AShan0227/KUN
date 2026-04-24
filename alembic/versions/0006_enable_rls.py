"""Enable tenant row-level security.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
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

POLICY_EXPR = (
    "current_setting('app.bypass_rls', true) = 'on' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    for table in TENANT_TABLES:
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
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
