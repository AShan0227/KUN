"""Harden core numeric and enum constraints.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    (
        "tasks",
        "ck_tasks_estimated_cost_nonnegative",
        "estimated_cost_usd >= 0",
    ),
    (
        "tasks",
        "ck_tasks_estimated_duration_nonnegative",
        "estimated_duration_sec >= 0",
    ),
    ("tasks", "ck_tasks_task_version_positive", "version >= 1"),
    ("tasks", "ck_tasks_success_criteria_not_empty", "length(success_criteria_short) > 0"),
    (
        "runtime_states",
        "ck_runtime_states_runtime_actual_cost_nonnegative",
        "accumulated_cost_usd_actual >= 0",
    ),
    (
        "runtime_states",
        "ck_runtime_states_runtime_equivalent_cost_nonnegative",
        "accumulated_cost_usd_equivalent >= 0",
    ),
    (
        "runtime_states",
        "ck_runtime_states_runtime_tokens_nonnegative",
        "accumulated_tokens >= 0",
    ),
    (
        "runtime_states",
        "ck_runtime_states_runtime_failures_nonnegative",
        "failures_this_run >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_actual_cost_nonnegative",
        "cost_usd_actual >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_equivalent_cost_nonnegative",
        "cost_usd_equivalent >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_tokens_in_nonnegative",
        "tokens_in >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_tokens_out_nonnegative",
        "tokens_out >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_duration_nonnegative",
        "duration_sec >= 0",
    ),
    (
        "task_results",
        "ck_task_results_task_result_surprise_score_range",
        "surprise_score >= 0 AND surprise_score <= 1",
    ),
    (
        "capability_cards",
        "ck_capability_cards_capability_entity_type_valid",
        "entity_type IN ('role_template', 'model', 'skill', 'tool', 'human', 'external_agent')",
    ),
    (
        "capability_cards",
        "ck_capability_cards_capability_maturity_valid",
        "maturity IN ('cold_start', 'warming_up', 'mature')",
    ),
    ("capability_cards", "ck_capability_cards_capability_version_positive", "version >= 1"),
    (
        "capability_cards",
        "ck_capability_cards_capability_reliability_range",
        "overall_reliability >= 0 AND overall_reliability <= 1",
    ),
    (
        "notifications",
        "ck_notifications_notification_severity_valid",
        "severity IN ('info', 'insight', 'warn', 'error')",
    ),
    (
        "notifications",
        "ck_notifications_notification_channel_valid",
        "channel IN ('main', 'side', 'email', 'webhook', 'push', 'silent')",
    ),
    ("idempotency_keys", "ck_idempotency_keys_idempotency_ttl_positive", "ttl_sec > 0"),
)


def upgrade() -> None:
    for table, name, condition in CONSTRAINTS:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    for table, name, _condition in reversed(CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
