"""Mission stale-task reaper.

Long missions cannot rely on a human noticing that a task is stuck. The reaper
turns stale runtime rows back into explicit queued work or blocks the mission
when retry budget is exhausted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.metrics import mission_reaper_actions_total
from kun.core.orm import MissionRow, MissionTaskRow, RuntimeStateRow
from kun.datamodel.events import Event

MissionReapAction = Literal["requeued", "blocked"]


class MissionReapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    action: MissionReapAction
    previous_runtime_status: str
    resume_attempts: int
    reason: str


async def reap_stale_mission_tasks(
    *,
    tenant_id: str,
    stale_after: timedelta = timedelta(minutes=30),
    max_attempts: int = 3,
    limit: int = 50,
) -> list[MissionReapResult]:
    """Requeue or block stale mission tasks.

    The worker is deliberately conservative: it only touches mission-linked
    tasks whose runtime state has not moved for ``stale_after``.
    """

    now = datetime.now(UTC)
    stale_before = now - stale_after
    results: list[MissionReapResult] = []
    async with session_scope(tenant_id=tenant_id) as s:
        rows = (
            await s.execute(
                _stale_mission_tasks_stmt(
                    tenant_id=tenant_id,
                    stale_before=stale_before,
                    limit=limit,
                )
            )
        ).all()
        for mission, mission_task, runtime in rows:
            previous_status = runtime.status
            checkpoint = dict(mission_task.checkpoint_json or {})
            checkpoint["last_reaper"] = {
                "at": now.isoformat(),
                "previous_runtime_status": previous_status,
                "stale_after_sec": int(stale_after.total_seconds()),
                "resume_attempts": mission_task.resume_attempts,
            }
            runtime_blob = dict(runtime.blob or {})
            runtime_blob["mission_reaper"] = checkpoint["last_reaper"]

            if mission_task.resume_attempts >= max_attempts:
                reason = f"stale task exceeded max resume attempts ({max_attempts})"
                action: MissionReapAction = "blocked"
                mission_task.status = "blocked"
                mission_task.checkpoint_json = {**checkpoint, "blocked_reason": reason}
                runtime.status = "paused"
                runtime.blob = {**runtime_blob, "blocked_reason": reason}
                mission.status = "paused"
                mission.blocked_reason = reason
            else:
                reason = "stale runtime requeued for mission resume worker"
                action = "requeued"
                mission_task.status = "queued"
                mission_task.checkpoint_json = checkpoint
                runtime.status = "queued"
                runtime.blob = runtime_blob
                if mission.status in {"planned", "paused"}:
                    mission.status = "running"
                    mission.started_at = mission.started_at or now

            mission_task.updated_at = now
            runtime.last_updated = now
            mission.updated_at = now
            result = MissionReapResult(
                mission_id=mission.mission_id,
                task_id=mission_task.task_id,
                action=action,
                previous_runtime_status=previous_status,
                resume_attempts=mission_task.resume_attempts,
                reason=reason,
            )
            results.append(result)
            mission_reaper_actions_total.labels(
                tenant_id=tenant_id,
                outcome=action,
            ).inc()
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="mission.task.reaped",
                    payload=result.model_dump(mode="json"),
                    task_ref=mission_task.task_id,
                ),
            )
    return results


def _stale_mission_tasks_stmt(*, tenant_id: str, stale_before: datetime, limit: int) -> Any:
    return (
        select(MissionRow, MissionTaskRow, RuntimeStateRow)
        .join(
            MissionTaskRow,
            (MissionTaskRow.mission_id == MissionRow.mission_id)
            & (MissionTaskRow.tenant_id == MissionRow.tenant_id),
        )
        .join(
            RuntimeStateRow,
            (RuntimeStateRow.task_ref == MissionTaskRow.task_id)
            & (RuntimeStateRow.tenant_id == MissionTaskRow.tenant_id),
        )
        .where(
            MissionRow.tenant_id == tenant_id,
            MissionTaskRow.tenant_id == tenant_id,
            RuntimeStateRow.tenant_id == tenant_id,
            MissionRow.status.in_(("planned", "running", "paused")),
            MissionTaskRow.status.in_(("queued", "running")),
            RuntimeStateRow.status.in_(("queued", "running")),
            RuntimeStateRow.last_updated < stale_before,
        )
        .order_by(RuntimeStateRow.last_updated)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


__all__ = ["MissionReapResult", "reap_stale_mission_tasks"]
