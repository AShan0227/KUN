"""Task Control API — Kill Switch / 状态 / 超时查询 (V2.1 wire).

REST 接口让 NUO 面板 / 移动端 / 跨 session 都能控制任务.
SLA: kill ≤500ms 收到 SIGSTOP (§5.2.3 / T55).

POST /api/tasks/{task_id}/kill           发 kill 信号
GET  /api/tasks/{task_id}/status         查 KillSwitch + TaskTimeoutGuard 状态
POST /api/tasks/{task_id}/register       注册 task (orchestrator 内部用, 也对外暴露)
GET  /api/tasks/active                   列出所有未完成 task (基础版)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from kun.api.runtime import get_kill_switch, get_task_timeout
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.orm import TaskRow
from kun.core.state_ledger import get_state_ledger
from kun.core.tenancy import current_tenant, require_scope
from kun.datamodel.events import Event

log = get_logger("kun.api.task_control")

router = APIRouter(prefix="/api/tasks", tags=["task-control"])


class KillRequest(BaseModel):
    reason: str = "user_interrupt"


class KillResponse(BaseModel):
    task_id: str
    killed: bool
    reason: str
    requested_at: str
    registered: bool = True


class TaskStatusResponse(BaseModel):
    task_id: str
    registered: bool = False
    is_killed: bool
    kill_reason: str | None = None
    is_timed_out: bool = False
    timeout_reason: str = ""
    timeout_action: str = ""


class RegisterRequest(BaseModel):
    max_duration_sec: int | None = None
    max_steps: int | None = None
    timeout_action: str = "pause_ask_user"


class TaskMetadataUpdateRequest(BaseModel):
    risk_level: Literal["low", "medium", "high", "critical"] | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    success_criteria_short: str | None = Field(default=None, min_length=1, max_length=200)
    constraint_note: str | None = Field(default=None, max_length=500)
    confirmation_policy: (
        Literal[
            "normal",
            "ask_before_external",
            "always_ask",
        ]
        | None
    ) = None


class TaskMetadataUpdateResponse(BaseModel):
    task_id: str
    updated: bool
    changed_fields: list[str]
    message: str


@router.post("/{task_id}/kill", response_model=KillResponse)
async def kill_task(
    task_id: str,
    body: KillRequest,
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> KillResponse:
    """V2.1 §5.2.3 / T55: 发 kill 信号. SLA ≤500ms."""
    ks = get_kill_switch(request.app)
    killed = ks.kill(task_id, reason=body.reason)
    if not killed:
        raise HTTPException(
            404,
            (
                f"task {task_id} is not registered in this API process. "
                "It may already be finished, paused/queued, or running in another worker."
            ),
        )
    try:
        tenant = current_tenant()
        requested_by = x_user_id or tenant.user_id or tenant.tenant_id
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.cancelled",
                    payload={
                        "task_id": task_id,
                        "status": "requested",
                        "reason": body.reason,
                        "requested_by": requested_by,
                    },
                    task_ref=task_id,
                ),
            )
    except Exception:
        log.warning("task_control.kill_event_emit_failed", task_id=task_id, exc_info=True)
    return KillResponse(
        task_id=task_id,
        killed=True,
        reason=body.reason,
        requested_at=datetime.now(UTC).isoformat(),
        registered=True,
    )


@router.get("/{task_id}/status", response_model=TaskStatusResponse)
def get_task_status(
    task_id: str,
    request: Request,
) -> TaskStatusResponse:
    """查 KillSwitch + TaskTimeoutGuard 状态."""
    ks = get_kill_switch(request.app)
    tt = get_task_timeout(request.app)

    is_killed = ks.is_killed(task_id)
    sig = ks.get_kill_signal(task_id)
    is_to, to_reason = tt.check(task_id)

    return TaskStatusResponse(
        task_id=task_id,
        registered=ks.is_registered(task_id),
        is_killed=is_killed,
        kill_reason=sig.reason if sig else None,
        is_timed_out=is_to,
        timeout_reason=to_reason,
        timeout_action=tt.get_action(task_id) if is_to else "",
    )


@router.post("/{task_id}/register")
def register_task(
    task_id: str,
    body: RegisterRequest,
    request: Request,
) -> dict[str, Any]:
    """注册 task 到 KillSwitch + TaskTimeoutGuard.

    orchestrator stream 应在任务开始时调用. 也对外暴露给手动控制.
    """
    ks = get_kill_switch(request.app)
    tt = get_task_timeout(request.app)
    ks.register_task(task_id)
    rt = tt.start(
        task_id,
        max_duration_sec=body.max_duration_sec,
        max_steps=body.max_steps,
        timeout_action=body.timeout_action,  # type: ignore[arg-type]
    )
    return {
        "task_id": task_id,
        "registered": True,
        "max_duration_sec": rt.max_duration_sec,
        "max_steps": rt.max_steps,
        "started_at": rt.started_at.isoformat(),
    }


@router.patch("/{task_id}/metadata", response_model=TaskMetadataUpdateResponse)
async def update_task_metadata(
    task_id: str,
    body: TaskMetadataUpdateRequest,
) -> TaskMetadataUpdateResponse:
    """Lightweight human edit for an existing TASK.md record.

    This edits the durable task metadata and records an event/StateLedger trail.
    It does not mutate an already-running in-memory LLM call; future resume,
    dashboards, guards, and reviews can consume the updated metadata.
    """

    _require_scope_when_enforced("task:write")
    tenant = current_tenant()
    cleaned_task_id = task_id.strip()
    if not cleaned_task_id:
        raise HTTPException(status_code=400, detail="task_id is required")

    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="no metadata fields supplied")

    async with session_scope(tenant_id=tenant.tenant_id) as s:
        result = await s.execute(
            select(TaskRow).where(
                TaskRow.tenant_id == tenant.tenant_id,
                TaskRow.task_id == cleaned_task_id,
            )
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        changed: dict[str, Any] = {}
        if body.risk_level is not None and task.risk_level != body.risk_level:
            task.risk_level = body.risk_level
            changed["risk_level"] = body.risk_level
        if body.estimated_cost_usd is not None and float(task.estimated_cost_usd) != float(
            body.estimated_cost_usd
        ):
            task.estimated_cost_usd = float(body.estimated_cost_usd)
            changed["estimated_cost_usd"] = float(body.estimated_cost_usd)
        if (
            body.success_criteria_short is not None
            and task.success_criteria_short != body.success_criteria_short
        ):
            task.success_criteria_short = body.success_criteria_short
            changed["success_criteria_short"] = body.success_criteria_short

        spec_json = dict(task.spec_json or {})
        if body.constraint_note:
            constraints = list(spec_json.get("constraints") or [])
            constraints.append({"kind": "custom", "detail": body.constraint_note})
            spec_json["constraints"] = constraints[-20:]
            changed["constraint_note"] = body.constraint_note
        if body.confirmation_policy:
            controls = dict(spec_json.get("user_controls") or {})
            controls["confirmation_policy"] = body.confirmation_policy
            spec_json["user_controls"] = controls
            changed["confirmation_policy"] = body.confirmation_policy
        if "constraint_note" in changed or "confirmation_policy" in changed:
            task.spec_json = spec_json

        if not changed:
            return TaskMetadataUpdateResponse(
                task_id=cleaned_task_id,
                updated=False,
                changed_fields=[],
                message="没有变化；任务控制参数保持原样。",
            )

        await emit(
            s,
            Event.build(
                tenant_id=tenant.tenant_id,
                event_type="task.metadata_updated",
                payload={
                    "task_id": cleaned_task_id,
                    "changed": _redact_metadata_patch(changed),
                    "updated_by": tenant.user_id or tenant.tenant_id,
                },
                task_ref=cleaned_task_id,
            ),
        )

    get_state_ledger().record_task_metadata_updated(
        cleaned_task_id,
        tenant_id=tenant.tenant_id,
        risk_level=body.risk_level,
        estimated_cost_usd=body.estimated_cost_usd,
        success_criteria_short=body.success_criteria_short,
        constraint_note=body.constraint_note,
        confirmation_policy=body.confirmation_policy,
    )
    return TaskMetadataUpdateResponse(
        task_id=cleaned_task_id,
        updated=True,
        changed_fields=sorted(changed),
        message="任务控制参数已写入账本；正在运行的单次 LLM 调用不会被强行热改，后续续跑和复盘会看到。",
    )


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    from kun.core.config import settings

    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _redact_metadata_patch(patch: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("[redacted]" if "token" in key.lower() else value) for key, value in patch.items()
    }


__all__ = ["router"]
