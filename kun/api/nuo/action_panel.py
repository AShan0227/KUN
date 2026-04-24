"""傩 · 待审批动作面板."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from kun.core.db import session_scope
from kun.core.orm import PendingActionRow
from kun.core.tenancy import current_tenant

router = APIRouter()

ActionStatus = Literal["pending_approval", "approved", "rejected", "executed", "cancelled"]
ActionDecision = Literal["approve", "reject", "cancel"]


class PendingActionItem(BaseModel):
    action_id: str
    task_ref: str
    action_type: str
    target_ref: str
    status: ActionStatus
    risk_level: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class PendingActionList(BaseModel):
    tenant_id: str
    actions: list[PendingActionItem]


class ActionDecisionRequest(BaseModel):
    decision: ActionDecision
    reason: str | None = None


class ActionDecisionResponse(BaseModel):
    action_id: str
    status: ActionStatus


@router.get("/pending", response_model=PendingActionList)
async def list_pending_actions(
    status: ActionStatus = Query(default="pending_approval"),
    limit: int = Query(default=50, ge=1, le=200),
) -> PendingActionList:
    """List tenant-scoped side-effect actions waiting in NUO."""
    tenant = current_tenant()
    async with session_scope() as s:
        result = await s.execute(
            select(PendingActionRow)
            .where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.status == status,
            )
            .order_by(PendingActionRow.created_at)
            .limit(limit)
        )
        rows = list(result.scalars().all())

    return PendingActionList(
        tenant_id=tenant.tenant_id,
        actions=[_row_to_item(row) for row in rows],
    )


@router.post("/{action_id}/decision", response_model=ActionDecisionResponse)
async def decide_pending_action(
    action_id: str,
    req: ActionDecisionRequest,
) -> ActionDecisionResponse:
    """Approve/reject/cancel a pending side-effect action.

    This endpoint only changes the queue state. A future executor will pick up
    approved actions and perform the external side effect behind its own guard.
    """
    tenant = current_tenant()
    new_status = _decision_to_status(req.decision)
    now = datetime.now(UTC)

    async with session_scope() as s:
        existing = await s.execute(
            select(PendingActionRow.status).where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.action_id == action_id,
            )
        )
        old_status = existing.scalar_one_or_none()
        if old_status is None:
            raise HTTPException(status_code=404, detail="pending action not found")
        if old_status != "pending_approval":
            raise HTTPException(
                status_code=409,
                detail=f"pending action already decided: {old_status}",
            )

        payload_update = {"decision_reason": req.reason} if req.reason else {}
        values: dict[str, Any] = {
            "status": new_status,
            "updated_at": now,
            "decided_at": now,
        }
        if payload_update:
            row = await s.execute(
                select(PendingActionRow.payload).where(
                    PendingActionRow.tenant_id == tenant.tenant_id,
                    PendingActionRow.action_id == action_id,
                )
            )
            payload = dict(row.scalar_one_or_none() or {})
            payload.update(payload_update)
            values["payload"] = payload

        await s.execute(
            update(PendingActionRow)
            .where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.action_id == action_id,
            )
            .values(**values)
        )

    return ActionDecisionResponse(action_id=action_id, status=new_status)


def _decision_to_status(decision: ActionDecision) -> ActionStatus:
    if decision == "approve":
        return "approved"
    if decision == "reject":
        return "rejected"
    return "cancelled"


def _row_to_item(row: PendingActionRow) -> PendingActionItem:
    return PendingActionItem(
        action_id=row.action_id,
        task_ref=row.task_ref,
        action_type=row.action_type,
        target_ref=row.target_ref,
        status=cast(ActionStatus, row.status),
        risk_level=row.risk_level,
        payload=row.payload,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
