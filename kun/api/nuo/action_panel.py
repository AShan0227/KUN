"""傩 · 待审批动作面板."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from kun.core.db import session_scope
from kun.core.orm import PendingActionRow
from kun.core.tenancy import current_tenant
from kun.datamodel.runtime import TaskStatus
from kun.engineering.action_executor import ActionExecutionResult, execute_approved_action_once

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
    next_cursor: str | None = None
    has_more: bool = False
    remaining: int = 0
    round: int = 1
    max_rounds: int = 3


class ActionDecisionRequest(BaseModel):
    decision: ActionDecision
    reason: str | None = None


class ActionDecisionResponse(BaseModel):
    action_id: str
    status: ActionStatus
    message: str = ""
    task_ref: str | None = None
    task_status: TaskStatus | None = None


@router.get("/pending", response_model=PendingActionList)
async def list_pending_actions(
    status: ActionStatus = Query(default="pending_approval"),
    limit: int = Query(default=3, ge=1, le=50),
    expand_after: str | None = Query(default=None),
    max_rounds: int = Query(default=3, ge=1, le=3),
) -> PendingActionList:
    """List tenant-scoped side-effect actions waiting in NUO.

    Anchor-expand UX:
    - 首屏默认只返最高风险的 3 条;
    - expand_after=<上一页最后一条 action_id> 时返后续 3 条;
    - max_rounds 最多 3 轮, 防止用户一次拉太多注意力噪声.
    """
    tenant = current_tenant()
    async with session_scope() as s:
        result = await s.execute(
            select(PendingActionRow)
            .where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.status == status,
            )
            .order_by(PendingActionRow.created_at)
            .limit(200)
        )
        rows = list(result.scalars().all())

    rows = _sort_actions_for_anchor(rows)
    try:
        page, next_cursor, remaining, round_no = _page_actions_anchor(
            rows,
            limit=limit,
            expand_after=expand_after,
            max_rounds=max_rounds,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    return PendingActionList(
        tenant_id=tenant.tenant_id,
        actions=[_row_to_item(row) for row in page],
        next_cursor=next_cursor,
        has_more=remaining > 0 and round_no < max_rounds,
        remaining=remaining if round_no < max_rounds else 0,
        round=round_no,
        max_rounds=max_rounds,
    )


@router.post("/{action_id}/decision", response_model=ActionDecisionResponse)
async def decide_pending_action(
    action_id: str,
    req: ActionDecisionRequest,
) -> ActionDecisionResponse:
    """Approve/reject/cancel a pending side-effect action.

    Approve will immediately call the guarded pending-action executor when
    possible. Supported low-risk WorldGateway handlers may produce artifacts;
    unsupported action types stay honest through requires_handler metadata.
    """
    tenant = current_tenant()
    new_status = _decision_to_status(req.decision)
    now = datetime.now(UTC)

    async with session_scope() as s:
        result = await s.execute(
            _decision_update_stmt(
                tenant_id=tenant.tenant_id,
                action_id=action_id,
                new_status=new_status,
                now=now,
                reason=req.reason,
            )
        )
        updated = result.one_or_none()
        if updated is None:
            existing = await s.execute(
                select(PendingActionRow.status).where(
                    PendingActionRow.tenant_id == tenant.tenant_id,
                    PendingActionRow.action_id == action_id,
                )
            )
            old_status = existing.scalar_one_or_none()
            if old_status is None:
                raise HTTPException(status_code=404, detail="pending action not found")
            raise HTTPException(
                status_code=409,
                detail=f"pending action already decided: {old_status}",
            )

    execution: ActionExecutionResult | None = None
    if new_status == "approved":
        execution = await execute_approved_action_once(
            tenant_id=tenant.tenant_id,
            action_id=action_id,
        )

    return ActionDecisionResponse(
        action_id=action_id,
        status=cast(ActionStatus, execution.action_status if execution else new_status),
        message=execution.message if execution else _decision_message(new_status),
        task_ref=execution.task_ref if execution else None,
        task_status=execution.task_status if execution else None,
    )


def _decision_update_stmt(
    *,
    tenant_id: str,
    action_id: str,
    new_status: ActionStatus,
    now: datetime,
    reason: str | None = None,
) -> Any:
    values: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
        "decided_at": now,
    }
    if reason:
        values["payload"] = PendingActionRow.payload.op("||")(
            literal({"decision_reason": reason}, type_=JSONB)
        )

    return (
        update(PendingActionRow)
        .where(
            PendingActionRow.tenant_id == tenant_id,
            PendingActionRow.action_id == action_id,
            PendingActionRow.status == "pending_approval",
        )
        .values(**values)
        .returning(PendingActionRow.action_id, PendingActionRow.status)
    )


def _decision_message(status: ActionStatus) -> str:
    if status == "approved":
        return "Action approved. KUN will execute the guarded approval gate when possible."
    return f"Action marked {status}."


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


_RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _sort_actions_for_anchor(rows: list[PendingActionRow]) -> list[PendingActionRow]:
    """最高风险优先, 同风险按创建时间稳定排序."""
    return sorted(
        rows,
        key=lambda row: (
            _RISK_ORDER.get(row.risk_level, 99),
            row.created_at,
            row.action_id,
        ),
    )


def _page_actions_anchor(
    rows: list[PendingActionRow],
    *,
    limit: int,
    expand_after: str | None,
    max_rounds: int,
) -> tuple[list[PendingActionRow], str | None, int, int]:
    """返回一页 action + 下一页 cursor + 剩余数量 + 当前轮次."""
    if expand_after is None:
        start = 0
    else:
        idx = next((i for i, row in enumerate(rows) if row.action_id == expand_after), None)
        if idx is None:
            raise ValueError("expand_after action not found")
        start = idx + 1

    round_no = min(max(start // limit + 1, 1), max_rounds)
    if round_no > max_rounds:
        return ([], None, 0, round_no)

    page = rows[start : start + limit]
    next_cursor = page[-1].action_id if page else None
    remaining = max(0, len(rows) - (start + len(page)))
    return (page, next_cursor, remaining, round_no)
