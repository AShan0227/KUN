"""V2.3 Wire 39: protocols table — KUN 协议 (启 → 鲲 IP 沉淀).

Revision ID: 0015_protocols
Revises: 0014
Create Date: 2026-04-27

跟 V2.2 LabRecipeRegistry 不同:
- LabRecipeRegistry: (task_type, target_module) → 单 strategy + win_rate
- protocols: protocol_id × version × tenant_id, 含完整执行模板 (JSONB)

lifecycle status:
  experimental → shadow → canary → stable
                                  ↓ rolled_back
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_protocols"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "protocols",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("protocol_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=sa.text("'qi'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "protocol_id", "version"),
        sa.CheckConstraint(
            "status IN ('experimental', 'shadow', 'canary', 'stable', 'rolled_back')",
            name="protocols_status_valid",
        ),
    )
    op.create_index(
        "ix_protocols_tenant_status",
        "protocols",
        ["tenant_id", "protocol_id", "status"],
    )
    op.create_index(
        "ix_protocols_tenant_promoted",
        "protocols",
        ["tenant_id", "promoted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_protocols_tenant_promoted", table_name="protocols")
    op.drop_index("ix_protocols_tenant_status", table_name="protocols")
    op.drop_table("protocols")
