"""Pending action executor tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.core.orm import WorldActionExecutionRow
from kun.datamodel.decision_ticket import ticket_from_world_policy
from kun.engineering.action_executor import (
    _action_type_needs_fail_closed,
    _claim_approved_action_stmt,
    _count_unresolved_actions_stmt,
    _enqueue_world_action_problem,
    _execution_blocked_message,
    _execution_failed_message,
    _execution_message,
    _executor_blocked_payload,
    _executor_error_payload,
    _executor_payload,
    _fail_world_action_execution,
    _finish_world_action_execution,
    _gateway_result_blocks_resume,
    _handler_health_blocked_result,
    _handler_health_blocks_execution,
    _health_lookup_failed_card,
    _mark_task_result_queued_stmt,
    _resume_request_payload,
    _unblock_paused_runtime_stmt,
    _world_action_credit_inputs,
)
from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue
from kun.world.gateway import WorldGatewayResult
from kun.world.handler_health import WorldHandlerHealthCard
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
    resume_request = _resume_request_payload("act-1", datetime.now(UTC))
    compiled = _unblock_paused_runtime_stmt(
        "u-sylvan",
        "task-1",
        datetime.now(UTC),
        resume_request=resume_request,
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "UPDATE runtime_states" in sql
    assert "runtime_states.status = " in sql
    assert "runtime_states.blob || " in sql
    assert compiled.params["param_1"] == {"resume_request": resume_request}


@pytest.mark.unit
def test_unblock_runtime_can_stay_legacy_without_resume_request() -> None:
    sql = str(
        _unblock_paused_runtime_stmt(
            "u-sylvan",
            "task-1",
            datetime.now(UTC),
        ).compile(dialect=postgresql.dialect())
    )

    assert "UPDATE runtime_states" in sql
    assert "runtime_states.status = " in sql
    assert "runtime_states.blob || " not in sql


@pytest.mark.unit
def test_mark_task_result_queued_updates_cached_json() -> None:
    resume_request = _resume_request_payload("act-1", datetime.now(UTC))
    compiled = _mark_task_result_queued_stmt(
        "u-sylvan",
        "task-1",
        datetime.now(UTC),
        resume_request=resume_request,
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    payload_patch = compiled.params["param_1"]

    assert "UPDATE task_results" in sql
    assert "result_json=(" in sql
    assert "||" in sql
    assert payload_patch == {
        "status": "queued",
        "answer": "审批已通过，任务已解除阻塞，等待恢复执行。",
        "resume_ready": True,
        "resume_request": resume_request,
    }


@pytest.mark.unit
def test_resume_request_payload_is_specific_and_retriable() -> None:
    now = datetime.now(UTC)
    payload = _resume_request_payload("act-1", now)

    assert payload["needed"] is True
    assert payload["status"] == "queued"
    assert payload["reason"] == "all_pending_actions_executed"
    assert payload["pending_action_ids"] == ["act-1"]
    assert payload["attempts"] == 0
    assert payload["requested_at"] == now.isoformat()


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


@pytest.mark.unit
def test_handler_health_block_prevents_external_execution() -> None:
    card = WorldHandlerHealthCard(
        action_type="email.send",
        status="blocked",
        registered=True,
        configured=True,
        recommendation="暂停自动执行，必须人工确认并排查失败原因。",
        issues=["最近 3 次执行失败"],
        failure_rate=0.5,
    )

    result = _handler_health_blocked_result(action_id="act-1", health_card=card)

    assert _handler_health_blocks_execution(card) is True
    assert result.gateway_mode == "policy_blocked"
    assert result.external_dispatched is False
    assert result.requires_handler is True
    assert result.audit["policy"]["source"] == "nuo.handler_health"
    assert "安全拦截" in result.user_summary


@pytest.mark.unit
def test_external_actions_fail_closed_when_health_lookup_fails() -> None:
    assert _action_type_needs_fail_closed("email.send") is True
    assert _action_type_needs_fail_closed("enterprise_api.post") is True
    assert _action_type_needs_fail_closed("local_file.write") is False

    card = _health_lookup_failed_card(action_type="email.send", exc=RuntimeError("db down"))

    assert card.status == "blocked"
    assert card.external_dispatched is True
    assert card.has_compensation is False
    assert _handler_health_blocks_execution(card) is True
    assert "fail-closed" in " ".join(card.issues)


@pytest.mark.unit
def test_handler_health_blocks_limited_external_handler_without_compensation() -> None:
    card = WorldHandlerHealthCard(
        action_type="email.send",
        status="limited",
        external_dispatched=True,
        registered=True,
        configured=True,
        has_compensation=False,
        recommendation="保留人工确认；先补齐补偿和失败复盘。",
        issues=["补偿策略不清楚"],
    )

    assert _handler_health_blocks_execution(card) is True


@pytest.mark.unit
def test_handler_health_blocks_limited_handler_missing_config() -> None:
    card = WorldHandlerHealthCard(
        action_type="enterprise_api.post",
        status="limited",
        external_dispatched=True,
        registered=True,
        configured=False,
        has_compensation=True,
        recommendation="补齐租户密钥后再执行。",
        issues=["缺少全局或租户级环境变量"],
    )

    assert _handler_health_blocks_execution(card) is True


@pytest.mark.unit
def test_world_action_credit_tracks_handler_action_and_policy_ticket() -> None:
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="handler_executed",
        capability_status="supported_execute",
        external_dispatched=True,
        requires_handler=False,
        audit={"handler_id": "email.send.v1"},
    )
    ticket = ticket_from_world_policy(
        tenant_id="tenant-1",
        task_id="task-1",
        action_id="act-1",
        action_type="email.send",
        risk_level="high",
        gateway_mode=gateway_result.gateway_mode,
        external_dispatched=gateway_result.external_dispatched,
        requires_handler=gateway_result.requires_handler,
        reason="handler executed",
    )

    resources, reward, outcome = _world_action_credit_inputs(
        action_type="email.send",
        gateway_result=gateway_result,
        decision_ticket=ticket,
    )

    assert resources["world_action"] == ["email.send"]
    assert resources["world_handler"] == ["email.send.v1"]
    assert resources["world_gateway_mode"] == ["handler_executed"]
    assert resources["decision_ticket"] == [ticket.ticket_id]
    assert reward == 0.9
    assert outcome == "pass"


@pytest.mark.unit
def test_world_action_credit_marks_blocked_handler_as_failure() -> None:
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=True,
    )
    ticket = ticket_from_world_policy(
        tenant_id="tenant-1",
        task_id="task-1",
        action_id="act-1",
        action_type="email.send",
        risk_level="high",
        gateway_mode=gateway_result.gateway_mode,
        external_dispatched=gateway_result.external_dispatched,
        requires_handler=gateway_result.requires_handler,
        reason="blocked",
    )

    resources, reward, outcome = _world_action_credit_inputs(
        action_type="email.send",
        gateway_result=gateway_result,
        decision_ticket=ticket,
    )

    assert resources["world_action"] == ["email.send"]
    assert "world_handler" not in resources
    assert reward == 0.15
    assert outcome == "fail"


@pytest.mark.unit
def test_finish_world_action_execution_persists_gateway_audit() -> None:
    now = datetime.now(UTC)
    row = WorldActionExecutionRow(
        tenant_id="tenant-1",
        action_id="act-1",
        task_ref="task-1",
        action_type="email.send",
        target_ref="user@example.com",
        idempotency_key="act-1",
        attempt_count=1,
    )
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="handler_executed",
        capability_status="supported_execute",
        external_dispatched=True,
        requires_handler=False,
        audit={
            "handler_id": "email.send.smtp.v1",
            "artifact_ref": "smtp://message/1",
            "policy": {
                "retry_policy": "不自动重试",
                "compensation_strategy": "发送更正邮件",
            },
        },
        message="sent",
    )

    _finish_world_action_execution(
        row,
        now,
        status="executed",
        gateway_result=gateway_result,
        decision_ticket={"ticket_id": "ticket-1"},
    )

    assert row.status == "executed"
    assert row.handler_id == "email.send.smtp.v1"
    assert row.external_dispatched is True
    assert row.requires_handler is False
    assert row.artifact_ref == "smtp://message/1"
    assert row.retry_policy == "不自动重试"
    assert row.compensation_strategy == "发送更正邮件"
    assert row.last_error == ""
    assert row.audit_json["gateway_mode"] == "handler_executed"
    assert row.decision_ticket_json == {"ticket_id": "ticket-1"}
    assert row.completed_at == now


@pytest.mark.unit
def test_fail_world_action_execution_records_error() -> None:
    now = datetime.now(UTC)
    row = WorldActionExecutionRow(
        tenant_id="tenant-1",
        action_id="act-1",
        task_ref="task-1",
        action_type="browser.execute",
        target_ref="https://example.com",
        idempotency_key="act-1",
        attempt_count=1,
    )

    _fail_world_action_execution(row, now, RuntimeError("browser crashed"))

    assert row.status == "failed"
    assert row.gateway_mode == "handler_failed"
    assert row.last_error == "browser crashed"
    assert row.audit_json["error"] == "browser crashed"
    assert row.completed_at == now


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_action_problem_is_fed_to_qi_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_qi_problem_queue()
    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    gateway_result = WorldGatewayResult(
        action_id="act-1",
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=True,
    )

    await _enqueue_world_action_problem(
        tenant_id="tenant-1",
        task_ref="task-1",
        action_id="act-1",
        action_type="email.send",
        severity="error",
        summary="WorldGateway handler health blocked approved action",
        gateway_result=gateway_result,
    )

    signals = get_qi_problem_queue().list("tenant-1")

    assert len(signals) == 1
    assert signals[0].category == "world_gateway"
    assert signals[0].source == "action_executor"
    assert signals[0].evidence["action_type"] == "email.send"
    assert signals[0].evidence["gateway"]["gateway_mode"] == "policy_blocked"
    reset_qi_problem_queue()
