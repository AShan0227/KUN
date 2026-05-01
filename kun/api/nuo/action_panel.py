"""傩 · 待审批动作面板."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from kun.api.runtime import get_orchestrator, run_background_job_via_lane
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.orm import PendingActionRow
from kun.core.tenancy import current_tenant, require_scope
from kun.datamodel.runtime import TaskStatus
from kun.engineering.action_executor import ActionExecutionResult, execute_approved_action_once
from kun.engineering.pending_task_resume import resume_unblocked_task_once
from kun.world.action_reliability import (
    WorldActionReliabilityItem,
    collect_world_action_reliability,
)
from kun.world.gateway import (
    WorldAction,
    WorldGateway,
    WorldGatewayResult,
    WorldHandlerDescriptor,
    get_world_gateway,
)
from kun.world.handler_auto_control import (
    WorldHandlerAutoControlReport,
    run_world_handler_auto_quarantine,
)
from kun.world.handler_control import WorldHandlerControl, set_world_handler_control
from kun.world.handler_health import (
    WorldHandlerHealthCard,
    collect_world_handler_health,
    summarize_handler_health,
)

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
    gateway_preview: dict[str, Any] | None = None
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
    external_dispatch_confirmed: bool = False


class ActionDecisionResponse(BaseModel):
    action_id: str
    status: ActionStatus
    message: str = ""
    task_ref: str | None = None
    task_status: TaskStatus | None = None
    gateway: dict[str, Any] | None = None


class WorldGatewayHandlersResponse(BaseModel):
    tenant_id: str
    artifact_root: str
    handlers: list[WorldHandlerDescriptor]
    unsupported_policy: str = (
        "没有 handler 的 action 只会生成审计包，并明确 requires_handler=true；"
        "不会假装已经真实外发。"
    )


class WorldGatewayHandlerHealthResponse(BaseModel):
    tenant_id: str
    summary: dict[str, int]
    handlers: list[WorldHandlerHealthCard]


class WorldActionReliabilityResponse(BaseModel):
    tenant_id: str
    summary: dict[str, int]
    items: list[WorldActionReliabilityItem]


class HandlerControlRequest(BaseModel):
    status: Literal["enabled", "quarantined", "disabled"]
    reason: str = ""


class HandlerControlResponse(BaseModel):
    tenant_id: str
    control: WorldHandlerControl
    message: str


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

    gateway = get_world_gateway()
    items: list[PendingActionItem] = []
    for row in page:
        preview = await _preview_for_row(row, gateway)
        items.append(_row_to_item(row, preview=preview))

    return PendingActionList(
        tenant_id=tenant.tenant_id,
        actions=items,
        next_cursor=next_cursor,
        has_more=remaining > 0 and round_no < max_rounds,
        remaining=remaining if round_no < max_rounds else 0,
        round=round_no,
        max_rounds=max_rounds,
    )


@router.get("/handlers", response_model=WorldGatewayHandlersResponse)
async def list_world_gateway_handlers() -> WorldGatewayHandlersResponse:
    """Show which approved world actions can produce real controlled artifacts."""
    tenant = current_tenant()
    gateway = get_world_gateway()
    return WorldGatewayHandlersResponse(
        tenant_id=tenant.tenant_id,
        artifact_root=str(gateway.artifact_root),
        handlers=gateway.handler_descriptors(),
    )


@router.get("/handler-health", response_model=WorldGatewayHandlerHealthResponse)
async def list_world_gateway_handler_health() -> WorldGatewayHandlerHealthResponse:
    """Show which external handlers are reliable, risky, or misconfigured."""
    tenant = current_tenant()
    cards = await collect_world_handler_health(tenant_id=tenant.tenant_id)
    return WorldGatewayHandlerHealthResponse(
        tenant_id=tenant.tenant_id,
        summary=summarize_handler_health(cards),
        handlers=cards,
    )


@router.get("/execution-reliability", response_model=WorldActionReliabilityResponse)
async def list_world_action_execution_reliability(
    limit: int = Query(default=20, ge=1, le=200),
) -> WorldActionReliabilityResponse:
    """Show retry/compensation risks from the durable world-action ledger."""

    tenant = current_tenant()
    report = await collect_world_action_reliability(tenant_id=tenant.tenant_id, limit=limit)
    return WorldActionReliabilityResponse(
        tenant_id=report.tenant_id,
        summary=report.summary,
        items=report.items,
    )


@router.post("/handler-control/auto-quarantine/run", response_model=WorldHandlerAutoControlReport)
async def run_world_gateway_handler_auto_quarantine(
    dry_run: bool = Query(default=True),
    min_seen: int = Query(default=3, ge=1, le=100),
    failure_threshold: float = Query(default=0.25, ge=0.0, le=1.0),
) -> WorldHandlerAutoControlReport:
    """Ask NUO to recommend/apply persistent handler quarantine."""

    tenant = current_tenant()
    if not dry_run:
        _require_scope_when_enforced("world:dispatch")
    return await run_world_handler_auto_quarantine(
        tenant_id=tenant.tenant_id,
        dry_run=dry_run,
        min_seen=min_seen,
        failure_threshold=failure_threshold,
    )


@router.post("/handler-control/{action_type}", response_model=HandlerControlResponse)
async def set_world_gateway_handler_control(
    action_type: str,
    req: HandlerControlRequest,
) -> HandlerControlResponse:
    """Persistently enable/quarantine/disable a WorldGateway action type."""

    tenant = current_tenant()
    _require_scope_when_enforced("world:dispatch")
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        control = await set_world_handler_control(
            s,
            tenant_id=tenant.tenant_id,
            action_type=action_type,
            status=req.status,
            reason=req.reason,
            source="nuo.api",
            updated_by=tenant.user_id,
        )
    if req.status == "enabled":
        message = "handler 已恢复；后续仍会继续接受健康检查和审批门控。"
    else:
        message = "handler 已持久化隔离；恢复前真实外发会被执行器拦截。"
    return HandlerControlResponse(tenant_id=tenant.tenant_id, control=control, message=message)


@router.get("/recent", response_model=PendingActionList)
async def list_recent_actions(
    limit: int = Query(default=5, ge=1, le=50),
) -> PendingActionList:
    """List recently decided/executed actions for human audit."""
    tenant = current_tenant()
    async with session_scope() as s:
        result = await s.execute(
            select(PendingActionRow)
            .where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.status.in_(("executed", "cancelled", "rejected")),
            )
            .order_by(PendingActionRow.updated_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())

    return PendingActionList(
        tenant_id=tenant.tenant_id,
        actions=[_row_to_item(row) for row in rows],
        has_more=False,
        remaining=0,
    )


@router.post("/{action_id}/decision", response_model=ActionDecisionResponse)
async def decide_pending_action(
    action_id: str,
    req: ActionDecisionRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> ActionDecisionResponse:
    """Approve/reject/cancel a pending side-effect action.

    Approve will immediately call the guarded pending-action executor when
    possible. Supported low-risk WorldGateway handlers may produce artifacts;
    unsupported action types stay honest through requires_handler metadata.
    """
    tenant = current_tenant()
    if req.decision == "approve":
        _require_scope_when_enforced("world:approve")
    if req.external_dispatch_confirmed:
        _require_scope_when_enforced("world:dispatch")
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
                external_dispatch_confirmed=req.external_dispatch_confirmed,
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
        execution = cast(
            ActionExecutionResult | None,
            await run_background_job_via_lane(
                request.app,
                name=f"world_action_execute:{tenant.tenant_id}:{action_id}",
                lane="world",
                callback=lambda: execute_approved_action_once(
                    tenant_id=tenant.tenant_id,
                    action_id=action_id,
                ),
                tenant_id=tenant.tenant_id,
                user_id=tenant.user_id,
            ),
        )
        if execution is not None and execution.task_status == "queued":
            background_tasks.add_task(
                resume_unblocked_task_once,
                tenant_id=tenant.tenant_id,
                task_id=execution.task_ref,
                orchestrator=get_orchestrator(request.app),
            )

    return ActionDecisionResponse(
        action_id=action_id,
        status=cast(ActionStatus, execution.action_status if execution else new_status),
        message=_decision_response_message(execution, new_status),
        task_ref=execution.task_ref if execution else None,
        task_status=execution.task_status if execution else None,
        gateway=(
            execution.gateway_result.model_dump(mode="json")
            if execution and execution.gateway_result
            else None
        ),
    )


def _decision_update_stmt(
    *,
    tenant_id: str,
    action_id: str,
    new_status: ActionStatus,
    now: datetime,
    reason: str | None = None,
    external_dispatch_confirmed: bool = False,
) -> Any:
    values: dict[str, Any] = {
        "status": new_status,
        "updated_at": now,
        "decided_at": now,
    }
    payload_patch: dict[str, Any] = {}
    if reason:
        payload_patch["decision_reason"] = reason
    if new_status == "approved" and external_dispatch_confirmed:
        payload_patch["external_dispatch_confirmed"] = True
    if payload_patch:
        values["payload"] = PendingActionRow.payload.op("||")(literal(payload_patch, type_=JSONB))

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


def _decision_response_message(
    execution: ActionExecutionResult | None,
    new_status: ActionStatus,
) -> str:
    if execution is None:
        return _decision_message(new_status)
    if execution.task_status == "queued":
        return f"{execution.message} Continuation resume has been scheduled in the background."
    return execution.message


def _decision_to_status(decision: ActionDecision) -> ActionStatus:
    if decision == "approve":
        return "approved"
    if decision == "reject":
        return "rejected"
    return "cancelled"


async def _preview_for_row(
    row: PendingActionRow,
    gateway: WorldGateway,
) -> WorldGatewayResult:
    try:
        return await gateway.preview(
            WorldAction(
                action_id=row.action_id,
                tenant_id=row.tenant_id,
                task_ref=row.task_ref,
                action_type=row.action_type,
                target_ref=row.target_ref,
                risk_level=row.risk_level,
                payload=row.payload,
            )
        )
    except Exception as exc:
        return WorldGatewayResult(
            action_id=row.action_id,
            gateway_mode="preview_failed",
            capability_status="preview_failed",
            requires_handler=False,
            audit={"error": str(exc), "action_type": row.action_type},
            user_summary="这个动作预览失败，批准前需要人工检查。",
            next_step="先修正动作参数或补齐 handler，再重新预览。",
            message=f"World Gateway preview failed: {exc}",
        )


def _row_to_item(
    row: PendingActionRow,
    *,
    preview: WorldGatewayResult | None = None,
) -> PendingActionItem:
    return PendingActionItem(
        action_id=row.action_id,
        task_ref=row.task_ref,
        action_type=row.action_type,
        target_ref=row.target_ref,
        status=cast(ActionStatus, row.status),
        risk_level=row.risk_level,
        payload=_redact_payload(row.payload),
        gateway_preview=preview.model_dump(mode="json") if preview else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


_SECRET_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(fragment in key.lower() for fragment in _SECRET_KEY_FRAGMENTS):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


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
