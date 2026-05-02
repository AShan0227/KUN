"""Mission control for long-horizon KUN work.

This module is deliberately modest: it gives KUN durable missions, task links,
milestones, and resumable-task discovery. It does not pretend to execute the
whole mission by itself yet; it creates clear resume requests for the runtime to
consume.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.ids import new_id
from kun.core.orm import (
    EventRow,
    MissionMilestoneRow,
    MissionRow,
    MissionTaskRow,
    RuntimeStateRow,
)
from kun.core.state_ledger import replay_state_ledger_story
from kun.datamodel.events import Event
from kun.datamodel.mission import (
    MissionCreate,
    MissionMilestone,
    MissionNextStep,
    MissionReview,
    MissionSnapshot,
    MissionStory,
    MissionStoryEvent,
    MissionTaskLink,
    MissionTaskStory,
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
            review_interval_hours=payload.review_interval_hours,
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


async def get_mission_story(
    *,
    tenant_id: str,
    mission_id: str,
    history_limit_per_task: int = 100,
) -> MissionStory | None:
    """Replay a Mission-level story from durable task links and EventRow history."""

    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            return None
        snapshot = await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)
        task_ids = [task.task_id for task in snapshot.tasks]
        histories: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
        mission_events: list[dict[str, Any]] = []
        if task_ids:
            event_limit = min(2000, max(100, history_limit_per_task * len(task_ids) + 100))
            stmt = (
                select(EventRow)
                .where(
                    EventRow.tenant_id == tenant_id,
                    or_(EventRow.task_ref.in_(task_ids), EventRow.task_ref.is_(None)),
                )
                .order_by(desc(EventRow.occurred_at))
                .limit(event_limit)
            )
            rows = list((await s.execute(stmt)).scalars().all())
        else:
            rows = list(
                (
                    await s.execute(
                        select(EventRow)
                        .where(EventRow.tenant_id == tenant_id, EventRow.task_ref.is_(None))
                        .order_by(desc(EventRow.occurred_at))
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )

    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        item = _mission_event_item_from_row(row)
        if row.task_ref in histories:
            bucket = histories[str(row.task_ref)]
            if len(bucket) < history_limit_per_task:
                bucket.append(item)
            continue
        if payload.get("mission_id") == mission_id:
            mission_events.append(item)

    return _build_mission_story(
        snapshot,
        histories=histories,
        mission_events=mission_events,
        history_limit_per_task=history_limit_per_task,
    )


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
            completed_by_task_id=milestone.completed_by_task_id,
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
                    "completed_by_task_id": milestone.completed_by_task_id,
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
                    "completed_by_task_id": milestone.completed_by_task_id,
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


async def refresh_mission_task_statuses(*, tenant_id: str, mission_id: str) -> MissionSnapshot:
    """Copy current RuntimeState status into mission_tasks and refresh rollup."""

    async with session_scope(tenant_id=tenant_id) as s:
        await _refresh_mission_rollup_in_session(s, tenant_id=tenant_id, mission_id=mission_id)
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def update_mission_next_step(
    next_step: MissionNextStep,
    *,
    tenant_id: str,
    mission_id: str,
) -> MissionSnapshot:
    """Persist the currently recommended next step for a long mission."""

    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise KeyError(f"mission not found: {mission_id}")
        now = datetime.now(UTC)
        if next_step.created_at is None:
            next_step = next_step.model_copy(update={"created_at": now})
        mission.next_step_json = next_step.model_dump(mode="json")
        mission.updated_at = now
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.next_step.updated",
                payload={"mission_id": mission_id, "next_step": mission.next_step_json},
                task_ref=next_step.task_id,
            ),
        )
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def record_mission_review(
    review: MissionReview,
    *,
    tenant_id: str,
    mission_id: str,
) -> MissionSnapshot:
    """Record a mission review without pretending it executed the next task."""

    async with session_scope(tenant_id=tenant_id) as s:
        mission = await s.get(MissionRow, mission_id)
        if mission is None or mission.tenant_id != tenant_id:
            raise KeyError(f"mission not found: {mission_id}")
        now = datetime.now(UTC)
        strategy = dict(mission.strategy_json or {})
        review_payload = review.model_dump(mode="json")
        history = list(strategy.get("review_history") or [])
        history.append({"recorded_at": now.isoformat(), **review_payload})
        strategy["review_history"] = history[-20:]
        strategy["last_review"] = {"recorded_at": now.isoformat(), **review_payload}
        mission.strategy_json = strategy
        mission.last_reviewed_at = now
        mission.updated_at = now
        if review.next_step is not None:
            next_step = review.next_step
            if next_step.created_at is None:
                next_step = next_step.model_copy(update={"created_at": now})
            mission.next_step_json = next_step.model_dump(mode="json")
        await emit(
            s,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.review.recorded",
                payload={
                    "mission_id": mission_id,
                    "summary": review.summary,
                    "budget_notes": review.budget_notes,
                    "risk_notes": review.risk_notes,
                    "next_step": mission.next_step_json if review.next_step else None,
                },
                task_ref=review.next_step.task_id if review.next_step else None,
            ),
        )
        await _refresh_mission_rollup_in_session(s, tenant_id=tenant_id, mission_id=mission_id)
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def refresh_mission_rollup(*, tenant_id: str, mission_id: str) -> MissionSnapshot:
    """Refresh budget/status/milestone rollups for a mission."""

    async with session_scope(tenant_id=tenant_id) as s:
        await _refresh_mission_rollup_in_session(s, tenant_id=tenant_id, mission_id=mission_id)
        await s.flush()
        return await _snapshot_from_session(s, tenant_id=tenant_id, mission_id=mission_id)


async def _refresh_mission_rollup_in_session(
    session: Any,
    *,
    tenant_id: str,
    mission_id: str,
) -> None:
    """Keep MissionRow as the fast current-state view over durable task/runtime rows."""

    mission = await session.get(MissionRow, mission_id)
    if mission is None or mission.tenant_id != tenant_id:
        raise KeyError(f"mission not found: {mission_id}")
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(MissionTaskRow, RuntimeStateRow)
            .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
            .where(
                MissionTaskRow.tenant_id == tenant_id,
                MissionTaskRow.mission_id == mission_id,
                RuntimeStateRow.tenant_id == tenant_id,
            )
        )
    ).all()
    budget_used = 0.0
    done_task_ids: set[str] = set()
    for mission_task, runtime in rows:
        mission_task.status = runtime.status
        mission_task.updated_at = now
        budget_used += float(runtime.accumulated_cost_usd_equivalent or 0.0)
        if runtime.status == "done":
            done_task_ids.add(mission_task.task_id)
    mission.budget_used_usd = max(0.0, budget_used)

    if done_task_ids:
        milestones = list(
            (
                await session.execute(
                    select(MissionMilestoneRow).where(
                        MissionMilestoneRow.tenant_id == tenant_id,
                        MissionMilestoneRow.mission_id == mission_id,
                        MissionMilestoneRow.task_ref.in_(done_task_ids),
                        MissionMilestoneRow.status.in_(("planned", "active")),
                    )
                )
            )
            .scalars()
            .all()
        )
        for milestone in milestones:
            milestone.status = "done"
            milestone.completed_by_task_id = milestone.task_ref
            milestone.completed_at = milestone.completed_at or now
            milestone.updated_at = now

    await _recompute_mission_status(session, tenant_id=tenant_id, mission_id=mission_id)
    if (
        mission.budget_cap_usd
        and mission.budget_cap_usd > 0
        and mission.budget_used_usd > mission.budget_cap_usd
        and mission.status not in {"done", "failed", "cancelled"}
    ):
        mission.status = "paused"
        mission.blocked_reason = (
            f"mission budget exceeded: used ${mission.budget_used_usd:.4f} "
            f"> cap ${mission.budget_cap_usd:.4f}"
        )
        mission.updated_at = now
        await emit(
            session,
            Event.build(
                tenant_id=tenant_id,
                event_type="mission.budget.exceeded",
                payload={
                    "mission_id": mission_id,
                    "budget_used_usd": mission.budget_used_usd,
                    "budget_cap_usd": mission.budget_cap_usd,
                    "blocked_reason": mission.blocked_reason,
                },
            ),
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
        budget_used_usd=mission.budget_used_usd,
        blocked_reason=mission.blocked_reason or "",
        next_step=_mission_next_step(mission.next_step_json),
        review_interval_hours=mission.review_interval_hours,
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
                completed_by_task_id=row.completed_by_task_id,
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
        last_reviewed_at=mission.last_reviewed_at,
    )


def _mission_next_step(raw: dict[str, Any] | None) -> MissionNextStep | None:
    if not raw:
        return None
    try:
        return MissionNextStep.model_validate(raw)
    except Exception:
        return MissionNextStep(
            summary=str(raw.get("summary") or "Review stored next step"),
            reason=str(
                raw.get("reason") or "Legacy mission next_step_json could not fully validate."
            ),
            task_id=raw.get("task_id") if isinstance(raw.get("task_id"), str) else None,
        )


def _build_mission_story(
    snapshot: MissionSnapshot,
    *,
    histories: dict[str, list[dict[str, Any]]],
    mission_events: list[dict[str, Any]],
    history_limit_per_task: int,
) -> MissionStory:
    task_stories: list[MissionTaskStory] = []
    all_events: list[MissionStoryEvent] = [
        MissionStoryEvent(
            event_id=str(item.get("event_id") or ""),
            event_type=str(item.get("event_type") or ""),
            occurred_at=_parse_event_time(item.get("occurred_at")),
            task_id=item.get("task_id") if isinstance(item.get("task_id"), str) else None,
            summary=str(item.get("summary") or ""),
            reason=str(item.get("reason") or ""),
            cost_usd=_float_value(item.get("cost_usd")),
        )
        for item in mission_events
    ]
    total_cost = sum(event.cost_usd for event in all_events)
    decision_count = 0
    world_action_count = 0
    external_action_count = 0
    risk_flags: list[str] = []
    open_questions: list[str] = []
    pending_confirmations: list[str] = []
    confidences: list[float] = []
    history_limit_reached = False
    latest_reason = ""
    current_action = ""

    for task in snapshot.tasks:
        task_history = histories.get(task.task_id, [])
        if len(task_history) >= history_limit_per_task:
            history_limit_reached = True
        replayed = replay_state_ledger_story(
            task.task_id,
            task_history,
            timeline_limit=10,
            history_limit_reached=len(task_history) >= history_limit_per_task,
        )
        task_story = MissionTaskStory(
            task_id=task.task_id,
            role=task.role,
            status=task.status,
            resume_attempts=task.resume_attempts,
            event_count=int(replayed.get("event_count") or 0),
            decision_count=int(replayed.get("decision_count") or 0),
            world_action_count=int(replayed.get("world_action_count") or 0),
            external_action_count=int(replayed.get("external_action_count") or 0),
            total_cost_usd=_float_value(replayed.get("total_cost_usd")),
            latest_reason=str(replayed.get("latest_reason") or ""),
            current_action=str(replayed.get("current_action") or ""),
            reconstruction_confidence=_float_value(replayed.get("reconstruction_confidence")),
            gaps=[str(item) for item in replayed.get("gaps", []) if item],
        )
        task_stories.append(task_story)
        total_cost += task_story.total_cost_usd
        decision_count += task_story.decision_count
        world_action_count += task_story.world_action_count
        external_action_count += task_story.external_action_count
        risk_flags.extend(str(item) for item in replayed.get("risk_flags", []) if item)
        open_questions.extend(str(item) for item in replayed.get("open_questions", []) if item)
        pending_confirmations.extend(
            str(item) for item in replayed.get("pending_confirmations", []) if item
        )
        if task_story.reconstruction_confidence:
            confidences.append(task_story.reconstruction_confidence)
        if not latest_reason and task_story.latest_reason:
            latest_reason = task_story.latest_reason
        if not current_action and task_story.current_action:
            current_action = task_story.current_action
        for item in task_history[:5]:
            all_events.append(
                MissionStoryEvent(
                    event_id=str(item.get("event_id") or ""),
                    event_type=str(item.get("event_type") or ""),
                    occurred_at=_parse_event_time(item.get("occurred_at")),
                    task_id=item.get("task_id")
                    if isinstance(item.get("task_id"), str)
                    else task.task_id,
                    summary=str(item.get("summary") or ""),
                    reason=str(item.get("reason") or ""),
                    cost_usd=_float_value(item.get("cost_usd")),
                )
            )

    all_events.sort(key=lambda item: item.occurred_at, reverse=True)
    if not latest_reason:
        latest_reason = next(
            (
                event.reason or event.summary
                for event in all_events
                if event.reason or event.summary
            ),
            "",
        )
    if not current_action and snapshot.next_step is not None:
        current_action = snapshot.next_step.summary
    return MissionStory(
        mission_id=snapshot.mission_id,
        title=snapshot.title,
        objective=snapshot.objective,
        status=snapshot.status,
        risk_level=snapshot.risk_level,
        task_count=len(snapshot.tasks),
        done_task_count=sum(1 for task in snapshot.tasks if task.status == "done"),
        blocked_task_count=sum(
            1 for task in snapshot.tasks if task.status in {"blocked", "paused"}
        ),
        event_count=len(mission_events) + sum(item.event_count for item in task_stories),
        decision_count=decision_count,
        world_action_count=world_action_count,
        external_action_count=external_action_count,
        total_event_cost_usd=round(total_cost, 6),
        budget_used_usd=snapshot.budget_used_usd,
        budget_cap_usd=snapshot.budget_cap_usd,
        latest_reason=latest_reason,
        current_action=current_action,
        pending_confirmations=_dedupe(pending_confirmations)[:10],
        risk_flags=_dedupe(risk_flags)[:10],
        open_questions=_dedupe(open_questions)[:10],
        reconstruction_confidence=round(sum(confidences) / len(confidences), 3)
        if confidences
        else (0.4 if snapshot.tasks and all_events else 0.0),
        history_limit_reached=history_limit_reached,
        next_step=snapshot.next_step,
        tasks=task_stories,
        timeline=all_events[:20],
    )


def _mission_event_item_from_row(row: EventRow) -> dict[str, Any]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    ticket = payload.get("decision_ticket")
    if not isinstance(ticket, dict):
        ticket = payload if payload.get("ticket_id") and payload.get("decision_point") else {}
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "task_id": row.task_ref,
        "summary": row.subject[:200],
        "reason": _first_text(
            ticket.get("reason") if isinstance(ticket, dict) else None,
            payload.get("reason"),
            payload.get("message"),
            payload.get("reason_summary"),
            payload.get("error"),
        ),
        "cost_usd": _event_cost(payload, ticket if isinstance(ticket, dict) else {}),
        "decision_ticket_id": ticket.get("ticket_id") if isinstance(ticket, dict) else None,
        "decision_point": ticket.get("decision_point") if isinstance(ticket, dict) else "",
        "phase": ticket.get("phase") if isinstance(ticket, dict) else "",
        "selected_action": ticket.get("selected_action") if isinstance(ticket, dict) else "",
        "decision_status": ticket.get("status") if isinstance(ticket, dict) else "",
        "payload": payload,
    }


def _event_cost(payload: dict[str, Any], ticket: dict[str, Any]) -> float:
    for source in (payload, ticket, ticket.get("metadata"), ticket.get("evidence")):
        if not isinstance(source, dict):
            continue
        for key in ("cost_delta_usd", "cost_usd", "cost_usd_actual"):
            try:
                value = source.get(key)
                if value is not None:
                    return round(float(value), 6)
            except (TypeError, ValueError):
                continue
    return 0.0


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _float_value(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


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
    "count_active_missions",
    "create_mission",
    "derive_mission_status",
    "get_mission",
    "get_mission_story",
    "list_missions",
    "record_milestone",
    "record_mission_review",
    "refresh_mission_rollup",
    "refresh_mission_task_statuses",
    "request_resumable_tasks",
    "update_mission_next_step",
]
