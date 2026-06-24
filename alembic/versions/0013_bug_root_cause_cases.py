"""bug_root_cause_cases — RCDH 案例库 fast-path lookup.

RCDH 4 级诊断每个 trace 从头分析慢. 这张表挂"案例库" — 同 signature 见过
就直接返 fix_pattern, 没命中再走完整 4 级诊断.

  trace_signature = error_type + top 3 内部 frame (kun.* 优先) + SHA256 prefix
  case lookup: O(1) by (tenant_id, trace_signature) unique index
  命中 → hit_count++ + last_hit_at = now()
  未命中 → 走 RCDH 完整诊断 → 诊断完 record_case 落新条

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation policy
(同 0011/0012 风格).

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "bug_root_cause_cases",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("trace_signature", sa.String(length=256), nullable=False),
        # trace_signature = exception type + top 3 frame 名 + SHA256 prefix
        # e.g. "AssertionError|test_x.py:test_y|kun.foo.bar.baz|hash:abc123"
        sa.Column("error_type", sa.String(length=128), nullable=False),
        sa.Column("root_cause_kind", sa.String(length=64), nullable=False),
        # e.g. 'double_responsibility' / 'race_condition' / 'missing_migration' /
        #      'keyword_false_positive' / 'tenant_isolation_leak' / ...
        sa.Column("fix_pattern", sa.Text(), nullable=False),
        # 中英描述如何修. 多个修法之间 \n--\n 分割.
        sa.Column("evidence_dx_id", sa.String(length=64), nullable=True),
        # 关联 diagnostic_records 行 (如有完整诊断)
        sa.Column(
            "hit_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_hit_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "hit_count >= 1",
            name="bug_case_hit_count_positive",
        ),
        sa.CheckConstraint(
            "length(trace_signature) > 0",
            name="bug_case_signature_not_empty",
        ),
        sa.CheckConstraint(
            "length(error_type) > 0",
            name="bug_case_error_type_not_empty",
        ),
        sa.CheckConstraint(
            "length(root_cause_kind) > 0",
            name="bug_case_root_cause_kind_not_empty",
        ),
        sa.CheckConstraint(
            "length(fix_pattern) > 0",
            name="bug_case_fix_pattern_not_empty",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "case_id", name="pk_bug_root_cause_cases"
        ),
    )
    # O(1) lookup by signature; unique 防同 tenant 同 signature 两条
    op.create_index(
        "ix_bug_cases_signature",
        "bug_root_cause_cases",
        ["tenant_id", "trace_signature"],
        unique=True,
    )
    # hit_count DESC: 热点案例 dashboard (top 案例)
    op.create_index(
        "ix_bug_cases_hit_count",
        "bug_root_cause_cases",
        ["tenant_id", "hit_count"],
    )

    # RLS (与 0011/0012 风格一致)
    op.execute("ALTER TABLE bug_root_cause_cases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bug_root_cause_cases FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON bug_root_cause_cases "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bug_root_cause_cases")
    op.execute("ALTER TABLE bug_root_cause_cases NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bug_root_cause_cases DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_bug_cases_hit_count", table_name="bug_root_cause_cases")
    op.drop_index("ix_bug_cases_signature", table_name="bug_root_cause_cases")
    op.drop_table("bug_root_cause_cases")
