"""RSI data spine — 7 tables for ADR-024 治理层 (L5).

闭环 = 数据流动. 这 7 张表是 RSI 闭环 + RCDH + Anti-drift 的数据脊柱:

  - runtime_capabilities    : Gate 写, Executor 读 (capability 晋级 → 实际启用)
  - runtime_experiments     : Strategist 写, Executor/Tester 读 (候选策略 → 真跑实验)
  - strategy_search_requests: Supervisor 写, Strategist 读 (异常 → 自动触发探索)
  - diagnostic_records      : Supervisor 走 RCDH 写, Gate 读 (诊断 → 引导修复)
  - goal_anchors            : Director 写, Executor system prompt 顶部读 (长任务防漂移)
  - plan_reviews            : Supervisor/Executor/ExtSupervisor 三方写 (漂移检测闭环)
  - evidence_ledger         : 全链路证据账本 (Gate 准入用 + 复盘回放用)

All tables 走 ADR-007 RLS: tenant_id 主键 + ENABLE/FORCE ROW LEVEL SECURITY +
tenant_isolation policy.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"

NEW_TABLES = (
    "runtime_capabilities",
    "runtime_experiments",
    "strategy_search_requests",
    "diagnostic_records",
    "goal_anchors",
    "plan_reviews",
    "evidence_ledger",
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({TENANT_POLICY_EXPR}) WITH CHECK ({TENANT_POLICY_EXPR})"
    )


def _disable_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


# ===================== runtime_capabilities =====================


def _create_runtime_capabilities() -> None:
    op.create_table(
        "runtime_capabilities",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("capability_id", sa.String(length=64), nullable=False),
        sa.Column("target_module", sa.String(length=256), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "promotion_state",
            sa.String(length=32),
            nullable=False,
            server_default="merged",
        ),
        sa.Column(
            "promotion_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("promotion_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rollback_on", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "sampling_rate",
            sa.Numeric(precision=5, scale=4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "promotion_state IN ('merged','in_replay','in_shadow','in_canary',"
            "'ready','enabled','rolled_back','expired')",
            name="runtime_capabilities_state_valid",
        ),
        sa.CheckConstraint(
            "sampling_rate >= 0 AND sampling_rate <= 1",
            name="runtime_capabilities_sampling_range",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "capability_id", name="pk_runtime_capabilities"),
    )
    op.create_index(
        "ix_runtime_capabilities_enabled",
        "runtime_capabilities",
        ["tenant_id", "target_module"],
        postgresql_where=sa.text("enabled = TRUE"),
    )
    op.create_index(
        "ix_runtime_capabilities_state",
        "runtime_capabilities",
        ["tenant_id", "promotion_state"],
    )


# ===================== runtime_experiments =====================


def _create_runtime_experiments() -> None:
    op.create_table(
        "runtime_experiments",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("target_module", sa.String(length=256), nullable=False),
        sa.Column("target_level", sa.Integer(), nullable=False),
        sa.Column("change_spec", JSONB(), nullable=False),
        sa.Column("rollout_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "sampling_rate",
            sa.Numeric(precision=5, scale=4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("success_metric", sa.String(length=128), nullable=False),
        sa.Column("acceptance_threshold", sa.Numeric(), nullable=False),
        sa.Column("rollback_on", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "target_level >= 0 AND target_level <= 3",
            name="runtime_experiments_level_range",
        ),
        sa.CheckConstraint(
            "rollout_mode IN ('shadow','canary','ab')",
            name="runtime_experiments_rollout_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','rolled_back')",
            name="runtime_experiments_status_valid",
        ),
        sa.CheckConstraint(
            "sampling_rate >= 0 AND sampling_rate <= 1",
            name="runtime_experiments_sampling_range",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "experiment_id", name="pk_runtime_experiments"),
    )
    op.create_index(
        "ix_runtime_experiments_active",
        "runtime_experiments",
        ["tenant_id", "target_module", "status"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


# ===================== strategy_search_requests =====================


def _create_strategy_search_requests() -> None:
    op.create_table(
        "strategy_search_requests",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column("target_module", sa.String(length=256), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "priority",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("dedup_key", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "priority IN ('low','medium','high')",
            name="strategy_search_priority_valid",
        ),
        sa.CheckConstraint(
            "status IN ('open','claimed','done','dropped')",
            name="strategy_search_status_valid",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('anomaly_threshold','external_supervisor','human','self')",
            name="strategy_search_triggered_by_valid",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "request_id", name="pk_strategy_search_requests"),
    )
    # dedup_key + open status: 工程层做 1 小时去重时查这个索引
    op.create_index(
        "ix_strategy_search_dedup_open",
        "strategy_search_requests",
        ["tenant_id", "dedup_key", "created_at"],
        postgresql_where=sa.text("status = 'open'"),
    )


# ===================== diagnostic_records =====================


def _create_diagnostic_records() -> None:
    op.create_table(
        "diagnostic_records",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("diagnostic_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_by_event_id", sa.String(length=64), nullable=False),
        sa.Column("symptom_summary", sa.Text(), nullable=False),
        sa.Column("repeat_history_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level_0_check", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("level_1_check", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("level_2_check", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("level_3_check", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("root_cause_level", sa.Integer(), nullable=True),
        sa.Column("recommended_action", sa.String(length=32), nullable=True),
        sa.Column("scope_modules", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "root_cause_level IS NULL OR (root_cause_level >= 0 AND root_cause_level <= 3)",
            name="diagnostic_root_cause_level_range",
        ),
        sa.CheckConstraint(
            "recommended_action IS NULL OR recommended_action IN "
            "('redesign','activate','module_rsi','code_fix')",
            name="diagnostic_recommended_action_valid",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(scope_modules) <= 5",
            name="diagnostic_scope_max_5",
        ),
        sa.CheckConstraint(
            "repeat_history_count >= 0",
            name="diagnostic_repeat_history_nonneg",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "diagnostic_id", name="pk_diagnostic_records"),
    )
    op.create_index(
        "ix_diagnostic_records_symptom",
        "diagnostic_records",
        ["tenant_id", "symptom_summary"],
    )
    op.create_index(
        "ix_diagnostic_records_created",
        "diagnostic_records",
        ["tenant_id", "created_at"],
    )


# ===================== goal_anchors =====================


def _create_goal_anchors() -> None:
    op.create_table(
        "goal_anchors",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("anchor_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("goal_statement", sa.Text(), nullable=False),
        sa.Column(
            "success_criteria", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("out_of_scope", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("invariants", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("immutable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(goal_statement) <= 200",
            name="goal_anchor_statement_max_200",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "anchor_id", name="pk_goal_anchors"),
    )
    op.create_index(
        "ix_goal_anchors_task",
        "goal_anchors",
        ["tenant_id", "task_id"],
    )


# ===================== plan_reviews =====================


def _create_plan_reviews() -> None:
    op.create_table(
        "plan_reviews",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("anchor_id", sa.String(length=64), nullable=False),
        sa.Column("triggered_at_step", sa.Integer(), nullable=False),
        sa.Column(
            "triggered_at_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "executor_self_report",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("supervisor_verdict", sa.String(length=32), nullable=False),
        sa.Column("drift_evidence", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("external_supervisor_verify", JSONB(), nullable=True),
        sa.Column("action_taken", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "supervisor_verdict IN ('ok','mild_drift','heavy_drift')",
            name="plan_review_verdict_valid",
        ),
        sa.CheckConstraint(
            "action_taken IN ('continue','remind','pause','rsi_trigger')",
            name="plan_review_action_valid",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "review_id", name="pk_plan_reviews"),
    )
    op.create_index(
        "ix_plan_reviews_task_time",
        "plan_reviews",
        ["tenant_id", "task_id", "triggered_at_time"],
    )


# ===================== evidence_ledger =====================


def _create_evidence_ledger() -> None:
    op.create_table(
        "evidence_ledger",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        # 关联到 RSI 治理表 (nullable)
        sa.Column("diagnostic_id", sa.String(length=64), nullable=True),
        sa.Column("external_supervisor_debrief_id", sa.String(length=64), nullable=True),
        sa.Column("diagnostic_level_reached", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('artifact','test_report','diagnostic','debrief','decision')",
            name="evidence_ledger_kind_valid",
        ),
        sa.CheckConstraint(
            "diagnostic_level_reached IS NULL OR "
            "(diagnostic_level_reached >= 0 AND diagnostic_level_reached <= 3)",
            name="evidence_ledger_level_range",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "entry_id", name="pk_evidence_ledger"),
    )
    op.create_index(
        "ix_evidence_ledger_task",
        "evidence_ledger",
        ["tenant_id", "task_id", "created_at"],
    )
    op.create_index(
        "ix_evidence_ledger_diagnostic",
        "evidence_ledger",
        ["tenant_id", "diagnostic_id"],
        postgresql_where=sa.text("diagnostic_id IS NOT NULL"),
    )


# ===================== upgrade / downgrade =====================


def upgrade() -> None:
    _create_runtime_capabilities()
    _create_runtime_experiments()
    _create_strategy_search_requests()
    _create_diagnostic_records()
    _create_goal_anchors()
    _create_plan_reviews()
    _create_evidence_ledger()

    # RLS for all 7 tables (ADR-007)
    for table in NEW_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in NEW_TABLES:
        _disable_rls(table)

    op.drop_index("ix_evidence_ledger_diagnostic", table_name="evidence_ledger")
    op.drop_index("ix_evidence_ledger_task", table_name="evidence_ledger")
    op.drop_table("evidence_ledger")

    op.drop_index("ix_plan_reviews_task_time", table_name="plan_reviews")
    op.drop_table("plan_reviews")

    op.drop_index("ix_goal_anchors_task", table_name="goal_anchors")
    op.drop_table("goal_anchors")

    op.drop_index("ix_diagnostic_records_created", table_name="diagnostic_records")
    op.drop_index("ix_diagnostic_records_symptom", table_name="diagnostic_records")
    op.drop_table("diagnostic_records")

    op.drop_index("ix_strategy_search_dedup_open", table_name="strategy_search_requests")
    op.drop_table("strategy_search_requests")

    op.drop_index("ix_runtime_experiments_active", table_name="runtime_experiments")
    op.drop_table("runtime_experiments")

    op.drop_index("ix_runtime_capabilities_state", table_name="runtime_capabilities")
    op.drop_index("ix_runtime_capabilities_enabled", table_name="runtime_capabilities")
    op.drop_table("runtime_capabilities")
