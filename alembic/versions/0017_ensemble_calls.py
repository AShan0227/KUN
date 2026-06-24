"""ensemble_calls — V7 §11.4 multi-LLM ensemble_invoke 调用日志表.

每次 ensemble_invoke 落一条 row, 给驾驶舱 + 启 (Qi) post-hoc retrospect +
成本归因用. Phase X.B 第 6 刀.

字段对齐 EnsembleResponse (kun/interface/llm/ensemble.py):
  - providers JSONB list[{name, model_id, family}] (V7 §11.1 cross-family 现状)
  - consensus_strategy ∈ {majority_vote, weighted, pick_best_by_metric}
  - divergence_score 0-1, divergence_signals JSONB list[str]
  - consensus_provider (winner)
  - total_cost_usd / failure_count / n_providers_total

不变量 (DB CHECK):
  - consensus_strategy 在 enum 里
  - divergence_score ∈ [0, 1]
  - n_providers_total ≥ 2 (V7 §11.4 ensemble 起步条件)
  - 0 ≤ failure_count ≤ n_providers_total

ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY + tenant_isolation
policy (与 0011-0016 风格一致).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "ensemble_calls",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("call_id", sa.String(length=64), nullable=False),
        sa.Column(
            "invoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "purpose", sa.String(length=64), nullable=False, server_default="execution"
        ),
        sa.Column(
            "providers",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("consensus_strategy", sa.String(length=32), nullable=False),
        sa.Column(
            "divergence_score",
            sa.Numeric(4, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "divergence_signals",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("consensus_provider", sa.String(length=128), nullable=True),
        sa.Column(
            "total_cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("n_providers_total", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "consensus_strategy IN ('majority_vote', 'weighted', "
            "'pick_best_by_metric')",
            name="ec_strategy_valid",
        ),
        sa.CheckConstraint(
            "divergence_score >= 0 AND divergence_score <= 1",
            name="ec_divergence_in_range",
        ),
        sa.CheckConstraint(
            "n_providers_total >= 2",
            name="ec_min_2_providers",
        ),
        sa.CheckConstraint(
            "failure_count >= 0 AND failure_count <= n_providers_total",
            name="ec_failure_count_bounds",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "call_id", name="pk_ensemble_calls"
        ),
    )
    op.create_index(
        "ix_ec_invoked_at",
        "ensemble_calls",
        ["tenant_id", "invoked_at"],
    )
    op.create_index(
        "ix_ec_high_divergence",
        "ensemble_calls",
        ["tenant_id", "divergence_score", "invoked_at"],
    )
    op.create_index(
        "ix_ec_purpose_recent",
        "ensemble_calls",
        ["tenant_id", "purpose", "invoked_at"],
    )
    op.execute("ALTER TABLE ensemble_calls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ensemble_calls FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON ensemble_calls "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON ensemble_calls")
    op.execute("ALTER TABLE ensemble_calls NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ensemble_calls DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_ec_purpose_recent", table_name="ensemble_calls")
    op.drop_index("ix_ec_high_divergence", table_name="ensemble_calls")
    op.drop_index("ix_ec_invoked_at", table_name="ensemble_calls")
    op.drop_table("ensemble_calls")
