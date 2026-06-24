"""engineering_discipline_reports — V7.1 §4.3 + X.S discipline report 持久化.

X.O 给 discipline 加了 process-local cache (kun/api/discipline_store.py), 但
多进程部署会丢. X.S 升 PG: LongTaskOrchestrator 完成时跑 enforcer →
record_discipline_report 写这张表 → cockpit /discipline/recent 读.

字段对齐 kun.api.discipline_store.DisciplineReportEntry:
  - task_id / captured_at / overall_score / n_total / n_passed
  - failed_disciplines JSONB list[str]

不变量 (DB CHECK):
  - 0 ≤ overall_score ≤ 1
  - 0 ≤ n_passed ≤ n_total
  - n_total ≥ 0

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY (同 0011-0017).

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "engineering_discipline_reports",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("report_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("n_total", sa.Integer(), nullable=False),
        sa.Column("n_passed", sa.Integer(), nullable=False),
        sa.Column(
            "failed_disciplines",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "overall_score >= 0 AND overall_score <= 1",
            name="edr_score_in_range",
        ),
        sa.CheckConstraint("n_total >= 0", name="edr_n_total_nonneg"),
        sa.CheckConstraint(
            "n_passed >= 0 AND n_passed <= n_total",
            name="edr_n_passed_bounds",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "report_id", name="pk_engineering_discipline_reports"
        ),
    )
    op.create_index(
        "ix_edr_captured_at",
        "engineering_discipline_reports",
        ["tenant_id", "captured_at"],
    )
    op.create_index(
        "ix_edr_task",
        "engineering_discipline_reports",
        ["tenant_id", "task_id", "captured_at"],
    )
    op.execute(
        "ALTER TABLE engineering_discipline_reports ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE engineering_discipline_reports FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY tenant_isolation ON engineering_discipline_reports "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation ON engineering_discipline_reports"
    )
    op.execute(
        "ALTER TABLE engineering_discipline_reports NO FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE engineering_discipline_reports DISABLE ROW LEVEL SECURITY"
    )
    op.drop_index(
        "ix_edr_task", table_name="engineering_discipline_reports"
    )
    op.drop_index(
        "ix_edr_captured_at", table_name="engineering_discipline_reports"
    )
    op.drop_table("engineering_discipline_reports")
