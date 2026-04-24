"""Add core check constraints.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_tasks_risk_level_valid",
        "tasks",
        "risk_level IN ('low', 'medium', 'high', 'critical')",
    )
    op.create_check_constraint(
        "ck_tasks_complexity_score_range",
        "tasks",
        "complexity_score >= 0 AND complexity_score <= 1",
    )
    op.create_check_constraint(
        "ck_runtime_states_runtime_status_valid",
        "runtime_states",
        "status IN ('queued', 'running', 'paused', 'done', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_runtime_states_runtime_current_step_nonnegative",
        "runtime_states",
        "current_step >= 0",
    )
    op.create_check_constraint(
        "ck_runtime_states_runtime_total_steps_nonnegative",
        "runtime_states",
        "total_planned_steps >= 0",
    )
    op.create_check_constraint(
        "ck_experiments_experiment_status_valid",
        "experiments",
        "status IN ('draft', 'shadow', 'canary', 'rollout', 'stable', 'rolled_back')",
    )
    op.create_check_constraint(
        "ck_experiments_experiment_rollout_percent_range",
        "experiments",
        "rollout_percent >= 0 AND rollout_percent <= 100",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_experiments_experiment_rollout_percent_range",
        "experiments",
        type_="check",
    )
    op.drop_constraint("ck_experiments_experiment_status_valid", "experiments", type_="check")
    op.drop_constraint(
        "ck_runtime_states_runtime_total_steps_nonnegative",
        "runtime_states",
        type_="check",
    )
    op.drop_constraint(
        "ck_runtime_states_runtime_current_step_nonnegative",
        "runtime_states",
        type_="check",
    )
    op.drop_constraint("ck_runtime_states_runtime_status_valid", "runtime_states", type_="check")
    op.drop_constraint("ck_tasks_complexity_score_range", "tasks", type_="check")
    op.drop_constraint("ck_tasks_risk_level_valid", "tasks", type_="check")
