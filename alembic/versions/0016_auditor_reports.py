"""auditor_reports — V7 §16.6 External Supervisor auditor hat 周期审计存档.

External Supervisor 戴 auditor hat 跑 "生产闭环攻击审计员" 7 角度审计后, JSON
output 落这张表. 给驾驶舱 (V7 §20) + lifecycle gate (V7 §15 Canary→Production
flip 前必查) + 启 Qi post-hoc retrospect (V7 §12.4 过去线) 提供历史数据.

9-field schema 严格对齐 kun/integration/external_supervisor_critique.py 的
AUDITOR_SYSTEM_PROMPT_TEMPLATE JSON output:
  design_promise / real_code_path / bypass_methods / min_repro_steps /
  risk_level / must_fix / acceptance_tests / allow_release / rationale

V7 §16.6 不变量 (服务层 + DB CHECK 双保险):
  - risk_level ∈ {'P0', 'P1', 'P2'}
  - risk_level='P0' ⇒ allow_release=false (P0 风险必须不许发布)

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation
policy (与 0011-0015 风格一致).

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "auditor_reports",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("report_id", sa.String(length=64), nullable=False),
        sa.Column("audited_capability", sa.String(length=128), nullable=False),
        sa.Column(
            "audited_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("auditor_provider", sa.String(length=128), nullable=False),
        sa.Column(
            "design_promise", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "real_code_path", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column(
            "bypass_methods",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "min_repro_steps", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("risk_level", sa.String(length=2), nullable=False),
        sa.Column(
            "must_fix",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "acceptance_tests",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allow_release",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('P0', 'P1', 'P2')",
            name="ar_risk_level_valid",
        ),
        # V7 §16.6 不变量 (DB 兜底): P0 风险必须不许发布
        sa.CheckConstraint(
            "NOT (risk_level = 'P0' AND allow_release = true)",
            name="ar_p0_blocks_release",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "report_id", name="pk_auditor_reports"
        ),
    )
    op.create_index(
        "ix_ar_capability_audited_at",
        "auditor_reports",
        ["tenant_id", "audited_capability", "audited_at"],
    )
    op.create_index(
        "ix_ar_risk_level_recent",
        "auditor_reports",
        ["tenant_id", "risk_level", "audited_at"],
    )
    op.execute("ALTER TABLE auditor_reports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditor_reports FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON auditor_reports "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON auditor_reports")
    op.execute("ALTER TABLE auditor_reports NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE auditor_reports DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_ar_risk_level_recent", table_name="auditor_reports")
    op.drop_index(
        "ix_ar_capability_audited_at", table_name="auditor_reports"
    )
    op.drop_table("auditor_reports")
