"""Mission resume worker.

The worker is honest by design: it only claims work moved forward when a real
runner accepts or completes the resume request. Without a runner it emits an
explicit skipped result instead of pretending long-horizon execution is wired.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from textwrap import shorten
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.orm import MissionRow, MissionTaskRow, RuntimeStateRow, TaskRow
from kun.core.tenancy import TenantContext, current_tenant, tenant_scope
from kun.datamodel.events import Event, EventKind
from kun.datamodel.mission import ResumeRequest
from kun.datamodel.runtime import TaskStatus
from kun.engineering.mission_control import request_resumable_tasks

if TYPE_CHECKING:
    from kun.engineering.orchestrator import Orchestrator, TaskResult

MissionResumeStatus = Literal["completed", "dispatched", "skipped", "failed"]
MissionResumeRunner = Callable[[ResumeRequest], Awaitable["MissionRunnerOutcome | None"]]


class MissionRunnerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executed_task_id: str | None = None
    final_status: TaskStatus
    answer_preview: str = ""
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_sec: float = 0.0


class MissionResumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    status: MissionResumeStatus
    reason: str = ""
    outcome: MissionRunnerOutcome | None = None


class MissionOrchestratorRunner:
    """Run a durable mission continuation through the shared Orchestrator.

    The current implementation starts a continuation task and records that
    outcome back onto the Mission checkpoint. It is not an in-place restart of
    the original TaskRow, so the executed task id can differ from request.task_id.
    """

    def __init__(self, orchestrator: Orchestrator, *, output_kind: str = "mission_worker") -> None:
        self.orchestrator = orchestrator
        self.output_kind = output_kind

    async def __call__(self, request: ResumeRequest) -> MissionRunnerOutcome:
        tenant = current_tenant()
        prompt = await _build_orchestrator_resume_prompt(tenant.tenant_id, request)
        await _mark_execution_started(tenant.tenant_id, request)
        try:
            result = await self.orchestrator.run_mission_continuation(
                request,
                prompt,
                output_kind=self.output_kind,
            )
        except Exception as exc:
            await _record_execution_exception(tenant.tenant_id, request, exc)
            raise

        outcome = _outcome_from_task_result(result)
        await _record_execution_outcome(tenant.tenant_id, request, outcome)
        return outcome


class MissionResumeWorker:
    """Scan durable mission tasks and hand them to a real runner."""

    def __init__(self, runner: MissionResumeRunner | None = None) -> None:
        self.runner = runner

    async def run_once(
        self,
        *,
        tenant_id: str,
        limit: int = 20,
        max_attempts: int = 3,
    ) -> list[MissionResumeResult]:
        requests = await request_resumable_tasks(
            tenant_id=tenant_id,
            limit=limit,
            max_attempts=max_attempts,
        )
        results: list[MissionResumeResult] = []
        for request in requests:
            if self.runner is None:
                result = MissionResumeResult(
                    mission_id=request.mission_id,
                    task_id=request.task_id,
                    status="skipped",
                    reason="no mission resume runner attached",
                )
                await _emit_resume_result(tenant_id, result)
                results.append(result)
                continue
            try:
                with tenant_scope(TenantContext(tenant_id=tenant_id)):
                    outcome = await self.runner(request)
            except Exception as e:
                result = MissionResumeResult(
                    mission_id=request.mission_id,
                    task_id=request.task_id,
                    status="failed",
                    reason=f"{type(e).__name__}: {e}",
                )
            else:
                if outcome is None:
                    result = MissionResumeResult(
                        mission_id=request.mission_id,
                        task_id=request.task_id,
                        status="dispatched",
                        reason="runner accepted resume request",
                    )
                else:
                    result = MissionResumeResult(
                        mission_id=request.mission_id,
                        task_id=request.task_id,
                        status="completed",
                        reason=f"runner completed with {outcome.final_status}",
                        outcome=outcome,
                    )
            await _emit_resume_result(tenant_id, result)
            results.append(result)
        return results


async def _emit_resume_result(tenant_id: str, result: MissionResumeResult) -> None:
    event_type: EventKind
    if result.status == "completed":
        event_type = "mission.task.resume_completed"
    elif result.status == "dispatched":
        event_type = "mission.task.resume_dispatched"
    elif result.status == "failed":
        event_type = "mission.task.resume_failed"
    else:
        event_type = "mission.task.resume_skipped"
    async with session_scope(tenant_id=tenant_id) as s:
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type=event_type,
                payload=result.model_dump(mode="json"),
                task_ref=result.task_id,
            ),
        )


async def _build_orchestrator_resume_prompt(tenant_id: str, request: ResumeRequest) -> str:
    async with session_scope(tenant_id=tenant_id) as s:
        row = (
            await s.execute(
                select(MissionRow, MissionTaskRow, TaskRow)
                .join(
                    MissionTaskRow,
                    (MissionTaskRow.mission_id == MissionRow.mission_id)
                    & (MissionTaskRow.tenant_id == MissionRow.tenant_id),
                )
                .join(TaskRow, TaskRow.task_id == MissionTaskRow.task_id)
                .where(
                    MissionRow.tenant_id == tenant_id,
                    MissionRow.mission_id == request.mission_id,
                    MissionTaskRow.task_id == request.task_id,
                    TaskRow.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
    if row is None:
        raise KeyError(f"mission task not found: {request.mission_id}/{request.task_id}")

    mission, mission_task, task = row
    spec = task.spec_json or {}
    goal_detail = str(spec.get("goal_detail") or task.success_criteria_short)
    success_metrics = spec.get("success_metrics") or []
    constraints = spec.get("constraints") or []
    required_skills = spec.get("required_skills") or []
    required_tools = spec.get("required_tools") or []

    return "\n".join(
        [
            "Resume this durable KUN mission task and produce the next concrete deliverable.",
            "",
            f"Mission ID: {mission.mission_id}",
            f"Mission title: {mission.title}",
            f"Mission objective: {mission.objective}",
            f"Mission risk: {mission.risk_level}",
            f"Mission budget cap USD: {mission.budget_cap_usd}",
            "",
            f"Original task ID: {task.task_id}",
            f"Original task type: {task.task_type}",
            f"Mission task role: {mission_task.role}",
            f"Mission task sequence: {mission_task.sequence_no}",
            f"Resume attempt: {request.resume_attempts}",
            f"Resume reason: {request.reason}",
            f"Runtime status before resume: {request.runtime_status}",
            "",
            f"Task goal: {goal_detail}",
            f"Success criteria: {task.success_criteria_short}",
            f"Success metrics: {success_metrics}",
            f"Required skills: {required_skills}",
            f"Required tools: {required_tools}",
            f"Constraints: {constraints}",
            "",
            "Rules:",
            "- Continue from the mission objective, not from a blank slate.",
            "- If a real-world side effect needs approval, stop with a clear approval request.",
            "- Return a concise execution result and the next recommended checkpoint.",
        ]
    )


async def _mark_execution_started(tenant_id: str, request: ResumeRequest) -> None:
    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        await s.execute(
            update(MissionTaskRow)
            .where(
                MissionTaskRow.tenant_id == tenant_id,
                MissionTaskRow.mission_id == request.mission_id,
                MissionTaskRow.task_id == request.task_id,
            )
            .values(status="running", updated_at=now)
        )
        await s.execute(
            update(MissionRow)
            .where(MissionRow.tenant_id == tenant_id, MissionRow.mission_id == request.mission_id)
            .values(status="running", started_at=now, updated_at=now)
        )
        await s.execute(
            update(RuntimeStateRow)
            .where(
                RuntimeStateRow.tenant_id == tenant_id, RuntimeStateRow.task_ref == request.task_id
            )
            .values(status="running", last_updated=now)
        )
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.task.orchestrator_started",
                payload=request.model_dump(mode="json"),
                task_ref=request.task_id,
            ),
        )


async def _record_execution_outcome(
    tenant_id: str,
    request: ResumeRequest,
    outcome: MissionRunnerOutcome,
) -> None:
    now = datetime.now(UTC)
    continuation = {
        **outcome.model_dump(mode="json"),
        "mode": "mission_continuation_task",
        "source_task_id": request.task_id,
    }
    checkpoint_patch = {
        "last_orchestrator_run": continuation,
        "last_resume_request": request.model_dump(mode="json"),
        "updated_at": now.isoformat(),
    }
    async with session_scope(tenant_id=tenant_id) as s:
        mission_task_checkpoint = (
            await s.execute(
                select(MissionTaskRow.checkpoint_json).where(
                    MissionTaskRow.tenant_id == tenant_id,
                    MissionTaskRow.mission_id == request.mission_id,
                    MissionTaskRow.task_id == request.task_id,
                )
            )
        ).scalar_one_or_none()
        runtime_blob = (
            await s.execute(
                select(RuntimeStateRow.blob).where(
                    RuntimeStateRow.tenant_id == tenant_id,
                    RuntimeStateRow.task_ref == request.task_id,
                )
            )
        ).scalar_one_or_none()
        checkpoint = {**dict(mission_task_checkpoint or {}), **checkpoint_patch}
        merged_blob = {**dict(runtime_blob or {}), "mission_resume": checkpoint_patch}
        await s.execute(
            update(MissionTaskRow)
            .where(
                MissionTaskRow.tenant_id == tenant_id,
                MissionTaskRow.mission_id == request.mission_id,
                MissionTaskRow.task_id == request.task_id,
            )
            .values(
                status=outcome.final_status,
                checkpoint_json=checkpoint,
                updated_at=now,
            )
        )
        await s.execute(
            update(RuntimeStateRow)
            .where(
                RuntimeStateRow.tenant_id == tenant_id, RuntimeStateRow.task_ref == request.task_id
            )
            .values(
                status=outcome.final_status,
                current_step=1 if outcome.final_status in {"done", "failed", "cancelled"} else 0,
                finished_at=now
                if outcome.final_status in {"done", "failed", "cancelled"}
                else None,
                last_updated=now,
                blob=merged_blob,
            )
        )
        await _recompute_mission_status_inline(
            s, tenant_id=tenant_id, mission_id=request.mission_id
        )
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.task.orchestrator_finished",
                payload={
                    "mission_id": request.mission_id,
                    "task_id": request.task_id,
                    **continuation,
                },
                task_ref=request.task_id,
            ),
        )


async def _record_execution_exception(
    tenant_id: str,
    request: ResumeRequest,
    exc: Exception,
) -> None:
    outcome = MissionRunnerOutcome(
        final_status="failed",
        answer_preview=f"{type(exc).__name__}: {exc}",
    )
    await _record_execution_outcome(tenant_id, request, outcome)


async def _recompute_mission_status_inline(
    session: Any,
    *,
    tenant_id: str,
    mission_id: str,
) -> None:
    statuses = list(
        (
            await session.execute(
                select(MissionTaskRow.status).where(
                    MissionTaskRow.tenant_id == tenant_id,
                    MissionTaskRow.mission_id == mission_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not statuses:
        return
    if all(status == "done" for status in statuses):
        mission_status = "done"
        finished_at = datetime.now(UTC)
    elif any(status in {"running", "queued"} for status in statuses):
        mission_status = "running"
        finished_at = None
    elif any(status in {"paused", "blocked"} for status in statuses):
        mission_status = "paused"
        finished_at = None
    elif any(status == "failed" for status in statuses):
        mission_status = "failed"
        finished_at = datetime.now(UTC)
    elif all(status == "cancelled" for status in statuses):
        mission_status = "cancelled"
        finished_at = datetime.now(UTC)
    else:
        mission_status = "planned"
        finished_at = None
    values: dict[str, Any] = {"status": mission_status, "updated_at": datetime.now(UTC)}
    if finished_at is not None:
        values["finished_at"] = finished_at
    await session.execute(
        update(MissionRow)
        .where(MissionRow.tenant_id == tenant_id, MissionRow.mission_id == mission_id)
        .values(**values)
    )


def _outcome_from_task_result(result: TaskResult) -> MissionRunnerOutcome:
    return MissionRunnerOutcome(
        executed_task_id=result.task_id,
        final_status=result.status,
        answer_preview=shorten(result.answer, width=500, placeholder="..."),
        cost_usd_actual=result.cost_usd_actual,
        cost_usd_equivalent=result.cost_usd_equivalent,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        duration_sec=result.duration_sec,
    )


__all__ = [
    "MissionOrchestratorRunner",
    "MissionResumeResult",
    "MissionResumeRunner",
    "MissionResumeStatus",
    "MissionResumeWorker",
    "MissionRunnerOutcome",
]
