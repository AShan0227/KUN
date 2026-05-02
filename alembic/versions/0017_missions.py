"""Add mission tables for long-horizon V3 work.

Revision ID: 0017_missions
Revises: 0016_pheromone
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_missions"
down_revision: str | None = "0016_pheromone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_EXPR = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    op.create_table(
        "missions",
        sa.Column("mission_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("budget_cap_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "success_metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "strategy_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned', 'running', 'paused', 'done', 'failed', 'cancelled')",
            name="mission_status_valid",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="mission_risk_level_valid",
        ),
        sa.CheckConstraint("budget_cap_usd >= 0", name="mission_budget_nonnegative"),
        sa.CheckConstraint("length(title) > 0", name="mission_title_not_empty"),
        sa.CheckConstraint("length(objective) > 0", name="mission_objective_not_empty"),
        sa.PrimaryKeyConstraint("mission_id"),
    )
    op.create_index("ix_missions_tenant_id", "missions", ["tenant_id"])
    op.create_index("ix_missions_tenant_status", "missions", ["tenant_id", "status"])
    op.create_index("ix_missions_tenant_project", "missions", ["tenant_id", "project_id"])

    op.create_table(
        "mission_tasks",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("mission_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="primary"),
        sa.Column("sequence_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column(
            "checkpoint_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("resume_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_resume_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'queued', 'running', 'paused', 'blocked', 'done', "
            "'failed', 'cancelled')",
            name="mission_task_status_valid",
        ),
        sa.CheckConstraint("sequence_no >= 0", name="mission_task_sequence_nonnegative"),
        sa.CheckConstraint(
            "resume_attempts >= 0",
            name="mission_task_resume_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["mission_id"], ["missions.mission_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "mission_id", "task_id"),
    )
    op.create_index("ix_mission_tasks_tenant_task", "mission_tasks", ["tenant_id", "task_id"])
    op.create_index(
        "ix_mission_tasks_tenant_status",
        "mission_tasks",
        ["tenant_id", "mission_id", "status"],
    )

    op.create_table(
        "mission_milestones",
        sa.Column("milestone_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("mission_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column("sequence_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("task_ref", sa.String(64), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "checkpoint_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned', 'active', 'done', 'blocked', 'cancelled')",
            name="mission_milestone_status_valid",
        ),
        sa.CheckConstraint("sequence_no >= 0", name="mission_milestone_sequence_nonnegative"),
        sa.CheckConstraint("length(title) > 0", name="mission_milestone_title_not_empty"),
        sa.ForeignKeyConstraint(["mission_id"], ["missions.mission_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id"),
    )
    op.create_index("ix_mission_milestones_tenant_id", "mission_milestones", ["tenant_id"])
    op.create_index("ix_mission_milestones_task_ref", "mission_milestones", ["task_ref"])
    op.create_index(
        "ix_mission_milestones_tenant_mission",
        "mission_milestones",
        ["tenant_id", "mission_id", "status"],
    )

    for table in ("missions", "mission_tasks", "mission_milestones"):
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
    for table in ("mission_milestones", "mission_tasks", "missions"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_mission_milestones_tenant_mission", table_name="mission_milestones")
    op.drop_index("ix_mission_milestones_task_ref", table_name="mission_milestones")
    op.drop_index("ix_mission_milestones_tenant_id", table_name="mission_milestones")
    op.drop_table("mission_milestones")
    op.drop_index("ix_mission_tasks_tenant_status", table_name="mission_tasks")
    op.drop_index("ix_mission_tasks_tenant_task", table_name="mission_tasks")
    op.drop_table("mission_tasks")
    op.drop_index("ix_missions_tenant_project", table_name="missions")
    op.drop_index("ix_missions_tenant_status", table_name="missions")
    op.drop_index("ix_missions_tenant_id", table_name="missions")
    op.drop_table("missions")
