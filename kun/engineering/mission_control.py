"""Mission control for long-horizon KUN work.

This module is deliberately modest: it gives KUN durable missions, task links,
milestones, and resumable-task discovery. It does not pretend to execute the
whole mission by itself yet; it creates clear resume requests for the runtime to
consume.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.ids import new_id
from kun.core.orm import (
    MissionMilestoneRow,
    MissionRow,
    MissionTaskRow,
    RuntimeStateRow,
    TaskResultRow,
)
from kun.datamodel.events import Event
from kun.datamodel.mission import (
    MissionBlockedResult,
    MissionBudgetSummary,
    MissionCheckpointSummary,
    MissionCreate,
    MissionExecutionSummary,
    MissionMilestone,
    MissionReaperResult,
    MissionSnapshot,
    MissionTaskLink,
    ResumeRequest,
)


async def create_mission(
    payload: MissionCreate,
    *,
    tenant_id: str,
    user_id: str | None = None,
) -> MissionSnapshot:
    """Create a durable mission and emit mission.created."""

    mission_id = new_id("mission")
    async with session_scope(tenant_id=tenant_id) as s:
        row = MissionRow(
            mission_id=mission_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=payload.project_id,
            title=payload.title,
            objective=payload.objective,
            status="planned",
            risk_level=payload.risk_level,
            budget_cap_usd=payload.budget_cap_usd,
            success_metrics=payload.success_metrics,
            strategy_json=payload.strategy,
        )
        s.add(row)
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.created",
                payload={
                    "mission_id": mission_id,
                    "title": payload.title,
                    "risk_level": payload.risk_level,
                    "budget_cap_usd": payload.budget_cap_usd,
                },
            ),
        )
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def list_missions(
    *,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
) -> list[MissionSnapshot]:
    async with session_scope(tenant_id=tenant_id) as s:
        stmt: Select[tuple[MissionRow]] = (
            select(MissionRow)
            .where(MissionRow.tenant_id == tenant_id)
            .order_by(desc(MissionRow.updated_at))
            .limit(limit)
        )
        if status:
            stmt = stmt.where(MissionRow.status == status)
        rows = list((await s.execute(stmt)).scalars().all())
        return [
            await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=row.mission_id)
            for row in rows
        ]


async def get_mission(*, tenant_id: str, mission_id: str) -> MissionSnapshot | None:
    async with session_scope(tenant_id=tenant_id) as s:
        row = await s.get(MissionRow, mission_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def attach_task_to_mission(
    *,
    tenant_id: str,
    mission_id: str,
    task_id: str,
    role: str = "primary",
    sequence_no: int = 0,
    checkpoint: dict[str, Any] | None = None,
) -> MissionSnapshot:
    """Attach a durable TASK.md object to a mission."""

    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise KeyError(f"mission not found: {mission_id}")
        now = datetime.now(UTC)
        stmt = pg_insert(MissionTaskRow).values(
            tenant_id=tenant_id,
            mission_id=mission_id,
            task_id=task_id,
            role=role,
            sequence_no=sequence_no,
            status="planned",
            checkpoint_json=checkpoint or {},
            updated_at=now,
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    MissionTaskRow.tenant_id,
                    MissionTaskRow.mission_id,
                    MissionTaskRow.task_id,
                ],
                set_={
                    "role": role,
                    "sequence_no": sequence_no,
                    "checkpoint_json": checkpoint or {},
                    "updated_at": now,
                },
            )
        )
        mission.updated_at = now
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.task.attached",
                payload={
                    "mission_id": mission_id,
                    "task_id": task_id,
                    "role": role,
                    "sequence_no": sequence_no,
                },
                task_ref=task_id,
            ),
        )
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def record_milestone(
    milestone: MissionMilestone,
    *,
    tenant_id: str,
    mission_id: str,
) -> MissionSnapshot:
    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise KeyError(f"mission not found: {mission_id}")
        now = datetime.now(UTC)
        stmt = pg_insert(MissionMilestoneRow).values(
            milestone_id=milestone.milestone_id,
            tenant_id=tenant_id,
            mission_id=mission_id,
            title=milestone.title,
            status=milestone.status,
            sequence_no=milestone.sequence_no,
            task_ref=milestone.task_ref,
            due_at=milestone.due_at,
            checkpoint_json=milestone.checkpoint,
            completed_at=milestone.completed_at,
            updated_at=now,
        )
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[MissionMilestoneRow.milestone_id],
                set_={
                    "title": milestone.title,
                    "status": milestone.status,
                    "sequence_no": milestone.sequence_no,
                    "task_ref": milestone.task_ref,
                    "due_at": milestone.due_at,
                    "checkpoint_json": milestone.checkpoint,
                    "completed_at": milestone.completed_at,
                    "updated_at": now,
                },
            )
        )
        mission.updated_at = now
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.milestone.recorded",
                payload={
                    "mission_id": mission_id,
                    "milestone_id": milestone.milestone_id,
                    "title": milestone.title,
                    "status": milestone.status,
                    "task_ref": milestone.task_ref,
                },
                task_ref=milestone.task_ref,
            ),
        )
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def request_resumable_tasks(
    *,
    tenant_id: str,
    limit: int = 20,
    max_attempts: int = 3,
) -> list[ResumeRequest]:
    """Find queued mission tasks and mark that runtime resume was requested.

    This is the durable bridge between mission planning and execution. It does
    not run the task itself; it produces explicit resume requests and events so
    a worker can safely consume them.
    """

    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        await _block_exhausted_mission_tasks_in_session(
            s,
            tenant_id=tenant_id,
            max_attempts=max_attempts,
            limit=max(limit, 20),
            now=now,
        )
        rows = (await s.execute(_resumable_tasks_stmt(tenant_id, limit, max_attempts))).all()
        requests: list[ResumeRequest] = []
        for mission_task, runtime in rows:
            attempts = int(mission_task.resume_attempts) + 1
            mission_task.status = "queued"
            mission_task.resume_attempts = attempts
            mission_task.last_resume_requested_at = now
            mission_task.updated_at = now
            request = ResumeRequest(
                mission_id=mission_task.mission_id,
                task_id=mission_task.task_id,
                runtime_status=runtime.status,
                resume_attempts=attempts,
                reason="runtime_state_queued",
            )
            requests.append(request)
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="mission.task.resume_requested",
                    payload=request.model_dump(mode="json"),
                    task_ref=mission_task.task_id,
                ),
            )
        return requests


async def block_exhausted_mission_tasks(
    *,
    tenant_id: str,
    max_attempts: int = 3,
    limit: int = 100,
) -> list[MissionBlockedResult]:
    """Block queued mission tasks that already exhausted resume attempts."""

    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        return await _block_exhausted_mission_tasks_in_session(
            s,
            tenant_id=tenant_id,
            max_attempts=max_attempts,
            limit=limit,
            now=now,
        )


async def refresh_mission_task_statuses(*, tenant_id: str, mission_id: str) -> MissionSnapshot:
    """Copy current RuntimeState status into mission_tasks."""

    async with session_scope(tenant_id=tenant_id) as s:
        rows = (
            await s.execute(
                select(MissionTaskRow, RuntimeStateRow)
                .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
                .where(
                    MissionTaskRow.tenant_id == tenant_id,
                    MissionTaskRow.mission_id == mission_id,
                    RuntimeStateRow.tenant_id == tenant_id,
                )
            )
        ).all()
        for mission_task, runtime in rows:
            mission_task.status = runtime.status
            mission_task.updated_at = datetime.now(UTC)
        await _recompute_mission_status(s, tenant_id=tenant_id, mission_id=mission_id)
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def reap_stale_mission_tasks(
    *,
    tenant_id: str,
    queued_stale_after_sec: int = 900,
    running_stale_after_sec: int = 3600,
    limit: int = 50,
) -> list[MissionReaperResult]:
    """Fail mission tasks whose runtime has been queued/running too long."""

    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        rows = (
            await s.execute(
                _stale_mission_tasks_stmt(
                    tenant_id,
                    queued_stale_after_sec=queued_stale_after_sec,
                    running_stale_after_sec=running_stale_after_sec,
                    limit=limit,
                    now=now,
                )
            )
        ).all()
        results: list[MissionReaperResult] = []
        touched_missions: set[str] = set()
        for mission_task, runtime in rows:
            previous_status = str(runtime.status)
            stale_for_sec = max(0, int((now - runtime.last_updated).total_seconds()))
            reason = f"stale_{previous_status}_runtime"
            result = MissionReaperResult(
                mission_id=mission_task.mission_id,
                task_id=mission_task.task_id,
                previous_status=previous_status,
                reason=reason,
                stale_for_sec=stale_for_sec,
            )
            reaper_checkpoint = {
                "reason": reason,
                "previous_status": previous_status,
                "stale_for_sec": stale_for_sec,
                "reaped_at": now.isoformat(),
            }
            mission_task.status = "failed"
            mission_task.updated_at = now
            mission_task.checkpoint_json = {
                **dict(mission_task.checkpoint_json or {}),
                "last_reaper": reaper_checkpoint,
            }
            runtime.status = "failed"
            runtime.failures_this_run = int(runtime.failures_this_run or 0) + 1
            runtime.finished_at = now
            runtime.last_updated = now
            runtime.blob = {
                **dict(runtime.blob or {}),
                "mission_reaper": reaper_checkpoint,
            }
            touched_missions.add(mission_task.mission_id)
            results.append(result)
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="mission.task.reaped",
                    payload=result.model_dump(mode="json"),
                    task_ref=mission_task.task_id,
                ),
            )
        for mission_id in touched_missions:
            await _recompute_mission_status(s, tenant_id=tenant_id, mission_id=mission_id)
        await s.flush()
        return results


async def summarize_mission(
    *,
    tenant_id: str,
    mission_id: str,
) -> MissionExecutionSummary | None:
    """Return mission-level budget and checkpoint rollup."""

    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            return None
        rows = (
            await s.execute(
                select(MissionTaskRow, RuntimeStateRow, TaskResultRow)
                .outerjoin(
                    RuntimeStateRow,
                    and_(
                        RuntimeStateRow.tenant_id == MissionTaskRow.tenant_id,
                        RuntimeStateRow.task_ref == MissionTaskRow.task_id,
                    ),
                )
                .outerjoin(
                    TaskResultRow,
                    and_(
                        TaskResultRow.tenant_id == MissionTaskRow.tenant_id,
                        TaskResultRow.task_id == MissionTaskRow.task_id,
                    ),
                )
                .where(
                    MissionTaskRow.tenant_id == tenant_id,
                    MissionTaskRow.mission_id == mission_id,
                )
                .order_by(MissionTaskRow.sequence_no, MissionTaskRow.created_at)
            )
        ).all()

    spent_actual = 0.0
    spent_equivalent = 0.0
    checkpoints: list[MissionCheckpointSummary] = []
    status_counts: Counter[str] = Counter()
    for mission_task, runtime, result in rows:
        status_counts[str(mission_task.status)] += 1
        cost_actual = _task_cost_actual(runtime, result)
        cost_equivalent = _task_cost_equivalent(runtime, result)
        spent_actual += cost_actual
        spent_equivalent += cost_equivalent
        checkpoint = dict(mission_task.checkpoint_json or {})
        if runtime is not None and runtime.blob:
            checkpoint.setdefault("runtime", runtime.blob)
        checkpoints.append(
            MissionCheckpointSummary(
                task_id=mission_task.task_id,
                role=mission_task.role,
                status=mission_task.status,
                runtime_status=runtime.status if runtime is not None else None,
                resume_attempts=mission_task.resume_attempts,
                last_resume_requested_at=mission_task.last_resume_requested_at,
                last_runtime_updated_at=runtime.last_updated if runtime is not None else None,
                cost_usd_actual=cost_actual,
                cost_usd_equivalent=cost_equivalent,
                checkpoint=checkpoint,
            )
        )

    budget_cap = float(mission.budget_cap_usd or 0.0)
    budget = MissionBudgetSummary(
        budget_cap_usd=budget_cap,
        spent_actual_usd=spent_actual,
        spent_equivalent_usd=spent_equivalent,
        remaining_equivalent_usd=max(0.0, budget_cap - spent_equivalent) if budget_cap > 0 else 0.0,
        usage_fraction=spent_equivalent / budget_cap if budget_cap > 0 else 0.0,
    )
    return MissionExecutionSummary(
        mission_id=mission.mission_id,
        tenant_id=mission.tenant_id,
        status=mission.status,
        budget=budget,
        task_status_counts=dict(status_counts),
        checkpoints=checkpoints,
        updated_at=mission.updated_at,
    )


async def _recompute_mission_status(
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
    mission = await session.get(MissionRow, mission_id)
    if mission is None or mission.tenant_id != tenant_id or not statuses:
        return
    next_status = derive_mission_status(statuses)
    if next_status is None:
        return
    mission.status = next_status
    if next_status == "done":
        mission.finished_at = mission.finished_at or datetime.now(UTC)
    elif next_status == "running":
        mission.started_at = mission.started_at or datetime.now(UTC)
    mission.updated_at = datetime.now(UTC)


def derive_mission_status(statuses: list[str]) -> str | None:
    if not statuses:
        return None
    if all(status == "done" for status in statuses):
        return "done"
    if any(status in {"running", "queued"} for status in statuses):
        return "running"
    if any(status in {"paused", "blocked"} for status in statuses):
        return "paused"
    if any(status == "failed" for status in statuses):
        return "failed"
    if all(status == "cancelled" for status in statuses):
        return "cancelled"
    return "planned"


def _resumable_tasks_stmt(tenant_id: str, limit: int, max_attempts: int) -> Any:
    return (
        select(MissionTaskRow, RuntimeStateRow)
        .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
        .join(MissionRow, MissionRow.mission_id == MissionTaskRow.mission_id)
        .where(
            MissionTaskRow.tenant_id == tenant_id,
            RuntimeStateRow.tenant_id == tenant_id,
            MissionRow.tenant_id == tenant_id,
            MissionRow.status.in_(("planned", "running", "paused")),
            MissionTaskRow.status.in_(("planned", "queued", "running", "paused", "blocked")),
            RuntimeStateRow.status == "queued",
            MissionTaskRow.resume_attempts < max_attempts,
        )
        .order_by(MissionTaskRow.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def _block_exhausted_mission_tasks_in_session(
    session: Any,
    *,
    tenant_id: str,
    max_attempts: int,
    limit: int,
    now: datetime,
) -> list[MissionBlockedResult]:
    rows = (
        await session.execute(
            _exhausted_resume_attempts_stmt(
                tenant_id,
                max_attempts=max_attempts,
                limit=limit,
            )
        )
    ).all()
    results: list[MissionBlockedResult] = []
    touched_missions: set[str] = set()
    for mission_task, runtime in rows:
        previous_status = str(mission_task.status)
        runtime_status = str(runtime.status)
        attempts = int(mission_task.resume_attempts or 0)
        reason = "max_resume_attempts_exhausted"
        block_checkpoint = {
            "reason": reason,
            "previous_status": previous_status,
            "runtime_status": runtime_status,
            "resume_attempts": attempts,
            "max_attempts": max_attempts,
            "blocked_at": now.isoformat(),
        }
        result = MissionBlockedResult(
            mission_id=mission_task.mission_id,
            task_id=mission_task.task_id,
            previous_status=previous_status,
            runtime_status=runtime_status,
            reason=reason,
            resume_attempts=attempts,
            max_attempts=max_attempts,
        )
        mission_task.status = "blocked"
        mission_task.updated_at = now
        mission_task.checkpoint_json = {
            **dict(mission_task.checkpoint_json or {}),
            "last_blocked": block_checkpoint,
        }
        runtime.status = "paused"
        runtime.last_updated = now
        runtime.blob = {
            **dict(runtime.blob or {}),
            "mission_blocked": block_checkpoint,
        }
        touched_missions.add(mission_task.mission_id)
        results.append(result)
        await emit(
            session,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.task.blocked",
                payload=result.model_dump(mode="json"),
                task_ref=mission_task.task_id,
            ),
        )
    for mission_id in touched_missions:
        await _recompute_mission_status(session, tenant_id=tenant_id, mission_id=mission_id)
    await session.flush()
    return results


def _exhausted_resume_attempts_stmt(
    tenant_id: str,
    *,
    max_attempts: int,
    limit: int,
) -> Any:
    return (
        select(MissionTaskRow, RuntimeStateRow)
        .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
        .join(MissionRow, MissionRow.mission_id == MissionTaskRow.mission_id)
        .where(
            MissionTaskRow.tenant_id == tenant_id,
            RuntimeStateRow.tenant_id == tenant_id,
            MissionRow.tenant_id == tenant_id,
            MissionRow.status.in_(("planned", "running", "paused")),
            MissionTaskRow.status.in_(("planned", "queued", "running", "paused")),
            RuntimeStateRow.status == "queued",
            MissionTaskRow.resume_attempts >= max_attempts,
        )
        .order_by(MissionTaskRow.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def _stale_mission_tasks_stmt(
    tenant_id: str,
    *,
    queued_stale_after_sec: int,
    running_stale_after_sec: int,
    limit: int,
    now: datetime | None = None,
) -> Any:
    now = now or datetime.now(UTC)
    queued_cutoff = now - timedelta(seconds=queued_stale_after_sec)
    running_cutoff = now - timedelta(seconds=running_stale_after_sec)
    return (
        select(MissionTaskRow, RuntimeStateRow)
        .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
        .join(MissionRow, MissionRow.mission_id == MissionTaskRow.mission_id)
        .where(
            MissionTaskRow.tenant_id == tenant_id,
            RuntimeStateRow.tenant_id == tenant_id,
            MissionRow.tenant_id == tenant_id,
            MissionRow.status.in_(("planned", "running", "paused")),
            MissionTaskRow.status.in_(("queued", "running")),
            or_(
                and_(
                    RuntimeStateRow.status == "queued",
                    RuntimeStateRow.last_updated < queued_cutoff,
                ),
                and_(
                    RuntimeStateRow.status == "running",
                    RuntimeStateRow.last_updated < running_cutoff,
                ),
            ),
        )
        .order_by(RuntimeStateRow.last_updated)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def _task_cost_actual(runtime: RuntimeStateRow | None, result: TaskResultRow | None) -> float:
    if result is not None:
        return float(result.cost_usd_actual or 0.0)
    if runtime is not None:
        return float(runtime.accumulated_cost_usd_actual or 0.0)
    return 0.0


def _task_cost_equivalent(runtime: RuntimeStateRow | None, result: TaskResultRow | None) -> float:
    if result is not None:
        return float(result.cost_usd_equivalent or 0.0)
    if runtime is not None:
        return float(runtime.accumulated_cost_usd_equivalent or 0.0)
    return 0.0


async def _snapshot_from_session(
    session: Any,
    *,
    tenant_id: str,
    mission_id: str,
) -> MissionSnapshot:
    mission = await session.get(MissionRow, mission_id)
    if mission is None or mission.tenant_id != tenant_id:
        raise KeyError(f"mission not found: {mission_id}")
    task_rows = list(
        (
            await session.execute(
                select(MissionTaskRow)
                .where(
                    MissionTaskRow.tenant_id == tenant_id,
                    MissionTaskRow.mission_id == mission_id,
                )
                .order_by(MissionTaskRow.sequence_no, MissionTaskRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    milestone_rows = list(
        (
            await session.execute(
                select(MissionMilestoneRow)
                .where(
                    MissionMilestoneRow.tenant_id == tenant_id,
                    MissionMilestoneRow.mission_id == mission_id,
                )
                .order_by(MissionMilestoneRow.sequence_no, MissionMilestoneRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    return MissionSnapshot(
        mission_id=mission.mission_id,
        tenant_id=mission.tenant_id,
        user_id=mission.user_id,
        project_id=mission.project_id,
        title=mission.title,
        objective=mission.objective,
        status=mission.status,
        risk_level=mission.risk_level,
        budget_cap_usd=mission.budget_cap_usd,
        success_metrics=list(mission.success_metrics or []),
        strategy=dict(mission.strategy_json or {}),
        tasks=[
            MissionTaskLink(
                task_id=row.task_id,
                role=row.role,
                sequence_no=row.sequence_no,
                status=row.status,
                checkpoint=dict(row.checkpoint_json or {}),
                resume_attempts=row.resume_attempts,
                last_resume_requested_at=row.last_resume_requested_at,
            )
            for row in task_rows
        ],
        milestones=[
            MissionMilestone(
                milestone_id=row.milestone_id,
                title=row.title,
                status=row.status,
                sequence_no=row.sequence_no,
                task_ref=row.task_ref,
                due_at=row.due_at,
                checkpoint=dict(row.checkpoint_json or {}),
                completed_at=row.completed_at,
            )
            for row in milestone_rows
        ],
        created_at=mission.created_at,
        updated_at=mission.updated_at,
        started_at=mission.started_at,
        finished_at=mission.finished_at,
    )


async def count_active_missions(*, tenant_id: str) -> int:
    async with session_scope(tenant_id=tenant_id) as s:
        value = await s.execute(
            select(func.count())
            .select_from(MissionRow)
            .where(
                MissionRow.tenant_id == tenant_id,
                MissionRow.status.in_(("planned", "running", "paused")),
            )
        )
        return int(value.scalar_one())


__all__ = [
    "attach_task_to_mission",
    "block_exhausted_mission_tasks",
    "count_active_missions",
    "create_mission",
    "derive_mission_status",
    "get_mission",
    "list_missions",
    "reap_stale_mission_tasks",
    "record_milestone",
    "refresh_mission_task_statuses",
    "request_resumable_tasks",
    "summarize_mission",
]
