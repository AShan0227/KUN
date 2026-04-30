"""Pending action executor tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.engineering.action_executor import (
    _claim_approved_action_stmt,
    _count_unresolved_actions_stmt,
    _execution_blocked_message,
    _execution_failed_message,
    _execution_message,
    _executor_blocked_payload,
    _executor_error_payload,
    _executor_payload,
    _gateway_result_blocks_resume,
    _mark_task_result_queued_stmt,
    _unblock_paused_runtime_stmt,
)
from kun.world.gateway import WorldGatewayResult
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
def test_executor_payload_embeds_gateway_handler_result() -> None:
    now = datetime.now(UTC)
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="handler_executed",
        external_dispatched=True,
        requires_handler=False,
        audit={"handler_id": "local_file.write.v1"},
    )

    payload = _executor_payload({}, now, gateway_result=gateway_result)

    assert payload["executor"]["gateway"]["gateway_mode"] == "handler_executed"
    assert payload["executor"]["gateway"]["requires_handler"] is False
    assert payload["executor"]["gateway"]["audit"]["handler_id"] == "local_file.write.v1"
    assert "low-risk delivery handler" in payload["executor"]["note"]


@pytest.mark.unit
def test_executor_payload_is_honest_when_handler_is_missing() -> None:
    now = datetime.now(UTC)
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="approval_gate",
        external_dispatched=False,
        requires_handler=True,
    )

    payload = _executor_payload({}, now, gateway_result=gateway_result)

    assert "no delivery handler" in payload["executor"]["note"]


@pytest.mark.unit
def test_gateway_blocked_result_must_not_resume_task() -> None:
    blocked = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="policy_blocked",
        capability_status="supported_execute",
        external_dispatched=False,
        requires_handler=False,
        message="blocked",
    )
    missing = WorldGatewayResult(
        action_id="act-2",
        gateway_mode="approval_gate",
        capability_status="missing_handler",
        external_dispatched=False,
        requires_handler=True,
    )
    executed = WorldGatewayResult(
        action_id="act-3",
        gateway_mode="handler_drafted",
        capability_status="supported_draft",
        external_dispatched=False,
        requires_handler=False,
    )

    assert _gateway_result_blocks_resume(blocked) is True
    assert _gateway_result_blocks_resume(missing) is True
    assert _gateway_result_blocks_resume(executed) is False


@pytest.mark.unit
def test_executor_blocked_payload_is_not_marked_executed() -> None:
    now = datetime.now(UTC)
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="policy_blocked",
        external_dispatched=False,
        requires_handler=False,
    )

    payload = _executor_blocked_payload({}, now, gateway_result=gateway_result)

    assert payload["executor"]["status"] == "blocked"
    assert "remains paused" in payload["executor"]["note"]
    assert "policy blocked" in _execution_blocked_message(gateway_result)


@pytest.mark.unit
def test_executor_error_payload_keeps_failure_visible() -> None:
    now = datetime.now(UTC)
    payload = _executor_error_payload({}, now, ValueError("bad path"))

    assert payload["executor"]["status"] == "failed"
    assert payload["executor"]["error"] == "bad path"
    assert "remains paused" in payload["executor"]["note"]


@pytest.mark.unit
def test_execution_message_mentions_unblocked_queue() -> None:
    assert "unblocked to queued" in _execution_message("queued")


@pytest.mark.unit
def test_execution_message_includes_gateway_message() -> None:
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="handler_drafted",
        external_dispatched=False,
        requires_handler=False,
        message="Email draft created. It was not sent.",
    )

    assert "Email draft created" in _execution_message("queued", gateway_result)


@pytest.mark.unit
def test_execution_failed_message_is_honest() -> None:
    assert "remains paused" in _execution_failed_message(ValueError("bad path"))
