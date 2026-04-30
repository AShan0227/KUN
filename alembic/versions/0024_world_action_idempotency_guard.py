"""Add WorldGateway idempotency guard index.

Revision ID: 0024_world_action_idempotency_guard
Revises: 0023_world_action_executions
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_world_action_idempotency_guard"
down_revision: str | None = "0023_world_action_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_world_action_executions_tenant_idempotency_active",
        "world_action_executions",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('claimed', 'executed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_world_action_executions_tenant_idempotency_active",
        table_name="world_action_executions",
    )
