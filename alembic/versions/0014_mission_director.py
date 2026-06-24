"""mission_alignment_reviews + plan_change_proposals — Mission Director 持久化.

V7 §9.7 Mission Director (交付总监) 一级子系统从 0 建. Phase X.A 加了
service + frozen IO + emitter callback (kun/agents/mission_director/), 但
emitter 默认 None — 没 DB 接入就跑不到生产链路. 这条 migration 是 Phase X.B
"接真 DB" 的第一步.

两张表 (1 对 N 关系, 不强外键):
  mission_alignment_reviews — 每 tick / milestone 一条 (verdict + 3 coverage)
  plan_change_proposals     — 偏离明显时生成的方案变更提案 (severity 三档)

V7 §10.3.3 决策权 3 档 → severity enum:
  low    — KUN 自动 (log)
  medium — KUN 自动 (+ NUO panel, 可一键回滚)
  high   — 等用户审 (CollaborationTicket) — DB 不变量保证 high 必 user_approval_required

V7 §10.4 三级信号 → verdict enum:
  ok / drifting / off_anchor / needs_human

ADR-007 RLS: 两张表都 tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY
+ tenant_isolation policy (与 0011/0012/0013 风格一致).

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    # =========================================================
    # 1) mission_alignment_reviews
    # =========================================================
    op.create_table(
        "mission_alignment_reviews",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("task_plan_version", sa.String(length=64), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("alignment_score", sa.Numeric(4, 3), nullable=False),
        sa.Column(
            "findings",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("info_gap_coverage", sa.Numeric(4, 3), nullable=False),
        sa.Column("decomposition_coverage", sa.Numeric(4, 3), nullable=False),
        sa.Column("evidence_coverage", sa.Numeric(4, 3), nullable=False),
        sa.Column(
            "plan_change_proposed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "plan_change_proposal_id", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "verdict IN ('ok', 'drifting', 'off_anchor', 'needs_human')",
            name="mar_verdict_valid",
        ),
        sa.CheckConstraint(
            "alignment_score >= 0 AND alignment_score <= 1",
            name="mar_score_in_range",
        ),
        sa.CheckConstraint(
            "info_gap_coverage >= 0 AND info_gap_coverage <= 1",
            name="mar_info_gap_in_range",
        ),
        sa.CheckConstraint(
            "decomposition_coverage >= 0 AND decomposition_coverage <= 1",
            name="mar_decomp_in_range",
        ),
        sa.CheckConstraint(
            "evidence_coverage >= 0 AND evidence_coverage <= 1",
            name="mar_evidence_in_range",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "review_id", name="pk_mission_alignment_reviews"
        ),
    )
    op.create_index(
        "ix_mar_task_reviewed_at",
        "mission_alignment_reviews",
        ["tenant_id", "task_id", "reviewed_at"],
    )
    op.create_index(
        "ix_mar_verdict",
        "mission_alignment_reviews",
        ["tenant_id", "verdict", "reviewed_at"],
    )
    op.execute("ALTER TABLE mission_alignment_reviews ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mission_alignment_reviews FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON mission_alignment_reviews "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )

    # =========================================================
    # 2) plan_change_proposals
    # =========================================================
    op.create_table(
        "plan_change_proposals",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_by", sa.String(length=32), nullable=False),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column(
            "affected_work_items",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "affected_deliverables",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "candidate_changes",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "rollback_condition", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "user_approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("user_decision", sa.Boolean(), nullable=True),
        sa.Column(
            "user_decided_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high')",
            name="pcp_severity_valid",
        ),
        sa.CheckConstraint(
            "change_type IN ('scope', 'criteria', 'resource', 'risk')",
            name="pcp_change_type_valid",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('mission_director', 'qi', 'nuo', "
            "'external_supervisor')",
            name="pcp_triggered_by_valid",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(candidate_changes) >= 1",
            name="pcp_candidate_changes_nonempty",
        ),
        sa.CheckConstraint(
            "NOT (severity = 'high' AND user_approval_required = false)",
            name="pcp_high_severity_needs_approval",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "proposal_id", name="pk_plan_change_proposals"
        ),
    )
    op.create_index(
        "ix_pcp_task_triggered_at",
        "plan_change_proposals",
        ["tenant_id", "task_id", "triggered_at"],
    )
    op.create_index(
        "ix_pcp_severity_pending",
        "plan_change_proposals",
        ["tenant_id", "severity", "user_decision"],
    )
    op.execute("ALTER TABLE plan_change_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE plan_change_proposals FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON plan_change_proposals "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON plan_change_proposals")
    op.execute(
        "ALTER TABLE plan_change_proposals NO FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE plan_change_proposals DISABLE ROW LEVEL SECURITY"
    )
    op.drop_index(
        "ix_pcp_severity_pending", table_name="plan_change_proposals"
    )
    op.drop_index(
        "ix_pcp_task_triggered_at", table_name="plan_change_proposals"
    )
    op.drop_table("plan_change_proposals")

    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation ON mission_alignment_reviews"
    )
    op.execute(
        "ALTER TABLE mission_alignment_reviews NO FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE mission_alignment_reviews DISABLE ROW LEVEL SECURITY"
    )
    op.drop_index(
        "ix_mar_verdict", table_name="mission_alignment_reviews"
    )
    op.drop_index(
        "ix_mar_task_reviewed_at", table_name="mission_alignment_reviews"
    )
    op.drop_table("mission_alignment_reviews")
