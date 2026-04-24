"""Pending action executor tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.engineering.action_executor import (
    _claim_approved_action_stmt,
    _count_unresolved_actions_stmt,
    _execution_message,
    _executor_payload,
    _mark_task_result_queued_stmt,
    _unblock_paused_runtime_stmt,
)
from sqlalchemy.dialects import postgresql


@pytest.mark.unit
def test_claim_approved_action_uses_row_lock() -> None:
    sql = str(
        _claim_approved_action_stmt("u-sylvan", "act-1").compile(dialect=postgresql.dialect())
    )

    assert "FROM pending_actions" in sql
    assert "pending_actions.status = " in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.unit
def test_count_unresolved_actions_only_counts_open_gate_states() -> None:
    sql = str(
        _count_unresolved_actions_stmt("u-sylvan", "task-1").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "count" in sql.lower()
    assert "pending_approval" in sql
    assert "approved" in sql


@pytest.mark.unit
def test_unblock_runtime_sets_task_back_to_queued() -> None:
    sql = str(
        _unblock_paused_runtime_stmt("u-sylvan", "task-1", datetime.now(UTC)).compile(
            dialect=postgresql.dialect()
        )
    )

    assert "UPDATE runtime_states" in sql
    assert "runtime_states.status = " in sql


@pytest.mark.unit
def test_mark_task_result_queued_updates_cached_json() -> None:
    sql = str(
        _mark_task_result_queued_stmt("u-sylvan", "task-1", datetime.now(UTC)).compile(
            dialect=postgresql.dialect()
        )
    )

    assert "UPDATE task_results" in sql
    assert "result_json=(" in sql
    assert "||" in sql


@pytest.mark.unit
def test_executor_payload_records_audit_note() -> None:
    now = datetime.now(UTC)
    payload = _executor_payload({"task_id": "task-1"}, now)

    assert payload["task_id"] == "task-1"
    assert payload["executor"]["mode"] == "approval_gate"
    assert payload["executor"]["executed_at"] == now.isoformat()


@pytest.mark.unit
def test_execution_message_mentions_unblocked_queue() -> None:
    assert "unblocked to queued" in _execution_message("queued")
