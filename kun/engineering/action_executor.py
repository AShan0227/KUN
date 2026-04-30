"""Pending side-effect action executor.

This is the first safe execution loop for NUO pending actions. It does not
perform arbitrary external side effects yet; it executes the approval gate:

1. claim one approved action under row lock,
2. mark it executed with audit metadata,
3. when all task actions are resolved, unblock the paused task back to queued.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.orm import (
    PendingActionRow,
    RuntimeStateRow,
    TaskResultRow,
    WorldActionExecutionRow,
)
from kun.core.state_ledger import get_state_ledger
from kun.datamodel.decision_ticket import DecisionTicket, ticket_from_world_policy
from kun.datamodel.events import Event
from kun.datamodel.runtime import TaskStatus
from kun.engineering.credit_assignment import CreditAssignment, persist_resource_credit_report
from kun.world.gateway import WorldAction, WorldGatewayResult, get_world_gateway
from kun.world.handler_health import WorldHandlerHealthCard, collect_world_handler_health


class ActionExecutionResult(BaseModel):
    """Outcome from one executor pass."""

    action_id: str
    task_ref: str
    action_status: str
    task_status: TaskStatus | None = None
    message: str
    gateway_result: WorldGatewayResult | None = None


async def execute_approved_action_once(
    *,
    tenant_id: str,
    action_id: str,
) -> ActionExecutionResult | None:
    """Execute one approved pending action and unblock its task if ready.

    Returns None when the action is no longer in `approved` state. This keeps
    the executor idempotent for repeated or racing approval calls.
    """
    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        action_result = await s.execute(_claim_approved_action_stmt(tenant_id, action_id))
        action = action_result.scalar_one_or_none()
        if action is None:
            return None

        task_ref = str(action.task_ref)
        idempotency_key = _world_action_idempotency_key(action)
        gateway_result: WorldGatewayResult | None = None
        if _action_type_needs_fail_closed(
            action.action_type
        ) and not _world_action_has_explicit_idempotency_key(action):
            gateway_result = _missing_idempotency_blocked_result(
                action=action,
                idempotency_key=idempotency_key,
            )
        else:
            duplicate_execution = await _find_duplicate_world_action_execution(
                s,
                action=action,
                idempotency_key=idempotency_key,
            )
            if duplicate_execution is not None and _duplicate_execution_blocks_action(action):
                gateway_result = _idempotency_blocked_result(
                    action=action,
                    duplicate_execution=duplicate_execution,
                    idempotency_key=idempotency_key,
                )

        if gateway_result is not None:
            execution_row = await _start_world_action_execution(
                s,
                action=action,
                now=now,
                initial_status="blocked",
            )
            world_ticket = ticket_from_world_policy(
                tenant_id=tenant_id,
                task_id=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                risk_level=action.risk_level,
                gateway_mode=gateway_result.gateway_mode,
                external_dispatched=False,
                requires_handler=False,
                policy=_dict_or_empty(gateway_result.audit.get("policy")),
                reason=gateway_result.message,
            )
            action.payload = _executor_blocked_payload(
                action.payload,
                now,
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            _finish_world_action_execution(
                execution_row,
                now,
                status="blocked",
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            action.status = "cancelled"
            action.updated_at = now
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.pending_action.blocked",
                    payload={
                        "task_id": task_ref,
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "target_ref": action.target_ref,
                        "executor_mode": gateway_result.gateway_mode,
                        "external_dispatched": False,
                        "requires_handler": False,
                        "capability_status": gateway_result.capability_status,
                        "decision_ticket": world_ticket.event_payload(),
                        "reason": gateway_result.message,
                    },
                    task_ref=task_ref,
                ),
            )
            get_state_ledger().record_decision_ticket(world_ticket)
            await _record_world_action_credit(
                s,
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_type=action.action_type,
                gateway_result=gateway_result,
                decision_ticket=world_ticket,
            )
            get_state_ledger().record_paused(
                task_ref,
                reason=_execution_blocked_message(gateway_result),
                pending_confirmations=[action.action_id],
            )
            await _enqueue_world_action_problem(
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                severity="warn",
                summary=_world_action_guard_problem_summary(gateway_result),
                gateway_result=gateway_result,
            )
            return ActionExecutionResult(
                action_id=action_id,
                task_ref=task_ref,
                action_status="cancelled",
                task_status="paused",
                message=_execution_blocked_message(gateway_result),
                gateway_result=gateway_result,
            )

        execution_row = await _start_world_action_execution(s, action=action, now=now)
        health_card = await _collect_handler_health_card(
            tenant_id=tenant_id,
            action_type=action.action_type,
        )
        if health_card is None and _action_type_needs_fail_closed(action.action_type):
            gateway_result = _handler_health_unknown_blocked_result(
                action_id=action.action_id,
                action_type=action.action_type,
            )
        elif health_card is not None and _handler_health_blocks_execution(health_card):
            gateway_result = _handler_health_blocked_result(
                action_id=action.action_id,
                health_card=health_card,
            )
        else:
            gateway_result = None
        if gateway_result is not None:
            world_ticket = ticket_from_world_policy(
                tenant_id=tenant_id,
                task_id=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                risk_level=action.risk_level,
                gateway_mode=gateway_result.gateway_mode,
                external_dispatched=False,
                requires_handler=True,
                policy={
                    "allowed": False,
                    "source": "nuo.handler_health",
                    "status": health_card.status if health_card is not None else "unknown",
                    "issues": health_card.issues
                    if health_card is not None
                    else ["handler health unknown"],
                },
                reason=gateway_result.message,
            )
            action.payload = _executor_blocked_payload(
                action.payload,
                now,
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            _finish_world_action_execution(
                execution_row,
                now,
                status="blocked",
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            action.status = "cancelled"
            action.updated_at = now
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.pending_action.blocked",
                    payload={
                        "task_id": task_ref,
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "target_ref": action.target_ref,
                        "executor_mode": gateway_result.gateway_mode,
                        "external_dispatched": False,
                        "requires_handler": True,
                        "capability_status": gateway_result.capability_status,
                        "decision_ticket": world_ticket.event_payload(),
                        "reason": gateway_result.message,
                    },
                    task_ref=task_ref,
                ),
            )
            get_state_ledger().record_decision_ticket(world_ticket)
            await _record_world_action_credit(
                s,
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_type=action.action_type,
                gateway_result=gateway_result,
                decision_ticket=world_ticket,
            )
            get_state_ledger().record_paused(
                task_ref,
                reason=_execution_blocked_message(gateway_result),
                pending_confirmations=[action.action_id],
            )
            await _enqueue_world_action_problem(
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                severity="error",
                summary="WorldGateway handler health blocked approved action",
                gateway_result=gateway_result,
            )
            return ActionExecutionResult(
                action_id=action_id,
                task_ref=task_ref,
                action_status="cancelled",
                task_status="paused",
                message=_execution_blocked_message(gateway_result),
                gateway_result=gateway_result,
            )

        try:
            gateway_result = await get_world_gateway().execute_approved(
                WorldAction(
                    action_id=action.action_id,
                    tenant_id=tenant_id,
                    task_ref=task_ref,
                    action_type=action.action_type,
                    target_ref=action.target_ref,
                    risk_level=action.risk_level,
                    payload=action.payload,
                )
            )
        except Exception as exc:
            action.payload = _executor_error_payload(action.payload, now, exc)
            _fail_world_action_execution(execution_row, now, exc)
            action.status = "cancelled"
            action.updated_at = now
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.pending_action.execution_failed",
                    payload={
                        "task_id": task_ref,
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "target_ref": action.target_ref,
                        "error": str(exc),
                    },
                    task_ref=task_ref,
                ),
            )
            get_state_ledger().record_paused(
                task_ref,
                reason=f"外部动作执行失败：{exc}",
                pending_confirmations=[action.action_id],
            )
            await _enqueue_world_action_problem(
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                severity="error",
                summary="WorldGateway handler execution failed",
                error=str(exc),
            )
            return ActionExecutionResult(
                action_id=action_id,
                task_ref=task_ref,
                action_status="cancelled",
                task_status="paused",
                message=_execution_failed_message(exc),
            )

        world_ticket = ticket_from_world_policy(
            tenant_id=tenant_id,
            task_id=task_ref,
            action_id=action.action_id,
            action_type=action.action_type,
            risk_level=action.risk_level,
            gateway_mode=gateway_result.gateway_mode,
            external_dispatched=gateway_result.external_dispatched,
            requires_handler=gateway_result.requires_handler,
            policy=_dict_or_empty(gateway_result.audit.get("policy")),
            reason=gateway_result.message,
        )
        if _gateway_result_blocks_resume(gateway_result):
            action.payload = _executor_blocked_payload(
                action.payload,
                now,
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            _finish_world_action_execution(
                execution_row,
                now,
                status="blocked",
                gateway_result=gateway_result,
                decision_ticket=world_ticket.event_payload(),
            )
            action.status = "cancelled"
            action.updated_at = now
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.pending_action.blocked",
                    payload={
                        "task_id": task_ref,
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "target_ref": action.target_ref,
                        "executor_mode": gateway_result.gateway_mode,
                        "external_dispatched": gateway_result.external_dispatched,
                        "requires_handler": gateway_result.requires_handler,
                        "capability_status": gateway_result.capability_status,
                        "decision_ticket": world_ticket.event_payload(),
                        "reason": gateway_result.message,
                    },
                    task_ref=task_ref,
                ),
            )
            get_state_ledger().record_decision_ticket(world_ticket)
            await _record_world_action_credit(
                s,
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_type=action.action_type,
                gateway_result=gateway_result,
                decision_ticket=world_ticket,
            )
            get_state_ledger().record_paused(
                task_ref,
                reason=_execution_blocked_message(gateway_result),
                pending_confirmations=[action.action_id],
            )
            await _enqueue_world_action_problem(
                tenant_id=tenant_id,
                task_ref=task_ref,
                action_id=action.action_id,
                action_type=action.action_type,
                severity="warn",
                summary="WorldGateway policy blocked approved action",
                gateway_result=gateway_result,
            )
            return ActionExecutionResult(
                action_id=action_id,
                task_ref=task_ref,
                action_status="cancelled",
                task_status="paused",
                message=_execution_blocked_message(gateway_result),
                gateway_result=gateway_result,
            )

        action.payload = _executor_payload(
            action.payload,
            now,
            gateway_result=gateway_result,
            decision_ticket=world_ticket.event_payload(),
        )
        _finish_world_action_execution(
            execution_row,
            now,
            status="executed",
            gateway_result=gateway_result,
            decision_ticket=world_ticket.event_payload(),
        )
        action.status = "executed"
        action.executed_at = now
        action.updated_at = now

        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="task.pending_action.executed",
                payload={
                    "task_id": task_ref,
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "target_ref": action.target_ref,
                    "executor_mode": gateway_result.gateway_mode,
                    "external_dispatched": gateway_result.external_dispatched,
                    "requires_handler": gateway_result.requires_handler,
                    "handler_id": gateway_result.audit.get("handler_id"),
                    "decision_ticket": world_ticket.event_payload(),
                },
                task_ref=task_ref,
            ),
        )
        get_state_ledger().record_world_action_executed(
            task_ref,
            action_id=action.action_id,
            action_type=action.action_type,
            gateway_mode=gateway_result.gateway_mode,
            external_dispatched=gateway_result.external_dispatched,
            requires_handler=gateway_result.requires_handler,
            handler_id=_optional_str(gateway_result.audit.get("handler_id")),
            artifact_ref=_optional_str(gateway_result.audit.get("artifact_ref")),
            message=gateway_result.message,
            decision_ticket=world_ticket,
        )
        await _record_world_action_credit(
            s,
            tenant_id=tenant_id,
            task_ref=task_ref,
            action_type=action.action_type,
            gateway_result=gateway_result,
            decision_ticket=world_ticket,
        )

        unresolved = await s.execute(_count_unresolved_actions_stmt(tenant_id, task_ref))
        unresolved_count = int(unresolved.scalar_one())
        if unresolved_count > 0:
            return ActionExecutionResult(
                action_id=action_id,
                task_ref=task_ref,
                action_status="executed",
                task_status="paused",
                message=(
                    "Action executed. Task is still paused because other pending actions "
                    "are not resolved yet."
                ),
                gateway_result=gateway_result,
            )

        resume_request = _resume_request_payload(action.action_id, now)
        unblocked = await s.execute(
            _unblock_paused_runtime_stmt(
                tenant_id,
                task_ref,
                now,
                resume_request=resume_request,
            )
        )
        task_status: TaskStatus | None = "queued" if _rowcount(unblocked) > 0 else None
        if task_status == "queued":
            await s.execute(
                _mark_task_result_queued_stmt(
                    tenant_id,
                    task_ref,
                    now,
                    resume_request=resume_request,
                )
            )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.resumed",
                    payload={
                        "task_id": task_ref,
                        "reason": "all_pending_actions_executed",
                        "resume_state": "queued",
                    },
                    task_ref=task_ref,
                ),
            )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.continuation.enqueued",
                    payload={
                        "task_id": task_ref,
                        **resume_request,
                    },
                    task_ref=task_ref,
                ),
            )
            get_state_ledger().record_resumed(
                task_ref,
                reason="all_pending_actions_executed",
            )

        return ActionExecutionResult(
            action_id=action_id,
            task_ref=task_ref,
            action_status="executed",
            task_status=task_status,
            message=_execution_message(task_status, gateway_result),
            gateway_result=gateway_result,
        )


def _claim_approved_action_stmt(tenant_id: str, action_id: str) -> Any:
    return (
        select(PendingActionRow)
        .where(
            PendingActionRow.tenant_id == tenant_id,
            PendingActionRow.action_id == action_id,
            PendingActionRow.status == "approved",
        )
        .with_for_update(skip_locked=True)
    )


async def _start_world_action_execution(
    session: AsyncSession,
    *,
    action: PendingActionRow,
    now: datetime,
    initial_status: str = "claimed",
) -> WorldActionExecutionRow:
    """Create or claim the durable WorldGateway execution ledger row."""

    result = await session.execute(
        select(WorldActionExecutionRow)
        .where(
            WorldActionExecutionRow.tenant_id == action.tenant_id,
            WorldActionExecutionRow.action_id == action.action_id,
        )
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = WorldActionExecutionRow(
            tenant_id=action.tenant_id,
            action_id=action.action_id,
            task_ref=action.task_ref,
            action_type=action.action_type,
            target_ref=action.target_ref,
            idempotency_key=_world_action_idempotency_key(action),
            status=initial_status,
            attempt_count=1,
            first_attempt_at=now,
            last_attempt_at=now,
            updated_at=now,
            audit_json={"source": "pending_action_executor"},
        )
        session.add(row)
        await session.flush()
        return row

    row.status = initial_status
    row.attempt_count = int(row.attempt_count or 0) + 1
    if row.first_attempt_at is None:
        row.first_attempt_at = now
    row.last_attempt_at = now
    row.updated_at = now
    row.last_error = ""
    return row


async def _find_duplicate_world_action_execution(
    session: AsyncSession,
    *,
    action: PendingActionRow,
    idempotency_key: str,
) -> WorldActionExecutionRow | None:
    """Find a prior ledger row that already claimed the same side effect."""

    result = await session.execute(
        select(WorldActionExecutionRow)
        .where(
            WorldActionExecutionRow.tenant_id == action.tenant_id,
            WorldActionExecutionRow.idempotency_key == idempotency_key,
            WorldActionExecutionRow.action_id != action.action_id,
            WorldActionExecutionRow.status.in_(("claimed", "executed")),
        )
        .order_by(WorldActionExecutionRow.updated_at.desc())
        .with_for_update()
    )
    return result.scalars().first()


def _world_action_idempotency_key(action: PendingActionRow) -> str:
    value = action.payload.get("idempotency_key") or action.payload.get("idempotencyKey")
    cleaned = str(value or "").strip()
    return cleaned or str(action.action_id)


def _world_action_has_explicit_idempotency_key(action: PendingActionRow) -> bool:
    value = action.payload.get("idempotency_key") or action.payload.get("idempotencyKey")
    return bool(str(value or "").strip())


def _duplicate_execution_blocks_action(action: PendingActionRow) -> bool:
    return _action_type_needs_fail_closed(str(action.action_type))


def _finish_world_action_execution(
    row: WorldActionExecutionRow,
    now: datetime,
    *,
    status: str,
    gateway_result: WorldGatewayResult,
    decision_ticket: dict[str, Any],
) -> None:
    """Persist a terminal WorldGateway execution outcome."""

    audit = gateway_result.audit
    policy = _dict_or_empty(audit.get("policy"))
    row.status = status
    row.gateway_mode = gateway_result.gateway_mode
    row.capability_status = gateway_result.capability_status
    row.external_dispatched = gateway_result.external_dispatched
    row.requires_handler = gateway_result.requires_handler
    row.handler_id = _optional_str(audit.get("handler_id"))
    row.artifact_ref = _optional_str(audit.get("artifact_ref"))
    row.compensation_strategy = str(
        policy.get("compensation_strategy")
        or audit.get("compensation")
        or gateway_result.next_step
        or ""
    )
    row.retry_policy = str(policy.get("retry_policy") or "")
    row.last_error = "" if status == "executed" else gateway_result.message
    row.audit_json = gateway_result.model_dump(mode="json")
    row.decision_ticket_json = decision_ticket
    row.completed_at = now
    row.updated_at = now


def _fail_world_action_execution(
    row: WorldActionExecutionRow,
    now: datetime,
    exc: Exception,
) -> None:
    row.status = "failed"
    row.gateway_mode = "handler_failed"
    row.capability_status = "preview_failed"
    row.last_error = str(exc)
    row.audit_json = {"error": str(exc), "source": "pending_action_executor"}
    row.completed_at = now
    row.updated_at = now


def _count_unresolved_actions_stmt(tenant_id: str, task_ref: str) -> Any:
    return (
        select(func.count())
        .select_from(PendingActionRow)
        .where(
            PendingActionRow.tenant_id == tenant_id,
            PendingActionRow.task_ref == task_ref,
            PendingActionRow.status.in_(("pending_approval", "approved")),
        )
    )


def _unblock_paused_runtime_stmt(
    tenant_id: str,
    task_ref: str,
    now: datetime,
    *,
    resume_request: dict[str, Any] | None = None,
) -> Any:
    values: dict[str, Any] = {"status": "queued", "finished_at": None, "last_updated": now}
    if resume_request is not None:
        values["blob"] = RuntimeStateRow.blob.op("||")(
            literal({"resume_request": resume_request}, type_=JSONB)
        )
    return (
        update(RuntimeStateRow)
        .where(
            RuntimeStateRow.tenant_id == tenant_id,
            RuntimeStateRow.task_ref == task_ref,
            RuntimeStateRow.status == "paused",
        )
        .values(**values)
    )


def _mark_task_result_queued_stmt(
    tenant_id: str,
    task_ref: str,
    now: datetime,
    *,
    resume_request: dict[str, Any] | None = None,
) -> Any:
    answer = "审批已通过，任务已解除阻塞，等待恢复执行。"
    resume_request = resume_request or _resume_request_payload("", now)
    return (
        update(TaskResultRow)
        .where(
            TaskResultRow.tenant_id == tenant_id,
            TaskResultRow.task_id == task_ref,
            TaskResultRow.status == "paused",
        )
        .values(
            status="queued",
            answer=answer,
            updated_at=now,
            result_json=TaskResultRow.result_json.op("||")(
                literal(
                    {
                        "status": "queued",
                        "answer": answer,
                        "resume_ready": True,
                        "resume_request": resume_request,
                    },
                    type_=JSONB,
                )
            ),
        )
    )


def _resume_request_payload(action_id: str, now: datetime) -> dict[str, Any]:
    return {
        "needed": True,
        "status": "queued",
        "reason": "all_pending_actions_executed",
        "pending_action_ids": [action_id] if action_id else [],
        "attempts": 0,
        "requested_at": now.isoformat(),
    }


def _executor_payload(
    payload: dict[str, Any],
    now: datetime,
    *,
    gateway_result: WorldGatewayResult | None = None,
    decision_ticket: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(payload)
    gateway = gateway_result.model_dump(mode="json") if gateway_result is not None else {}
    merged["executor"] = {
        "mode": "approval_gate",
        "status": "executed",
        "executed_at": now.isoformat(),
        "gateway": gateway,
        "decision_ticket": decision_ticket or {},
        "note": _executor_note(gateway_result),
    }
    return merged


def _executor_blocked_payload(
    payload: dict[str, Any],
    now: datetime,
    *,
    gateway_result: WorldGatewayResult,
    decision_ticket: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(payload)
    merged["executor"] = {
        "mode": "approval_gate",
        "status": "blocked",
        "executed_at": now.isoformat(),
        "gateway": gateway_result.model_dump(mode="json"),
        "decision_ticket": decision_ticket or {},
        "note": _execution_blocked_message(gateway_result),
    }
    return merged


def _executor_error_payload(
    payload: dict[str, Any],
    now: datetime,
    exc: Exception,
) -> dict[str, Any]:
    merged = dict(payload)
    merged["executor"] = {
        "mode": "approval_gate",
        "status": "failed",
        "executed_at": now.isoformat(),
        "error": str(exc),
        "note": "World Gateway handler failed; task remains paused for human review.",
    }
    return merged


def _executor_note(gateway_result: WorldGatewayResult | None) -> str:
    if gateway_result is None:
        return "World Gateway approval gate executed."
    if gateway_result.requires_handler:
        return (
            "World Gateway recorded the approved side-effect request, but no delivery "
            "handler is attached for this action type yet."
        )
    if gateway_result.external_dispatched:
        return "World Gateway executed a registered low-risk delivery handler."
    return "World Gateway produced a registered draft or dry-run artifact; no external dispatch happened."


def _execution_failed_message(exc: Exception) -> str:
    return f"Action execution failed; task remains paused for review. Gateway error: {exc}"


def _execution_blocked_message(gateway_result: WorldGatewayResult) -> str:
    if gateway_result.gateway_mode == "policy_blocked":
        return (
            "Action was approved by the user, but World Gateway policy blocked execution; "
            "task remains paused."
        )
    if gateway_result.requires_handler:
        return (
            "Action was approved by the user, but no World Gateway handler is attached; "
            "task remains paused."
        )
    return (
        "Action was approved by the user, but World Gateway could not safely complete it; "
        "task remains paused."
    )


def _execution_message(
    task_status: TaskStatus | None,
    gateway_result: WorldGatewayResult | None = None,
) -> str:
    gateway_message = f" Gateway: {gateway_result.message}" if gateway_result else ""
    if task_status == "queued":
        return (
            "Action executed. All approvals are complete; task has been unblocked to queued."
            f"{gateway_message}"
        )
    return f"Action executed. No paused runtime needed to be unblocked.{gateway_message}"


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _gateway_result_blocks_resume(gateway_result: WorldGatewayResult) -> bool:
    return (
        gateway_result.requires_handler
        or gateway_result.gateway_mode == "policy_blocked"
        or gateway_result.capability_status in {"missing_handler", "preview_failed"}
    )


async def _record_world_action_credit(
    session: AsyncSession,
    *,
    tenant_id: str,
    task_ref: str,
    action_type: str,
    gateway_result: WorldGatewayResult,
    decision_ticket: DecisionTicket,
) -> None:
    resources, immediate_reward, outcome = _world_action_credit_inputs(
        action_type=action_type,
        gateway_result=gateway_result,
        decision_ticket=decision_ticket,
    )
    credit = CreditAssignment()
    credit.record_step(
        task_ref,
        0,
        resources,
        immediate_reward=immediate_reward,
        metadata={
            "source": "world_gateway",
            "gateway_mode": gateway_result.gateway_mode,
            "capability_status": gateway_result.capability_status,
            "external_dispatched": gateway_result.external_dispatched,
            "requires_handler": gateway_result.requires_handler,
            "decision_ticket_id": decision_ticket.ticket_id,
        },
    )
    report = await credit.finalize_task(task_ref, outcome)
    await persist_resource_credit_report(session, tenant_id=tenant_id, report=report)


def _world_action_credit_inputs(
    *,
    action_type: str,
    gateway_result: WorldGatewayResult,
    decision_ticket: DecisionTicket,
) -> tuple[dict[str, list[str]], float, str]:
    handler_id = _optional_str(gateway_result.audit.get("handler_id"))
    resources: dict[str, list[str]] = {
        "world_action": [action_type],
        "world_gateway_mode": [gateway_result.gateway_mode],
        "decision_ticket": [decision_ticket.ticket_id],
    }
    if handler_id:
        resources["world_handler"] = [handler_id]
    if _gateway_result_blocks_resume(gateway_result):
        return resources, 0.15, "fail"
    if gateway_result.external_dispatched:
        return resources, 0.9, "pass"
    return resources, 0.7, "partial"


async def _collect_handler_health_card(
    *,
    tenant_id: str,
    action_type: str,
) -> WorldHandlerHealthCard | None:
    try:
        cards = await collect_world_handler_health(tenant_id=tenant_id)
    except Exception as exc:
        if _action_type_needs_fail_closed(action_type):
            return _health_lookup_failed_card(action_type=action_type, exc=exc)
        return None
    return next((card for card in cards if card.action_type == action_type), None)


def _action_type_needs_fail_closed(action_type: str) -> bool:
    return action_type in {
        "email.send",
        "enterprise_api.post",
        "browser.execute",
    } or action_type.startswith(("payment.", "deployment.", "resource.delete"))


def _health_lookup_failed_card(*, action_type: str, exc: Exception) -> WorldHandlerHealthCard:
    return WorldHandlerHealthCard(
        action_type=action_type,
        handler_id="",
        status="blocked",
        mode="unknown",
        external_dispatched=True,
        registered=False,
        configured=False,
        requires_human_approval=True,
        has_compensation=False,
        static_risk="high",
        dynamic_risk="high",
        total_seen=0,
        approved_count=0,
        rejected_count=0,
        executed_count=0,
        failed_count=0,
        missing_handler_count=1,
        policy_blocked_count=1,
        success_rate=0.0,
        failure_rate=1.0,
        approval_reject_rate=0.0,
        compensation_strategy="",
        control_status="enabled",
        control_reason="",
        recommendation="暂停真实外发；先恢复 NUO handler health / control 读取，再重新审批。",
        issues=[
            "NUO handler health/control 读取失败，真实外发按 fail-closed 拦截",
            f"{type(exc).__name__}: {exc}",
        ],
    )


def _handler_health_blocks_execution(card: WorldHandlerHealthCard) -> bool:
    return (
        card.status in {"blocked", "unregistered"}
        or not card.configured
        or (card.external_dispatched and not card.has_compensation)
    )


def _handler_health_blocked_result(
    *,
    action_id: str,
    health_card: WorldHandlerHealthCard,
) -> WorldGatewayResult:
    issue_text = "；".join(health_card.issues[:3]) or health_card.recommendation
    return WorldGatewayResult(
        action_id=action_id,
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=True,
        audit={
            "policy": {
                "allowed": False,
                "source": "nuo.handler_health",
                "handler_status": health_card.status,
                "action_type": health_card.action_type,
                "failure_rate": health_card.failure_rate,
                "approval_reject_rate": health_card.approval_reject_rate,
            },
            "handler_health": health_card.model_dump(mode="json"),
        },
        user_summary=(
            f"NUO 判断 {health_card.action_type} 当前不适合执行，这个外部动作已被安全拦截。"
        ),
        next_step=health_card.recommendation,
        permissions_required=["world:dispatch"],
        message=f"World Gateway blocked by NUO handler health: {issue_text}",
    )


def _handler_health_unknown_blocked_result(
    *,
    action_id: str,
    action_type: str,
) -> WorldGatewayResult:
    reason = "handler health/control state is unknown for a fail-closed external action"
    return WorldGatewayResult(
        action_id=action_id,
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=True,
        audit={
            "policy": {
                "allowed": False,
                "source": "world_action_reliability_guard",
                "action_type": action_type,
                "reliability_guard": "handler_health_unknown",
            },
            "reliability_guard": {
                "status": "blocked",
                "reasons": [reason],
            },
        },
        user_summary=f"{action_type} 的 handler 健康状态未知，真实外发已被安全拦截。",
        next_step="先恢复 NUO handler health / control 读取，再重新审批真实外部动作。",
        permissions_required=["world:dispatch", "handler_health_known"],
        message=f"World Gateway reliability guard blocked execution: {reason}",
    )


def _idempotency_blocked_result(
    *,
    action: PendingActionRow,
    duplicate_execution: WorldActionExecutionRow,
    idempotency_key: str,
) -> WorldGatewayResult:
    reason = "duplicate idempotency key already has a claimed/executed world-action ledger row"
    return WorldGatewayResult(
        action_id=action.action_id,
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=False,
        audit={
            "policy": {
                "allowed": False,
                "source": "world_action_reliability_guard",
                "action_type": action.action_type,
                "reliability_guard": "duplicate_idempotency_key",
            },
            "reliability_guard": {
                "status": "blocked",
                "reasons": [reason],
                "idempotency_key": idempotency_key,
                "duplicate_action_id": duplicate_execution.action_id,
                "duplicate_status": duplicate_execution.status,
            },
            "external_dispatched": False,
            "requires_handler": False,
            "action_type": action.action_type,
            "risk_level": action.risk_level,
        },
        user_summary="这个外部动作和已有账本记录使用同一个幂等键，已阻止重复执行。",
        next_step="查看已有 action 的执行结果；如确需再次外发，生成新的明确幂等键并重新审批。",
        permissions_required=["world:dispatch", "unique_idempotency_key"],
        message=(
            "World Gateway reliability guard blocked duplicate idempotency key "
            f"{idempotency_key!r}; prior action={duplicate_execution.action_id}."
        ),
    )


def _missing_idempotency_blocked_result(
    *,
    action: PendingActionRow,
    idempotency_key: str,
) -> WorldGatewayResult:
    reason = "explicit idempotency_key is required before executing fail-closed external action"
    return WorldGatewayResult(
        action_id=action.action_id,
        gateway_mode="policy_blocked",
        capability_status="preview_failed",
        external_dispatched=False,
        requires_handler=False,
        audit={
            "policy": {
                "allowed": False,
                "source": "world_action_reliability_guard",
                "action_type": action.action_type,
                "reliability_guard": "missing_idempotency_key",
            },
            "reliability_guard": {
                "status": "blocked",
                "reasons": [reason],
                "fallback_idempotency_key": idempotency_key,
            },
            "external_dispatched": False,
            "requires_handler": False,
            "action_type": action.action_type,
            "risk_level": action.risk_level,
        },
        user_summary="真实外部动作缺少显式幂等键，已阻止执行以避免重复副作用。",
        next_step="为该外部动作生成稳定的 idempotency_key，并重新审批。",
        permissions_required=["world:dispatch", "explicit_idempotency_key"],
        message=f"World Gateway reliability guard blocked missing idempotency key for {action.action_type}.",
    )


def _world_action_guard_problem_summary(gateway_result: WorldGatewayResult) -> str:
    guard = gateway_result.audit.get("reliability_guard")
    guard_name = ""
    if isinstance(guard, dict):
        guard_name = str(guard.get("status") or "")
    policy = _dict_or_empty(gateway_result.audit.get("policy"))
    reliability_guard = str(policy.get("reliability_guard") or guard_name)
    if reliability_guard == "missing_idempotency_key":
        return "WorldGateway reliability guard blocked external action without explicit idempotency key"
    if reliability_guard == "duplicate_idempotency_key":
        return "WorldGateway idempotency guard blocked duplicate external action"
    return "WorldGateway reliability guard blocked approved action"


async def _enqueue_world_action_problem(
    *,
    tenant_id: str,
    task_ref: str,
    action_id: str,
    action_type: str,
    severity: str,
    summary: str,
    gateway_result: WorldGatewayResult | None = None,
    error: str = "",
) -> None:
    """Feed real world-action failures into Qi's problem queue.

    This is deliberately best-effort: if the learning queue is unavailable, the
    user-facing block/failure must still be recorded and returned.
    """

    with contextlib.suppress(Exception):
        from kun.qi.problem_queue import QiProblemSignal, persist_problem_signals

        evidence: dict[str, Any] = {
            "task_id": task_ref,
            "action_id": action_id,
            "action_type": action_type,
        }
        if gateway_result is not None:
            evidence["gateway"] = gateway_result.model_dump(mode="json")
        if error:
            evidence["error"] = error
        await persist_problem_signals(
            [
                QiProblemSignal.build(
                    tenant_id=tenant_id,
                    category="world_gateway",
                    severity=severity,
                    summary=summary,
                    source="action_executor",
                    task_type="world_gateway.action",
                    evidence=evidence,
                )
            ]
        )


__all__ = ["ActionExecutionResult", "execute_approved_action_once"]
