"""Resume ordinary tasks after approved side-effect actions.

This is intentionally conservative.  The original paused TaskRow is not
replayed in-place; KUN starts a continuation task through the shared
Orchestrator, then writes the continuation outcome back onto the original
task's RuntimeState/TaskResult view.  That keeps the user-facing task card from
getting stuck at "queued" after a pending action is approved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.orm import MissionTaskRow, RuntimeStateRow, TaskResultRow, TaskRow
from kun.core.tenancy import TenantContext, tenant_scope
from kun.datamodel.events import Event
from kun.datamodel.runtime import TaskStatus

if TYPE_CHECKING:
    from kun.engineering.orchestrator import Orchestrator


class PendingTaskResumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_task_id: str
    continuation_task_id: str | None = None
    status: Literal["completed", "skipped", "failed"]
    final_status: TaskStatus | None = None
    message: str = ""


class PendingTaskResumeWorker:
    """Durable scanner for ordinary tasks waiting for continuation.

    Approval API already triggers one background resume attempt.  This worker
    is the safety net: if the API process crashes after marking a task queued,
    cron can pick it up later.  It only scans rows with an explicit
    ``resume_request`` marker, so normal queued tasks are not accidentally run.
    """

    def __init__(self, orchestrator: Orchestrator, *, max_tasks_per_run: int = 5) -> None:
        self.orchestrator = orchestrator
        self.max_tasks_per_run = max(1, min(max_tasks_per_run, 50))

    async def run_once(
        self,
        *,
        tenant_id: str,
        task_ids: list[str] | None = None,
    ) -> list[PendingTaskResumeResult]:
        targets = task_ids or await find_resume_ready_task_ids(
            tenant_id=tenant_id,
            limit=self.max_tasks_per_run,
        )
        results: list[PendingTaskResumeResult] = []
        for task_id in targets[: self.max_tasks_per_run]:
            results.append(
                await resume_unblocked_task_once(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    orchestrator=self.orchestrator,
                )
            )
        return results


async def find_resume_ready_task_ids(*, tenant_id: str, limit: int = 20) -> list[str]:
    """Find ordinary queued tasks that explicitly requested continuation."""

    safe_limit = max(1, min(limit, 100))
    async with session_scope(tenant_id=tenant_id) as s:
        mission_exists = (
            select(MissionTaskRow.task_id)
            .where(
                MissionTaskRow.tenant_id == tenant_id,
                MissionTaskRow.task_id == TaskRow.task_id,
            )
            .exists()
        )
        rows = (
            await s.execute(
                select(TaskRow.task_id, TaskResultRow.result_json)
                .join(
                    RuntimeStateRow,
                    (RuntimeStateRow.task_ref == TaskRow.task_id)
                    & (RuntimeStateRow.tenant_id == TaskRow.tenant_id),
                )
                .join(
                    TaskResultRow,
                    (TaskResultRow.task_id == TaskRow.task_id)
                    & (TaskResultRow.tenant_id == TaskRow.tenant_id),
                )
                .where(
                    TaskRow.tenant_id == tenant_id,
                    RuntimeStateRow.status == "queued",
                    TaskResultRow.status == "queued",
                    ~mission_exists,
                )
                .order_by(RuntimeStateRow.last_updated.asc())
                .limit(safe_limit)
            )
        ).all()
    return [
        str(task_id)
        for task_id, result_json in rows
        if _resume_request_from_result_json(dict(result_json or {}))
    ]


async def resume_unblocked_task_once(
    *,
    tenant_id: str,
    task_id: str,
    orchestrator: Orchestrator,
) -> PendingTaskResumeResult:
    """Run one continuation for a task unblocked by approved pending actions."""

    claimed = await _claim_resume_ready_task(tenant_id=tenant_id, task_id=task_id)
    if claimed is None:
        return PendingTaskResumeResult(
            source_task_id=task_id,
            status="skipped",
            message="task is not queued for pending-action continuation",
        )

    prompt = _build_resume_prompt_from_task(claimed)
    try:
        with tenant_scope(TenantContext(tenant_id=tenant_id, user_id=claimed.user_id)):
            result = await orchestrator.run(prompt, output_kind="pending_action_resume")
    except Exception as exc:
        await _record_resume_exception(tenant_id=tenant_id, task_id=task_id, exc=exc)
        return PendingTaskResumeResult(
            source_task_id=task_id,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
        )

    await _write_continuation_result(
        tenant_id=tenant_id,
        source_task=claimed,
        continuation_task_id=result.task_id,
        final_status=result.status,
        answer=result.answer,
        cost_usd_actual=result.cost_usd_actual,
        cost_usd_equivalent=result.cost_usd_equivalent,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        duration_sec=result.duration_sec,
    )
    return PendingTaskResumeResult(
        source_task_id=task_id,
        continuation_task_id=result.task_id,
        status="completed",
        final_status=result.status,
        message=f"continuation completed with {result.status}",
    )


async def _claim_resume_ready_task(*, tenant_id: str, task_id: str) -> TaskRow | None:
    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        row = (
            await s.execute(
                select(TaskRow, RuntimeStateRow, TaskResultRow)
                .join(
                    RuntimeStateRow,
                    (RuntimeStateRow.task_ref == TaskRow.task_id)
                    & (RuntimeStateRow.tenant_id == TaskRow.tenant_id),
                )
                .join(
                    TaskResultRow,
                    (TaskResultRow.task_id == TaskRow.task_id)
                    & (TaskResultRow.tenant_id == TaskRow.tenant_id),
                )
                .where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.task_id == task_id,
                    RuntimeStateRow.status == "queued",
                    TaskResultRow.status == "queued",
                )
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is None:
            return None
        task, runtime, result = row
        result_json = dict(result.result_json or {})
        resume_request = _resume_request_from_result_json(result_json)
        if not resume_request:
            return None

        runtime.status = "running"
        runtime.finished_at = None
        runtime.last_updated = now
        runtime.blob = {
            **dict(runtime.blob or {}),
            "pending_action_resume": {
                "status": "running",
                "started_at": now.isoformat(),
                "mode": "continuation_task",
            },
            "resume_request": {
                **resume_request,
                "status": "running",
                "attempts": int(resume_request.get("attempts") or 0) + 1,
                "claimed_at": now.isoformat(),
            },
        }
        result.status = "running"
        result.answer = "审批已通过，任务正在恢复执行。"
        result.updated_at = now
        result.result_json = {
            **result_json,
            "status": "running",
            "answer": result.answer,
            "resume_started_at": now.isoformat(),
            "resume_request": {
                **resume_request,
                "status": "running",
                "attempts": int(resume_request.get("attempts") or 0) + 1,
                "claimed_at": now.isoformat(),
            },
        }
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="task.continuation.started",
                payload={
                    "task_id": task_id,
                    "reason": "pending_action_continuation_started",
                    "resume_state": "running",
                    "mode": "continuation_task",
                },
                task_ref=task_id,
            ),
        )
        return cast(TaskRow, task)


def _build_resume_prompt_from_task(task: TaskRow) -> str:
    spec = dict(task.spec_json or {})
    goal_detail = str(spec.get("goal_detail") or task.success_criteria_short)
    success_metrics = spec.get("success_metrics") or []
    constraints = spec.get("constraints") or []
    required_skills = spec.get("required_skills") or []
    required_tools = spec.get("required_tools") or []
    return "\n".join(
        [
            "Continue this KUN task after the user approved its pending side-effect actions.",
            "",
            f"Original task ID: {task.task_id}",
            f"Task type: {task.task_type}",
            f"Risk level: {task.risk_level}",
            f"Original goal: {goal_detail}",
            f"Success criteria: {task.success_criteria_short}",
            f"Success metrics: {success_metrics}",
            f"Required skills: {required_skills}",
            f"Required tools: {required_tools}",
            f"Constraints: {constraints}",
            "",
            "Important rules:",
            "- Approved side-effect actions have already passed the guarded executor.",
            "- Do not repeat an external side effect unless a new approval is explicitly needed.",
            "- Finish the remaining user-facing deliverable and explain the result clearly.",
        ]
    )


async def _record_resume_exception(*, tenant_id: str, task_id: str, exc: Exception) -> None:
    now = datetime.now(UTC)
    message = f"审批后恢复执行失败：{type(exc).__name__}: {exc}"
    async with session_scope(tenant_id=tenant_id) as s:
        row = (
            await s.execute(
                select(RuntimeStateRow, TaskResultRow)
                .join(
                    TaskResultRow,
                    (TaskResultRow.task_id == RuntimeStateRow.task_ref)
                    & (TaskResultRow.tenant_id == RuntimeStateRow.tenant_id),
                )
                .where(
                    RuntimeStateRow.tenant_id == tenant_id,
                    RuntimeStateRow.task_ref == task_id,
                )
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is not None:
            runtime, result = row
            runtime.status = "failed"
            runtime.finished_at = now
            runtime.last_updated = now
            runtime.blob = {
                **dict(runtime.blob or {}),
                "pending_action_resume": {
                    "status": "failed",
                    "error": str(exc),
                    "finished_at": now.isoformat(),
                },
            }
            result.status = "failed"
            result.answer = message
            result.updated_at = now
            result.result_json = {
                **dict(result.result_json or {}),
                "status": "failed",
                "answer": message,
                "resume_error": str(exc),
                "resume_finished_at": now.isoformat(),
            }
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="task.continuation.failed",
                payload={
                    "task_id": task_id,
                    "status": "failed",
                    "reason": "pending_action_continuation_failed",
                    "error": str(exc),
                },
                task_ref=task_id,
            ),
        )


async def _write_continuation_result(
    *,
    tenant_id: str,
    source_task: TaskRow,
    continuation_task_id: str,
    final_status: TaskStatus,
    answer: str,
    cost_usd_actual: float,
    cost_usd_equivalent: float,
    tokens_in: int,
    tokens_out: int,
    duration_sec: float,
) -> None:
    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        row = (
            await s.execute(
                select(RuntimeStateRow, TaskResultRow)
                .join(
                    TaskResultRow,
                    (TaskResultRow.task_id == RuntimeStateRow.task_ref)
                    & (TaskResultRow.tenant_id == RuntimeStateRow.tenant_id),
                )
                .where(
                    RuntimeStateRow.tenant_id == tenant_id,
                    RuntimeStateRow.task_ref == source_task.task_id,
                )
                .with_for_update(skip_locked=True)
            )
        ).one_or_none()
        if row is not None:
            runtime, result = row
            runtime.status = final_status
            runtime.finished_at = now if final_status in {"done", "failed", "cancelled"} else None
            runtime.last_updated = now
            runtime.accumulated_cost_usd_actual += cost_usd_actual
            runtime.accumulated_cost_usd_equivalent += cost_usd_equivalent
            runtime.accumulated_tokens += tokens_in + tokens_out
            runtime.blob = {
                **dict(runtime.blob or {}),
                "pending_action_resume": {
                    "status": final_status,
                    "continuation_task_id": continuation_task_id,
                    "finished_at": now.isoformat(),
                },
                "resume_request": {
                    **dict((runtime.blob or {}).get("resume_request") or {}),
                    "needed": False,
                    "status": final_status,
                    "continuation_task_id": continuation_task_id,
                    "completed_at": now.isoformat(),
                },
            }
            result.status = final_status
            result.answer = answer
            result.cost_usd_actual += cost_usd_actual
            result.cost_usd_equivalent += cost_usd_equivalent
            result.tokens_in += tokens_in
            result.tokens_out += tokens_out
            result.duration_sec += duration_sec
            result.updated_at = now
            result.result_json = {
                **dict(result.result_json or {}),
                "status": final_status,
                "answer": answer,
                "continuation_task_id": continuation_task_id,
                "resume_finished_at": now.isoformat(),
                "resume_ready": False,
                "resume_request": {
                    **dict((result.result_json or {}).get("resume_request") or {}),
                    "needed": False,
                    "status": final_status,
                    "continuation_task_id": continuation_task_id,
                    "completed_at": now.isoformat(),
                },
            }
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="task.continuation.finished",
                payload={
                    "task_id": source_task.task_id,
                    "status": final_status,
                    "reason": "pending_action_continuation_completed",
                    "continuation_task_id": continuation_task_id,
                    "duration_sec": duration_sec,
                    "tokens": tokens_in + tokens_out,
                    "accumulated_cost_usd": cost_usd_equivalent,
                },
                task_ref=source_task.task_id,
            ),
        )
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type=_event_type_for_status(final_status),
                payload={
                    "task_id": source_task.task_id,
                    "status": final_status,
                    "reason": "pending_action_continuation_completed",
                    "continuation_task_id": continuation_task_id,
                },
                task_ref=source_task.task_id,
            ),
        )


def _resume_request_from_result_json(result_json: dict[str, Any]) -> dict[str, Any]:
    raw = result_json.get("resume_request")
    if isinstance(raw, dict):
        needed = raw.get("needed") is True or result_json.get("resume_ready") is True
        status = str(raw.get("status") or "")
        if needed and status in {"queued", "failed", ""}:
            return dict(raw)
        return {}
    if result_json.get("resume_ready") is True:
        return {
            "needed": True,
            "status": "queued",
            "reason": result_json.get("resume_reason") or "all_pending_actions_executed",
            "pending_action_ids": [],
            "attempts": 0,
        }
    return {}


def _event_type_for_status(
    status: TaskStatus,
) -> Literal["task.done", "task.failed", "task.cancelled", "task.paused"]:
    if status == "done":
        return "task.done"
    if status == "cancelled":
        return "task.cancelled"
    if status == "paused":
        return "task.paused"
    return "task.failed"


__all__ = [
    "PendingTaskResumeResult",
    "PendingTaskResumeWorker",
    "find_resume_ready_task_ids",
    "resume_unblocked_task_once",
]
