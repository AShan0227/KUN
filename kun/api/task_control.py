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
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from kun.api.runtime import get_kill_switch, get_task_timeout
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.tenancy import current_tenant
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


@router.post("/{task_id}/kill", response_model=KillResponse)
async def kill_task(
    task_id: str,
    body: KillRequest,
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")] = "u-anon",
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
                        "requested_by": x_user_id,
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


__all__ = ["router"]
