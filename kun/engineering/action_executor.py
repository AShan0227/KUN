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
from kun.datamodel.events import Event
from kun.datamodel.runtime import TaskStatus


class ActionExecutionResult(BaseModel):
    """Outcome from one executor pass."""

    action_id: str
    task_ref: str
    action_status: str
    task_status: TaskStatus | None = None
    message: str


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
        action.payload = _executor_payload(action.payload, now)
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
                    "executor_mode": "approval_gate",
                },
                task_ref=task_ref,
            ),
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

        return ActionExecutionResult(
            action_id=action_id,
            task_ref=task_ref,
            action_status="executed",
            task_status=task_status,
            message=_execution_message(task_status),
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


def _executor_payload(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    merged = dict(payload)
    merged["executor"] = {
        "mode": "approval_gate",
        "status": "executed",
        "executed_at": now.isoformat(),
        "note": "External side-effect adapters are not attached yet; this releases the approval gate.",
    }
    return merged


def _execution_message(task_status: TaskStatus | None) -> str:
    if task_status == "queued":
        return "Action executed. All approvals are complete; task has been unblocked to queued."
    return "Action executed. No paused runtime needed to be unblocked."


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["ActionExecutionResult", "execute_approved_action_once"]
