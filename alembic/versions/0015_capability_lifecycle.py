"""lifecycle_transitions — V7 §15 capability lifecycle stage transitions.

启 (Qi) capability 在 V7 §15 9 阶段 lifecycle 间流转的历史日志.
Phase X.A 给了 service + frozen IO + emitter callback
(kun/governance/capability_lifecycle.py), 本 migration 给 emitter 提供
DB 落地表.

V7 §15 9 阶段 (enum CHECK):
  observation → candidate → replay → holdout → shadow → canary →
  production → monitor (+ rollback / retire 分支)

V7 §12.2 严格不变量 (服务层 + DB CHECK 双保险):
  - to_stage='production' ⇒ user_approval_ticket_id IS NOT NULL
  - to_stage='replay'     ⇒ jsonb_array_length(evidence_refs) >= 1
    (服务层强 enforce 三类 evidence 齐全; DB 只保底 "不空")

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation
policy (与 0011-0014 风格一致).

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"

STAGE_LIST = (
    "'observation', 'candidate', 'replay', 'holdout', "
    "'shadow', 'canary', 'production', 'monitor', 'rollback', 'retire'"
)


def upgrade() -> None:
    op.create_table(
        "lifecycle_transitions",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("transition_id", sa.String(length=64), nullable=False),
        sa.Column("capability_id", sa.String(length=64), nullable=False),
        sa.Column("from_stage", sa.String(length=32), nullable=False),
        sa.Column("to_stage", sa.String(length=32), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "decision_rationale", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "user_approval_ticket_id", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "evidence_refs",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "metrics_snapshot",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"from_stage IN ({STAGE_LIST})",
            name="lct_from_stage_valid",
        ),
        sa.CheckConstraint(
            f"to_stage IN ({STAGE_LIST})",
            name="lct_to_stage_valid",
        ),
        sa.CheckConstraint(
            "NOT (to_stage = 'production' AND user_approval_ticket_id IS NULL)",
            name="lct_production_needs_user_approval",
        ),
        sa.CheckConstraint(
            "NOT (to_stage = 'replay' AND "
            "jsonb_array_length(evidence_refs) < 1)",
            name="lct_replay_needs_evidence",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "transition_id", name="pk_lifecycle_transitions"
        ),
    )
    op.create_index(
        "ix_lct_capability_decided_at",
        "lifecycle_transitions",
        ["tenant_id", "capability_id", "decided_at"],
    )
    op.create_index(
        "ix_lct_to_stage",
        "lifecycle_transitions",
        ["tenant_id", "to_stage", "decided_at"],
    )
    op.execute("ALTER TABLE lifecycle_transitions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lifecycle_transitions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON lifecycle_transitions "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lifecycle_transitions")
    op.execute("ALTER TABLE lifecycle_transitions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lifecycle_transitions DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_lct_to_stage", table_name="lifecycle_transitions")
    op.drop_index(
        "ix_lct_capability_decided_at", table_name="lifecycle_transitions"
    )
    op.drop_table("lifecycle_transitions")
