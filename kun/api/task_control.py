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
from pydantic import BaseModel
from sqlalchemy import select

from kun.api.runtime import get_kill_switch, get_task_timeout
from kun.core.db import session_scope
from kun.core.orm import RuntimeStateRow, TaskRow

router = APIRouter(prefix="/api/tasks", tags=["task-control"])


class KillRequest(BaseModel):
    reason: str = "user_interrupt"


class KillResponse(BaseModel):
    task_id: str
    killed: bool
    reason: str
    requested_at: str


class TaskStatusResponse(BaseModel):
    task_id: str
    is_killed: bool
    kill_reason: str | None = None
    is_timed_out: bool = False
    timeout_reason: str = ""
    timeout_action: str = ""


class RegisterRequest(BaseModel):
    max_duration_sec: int | None = None
    max_steps: int | None = None
    timeout_action: str = "pause_ask_user"


class TaskFlowStepResponse(BaseModel):
    step_id: str
    title: str
    status: Literal["pending", "running", "done", "failed", "skipped"]
    deps: list[str] = []
    input: str = ""
    output: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0


class TaskFlowResponse(BaseModel):
    task_id: str
    title: str
    status: str
    steps: list[TaskFlowStepResponse]


class StepControlRequest(BaseModel):
    reason: str = "user_flow_control"
    action: Literal["skip", "force_done"] = "skip"


class StepControlResponse(BaseModel):
    task_id: str
    step_id: str
    action: Literal["skip", "force_done"]
    accepted: bool
    message: str


@router.post("/{task_id}/kill", response_model=KillResponse)
def kill_task(
    task_id: str,
    body: KillRequest,
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")] = "u-anon",
) -> KillResponse:
    """V2.1 §5.2.3 / T55: 发 kill 信号. SLA ≤500ms."""
    ks = get_kill_switch(request.app)
    killed = ks.kill(task_id, reason=body.reason)
    if not killed:
        raise HTTPException(404, f"task {task_id} not registered or already done")
    return KillResponse(
        task_id=task_id,
        killed=True,
        reason=body.reason,
        requested_at=datetime.now(UTC).isoformat(),
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
        is_killed=is_killed,
        kill_reason=sig.reason if sig else None,
        is_timed_out=is_to,
        timeout_reason=to_reason,
        timeout_action=tt.get_action(task_id) if is_to else "",
    )


@router.get("/{task_id}", response_model=TaskFlowResponse)
async def get_task_flow(
    task_id: str,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> TaskFlowResponse:
    """Return a task DAG shape for the React Flow UI.

    This is intentionally read-only and derived from TaskRow + RuntimeStateRow.
    Full orchestration edits stay in later M4 wire work.
    """

    async with session_scope(tenant_id=x_tenant_id) as session:
        stmt = (
            select(TaskRow, RuntimeStateRow)
            .join(RuntimeStateRow, RuntimeStateRow.task_ref == TaskRow.task_id, isouter=True)
            .where(TaskRow.tenant_id == x_tenant_id, TaskRow.task_id == task_id)
            .limit(1)
        )
        row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    task, runtime = row
    steps = _build_flow_steps(task, runtime)
    return TaskFlowResponse(
        task_id=task.task_id,
        title=task.success_criteria_short,
        status=runtime.status if runtime is not None else "queued",
        steps=steps,
    )


@router.post("/{task_id}/steps/{step_id}/skip", response_model=StepControlResponse)
async def control_task_step(
    task_id: str,
    step_id: str,
    body: StepControlRequest,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")] = "u-sylvan",
) -> StepControlResponse:
    """Record a UI-requested step skip/force-done intent.

    TODO: orchestrator wire by Claude in M4 — consume `ui_control_events` from
    RuntimeStateRow.blob and apply it in the live step loop.
    """

    async with session_scope(tenant_id=x_tenant_id) as session:
        stmt = select(RuntimeStateRow).where(
            RuntimeStateRow.tenant_id == x_tenant_id,
            RuntimeStateRow.task_ref == task_id,
        )
        runtime = (await session.execute(stmt)).scalar_one_or_none()
        if runtime is None:
            raise HTTPException(status_code=404, detail="runtime state not found")
        blob = dict(runtime.blob or {})
        events = list(blob.get("ui_control_events", []))
        events.append(
            {
                "step_id": step_id,
                "action": body.action,
                "reason": body.reason,
                "requested_at": datetime.now(UTC).isoformat(),
            }
        )
        blob["ui_control_events"] = events
        runtime.blob = blob
    return StepControlResponse(
        task_id=task_id,
        step_id=step_id,
        action=body.action,
        accepted=True,
        message="已记录控制意图，等待执行循环消费。",
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


def _build_flow_steps(
    task: TaskRow,
    runtime: RuntimeStateRow | None,
) -> list[TaskFlowStepResponse]:
    completed_raw: list[dict[str, Any]] = []
    if runtime is not None and isinstance(runtime.blob, dict):
        raw_steps = runtime.blob.get("completed_steps", [])
        if isinstance(raw_steps, list):
            completed_raw = [step for step in raw_steps if isinstance(step, dict)]

    steps: list[TaskFlowStepResponse] = []
    for raw in completed_raw:
        step_num = int(raw.get("step_id") or len(steps) + 1)
        output_ref = raw.get("output_ref")
        duration_sec = float(raw.get("duration_sec") or raw.get("duration") or 0.0)
        steps.append(
            TaskFlowStepResponse(
                step_id=str(step_num),
                title=str(raw.get("skill_used") or f"step {step_num}"),
                status="done",
                deps=[str(step_num - 1)] if step_num > 1 else [],
                output=str(output_ref or raw.get("output") or ""),
                cost_usd=float(raw.get("cost_usd_equivalent") or raw.get("cost_usd") or 0.0),
                duration_ms=int(duration_sec * 1000),
            )
        )

    total = max(runtime.total_planned_steps if runtime is not None else 1, len(steps), 1)
    current = runtime.current_step if runtime is not None else 0
    status = runtime.status if runtime is not None else "queued"
    for step_num in range(len(steps) + 1, total + 1):
        if status == "running" and step_num == max(current + 1, 1):
            step_status: Literal["pending", "running", "done", "failed", "skipped"] = "running"
        elif status in {"failed", "cancelled"} and step_num == max(current + 1, 1):
            step_status = "failed"
        else:
            step_status = "pending"
        title = "下一步" if step_status == "running" else f"step {step_num}"
        input_preview = ""
        if runtime is not None and runtime.blob:
            next_plan = (
                runtime.blob.get("next_step_plan") if isinstance(runtime.blob, dict) else None
            )
            if isinstance(next_plan, dict) and step_status == "running":
                title = str(next_plan.get("skill") or title)
                input_preview = str(next_plan.get("input_preview") or "")
        steps.append(
            TaskFlowStepResponse(
                step_id=str(step_num),
                title=title,
                status=step_status,
                deps=[str(step_num - 1)] if step_num > 1 else [],
                input=input_preview,
            )
        )
    if not steps:
        steps.append(
            TaskFlowStepResponse(
                step_id="1",
                title=task.success_criteria_short[:60],
                status="pending",
            )
        )
    return steps


__all__ = ["router"]
