"""Pending side-effect action executor.

This is the first safe execution loop for NUO pending actions. It does not
perform arbitrary external side effects yet; it executes the approval gate:

1. claim one approved action under row lock,
2. mark it executed with audit metadata,
3. when all task actions are resolved, unblock the paused task back to queued.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.orm import PendingActionRow, RuntimeStateRow, TaskResultRow
from kun.core.state_ledger import get_state_ledger
from kun.datamodel.decision_ticket import ticket_from_world_policy
from kun.datamodel.events import Event
from kun.datamodel.runtime import TaskStatus
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
        health_card = await _collect_handler_health_card(
            tenant_id=tenant_id,
            action_type=action.action_type,
        )
        if health_card is not None and _handler_health_blocks_execution(health_card):
            gateway_result = _handler_health_blocked_result(
                action_id=action.action_id,
                health_card=health_card,
            )
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
                    "status": health_card.status,
                    "issues": health_card.issues,
                },
                reason=gateway_result.message,
            )
            action.payload = _executor_blocked_payload(
                action.payload,
                now,
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
            get_state_ledger().record_paused(
                task_ref,
                reason=_execution_blocked_message(gateway_result),
                pending_confirmations=[action.action_id],
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
                    task_ref=task_ref,
                    action_type=action.action_type,
                    target_ref=action.target_ref,
                    risk_level=action.risk_level,
                    payload=action.payload,
                )
            )
        except Exception as exc:
            action.payload = _executor_error_payload(action.payload, now, exc)
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
            get_state_ledger().record_paused(
                task_ref,
                reason=_execution_blocked_message(gateway_result),
                pending_confirmations=[action.action_id],
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

        unblocked = await s.execute(_unblock_paused_runtime_stmt(tenant_id, task_ref, now))
        task_status: TaskStatus | None = "queued" if _rowcount(unblocked) > 0 else None
        if task_status == "queued":
            await s.execute(_mark_task_result_queued_stmt(tenant_id, task_ref, now))
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


def _unblock_paused_runtime_stmt(tenant_id: str, task_ref: str, now: datetime) -> Any:
    return (
        update(RuntimeStateRow)
        .where(
            RuntimeStateRow.tenant_id == tenant_id,
            RuntimeStateRow.task_ref == task_ref,
            RuntimeStateRow.status == "paused",
        )
        .values(status="queued", finished_at=None, last_updated=now)
    )


def _mark_task_result_queued_stmt(tenant_id: str, task_ref: str, now: datetime) -> Any:
    answer = "审批已通过，任务已解除阻塞，等待恢复执行。"
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
                literal({"status": "queued", "answer": answer}, type_=JSONB)
            ),
        )
    )


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


async def _collect_handler_health_card(
    *,
    tenant_id: str,
    action_type: str,
) -> WorldHandlerHealthCard | None:
    try:
        cards = await collect_world_handler_health(tenant_id=tenant_id)
    except Exception:
        return None
    return next((card for card in cards if card.action_type == action_type), None)


def _handler_health_blocks_execution(card: WorldHandlerHealthCard) -> bool:
    return card.status in {"blocked", "unregistered"}


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


__all__ = ["ActionExecutionResult", "execute_approved_action_once"]
