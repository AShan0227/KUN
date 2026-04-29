"""Mission resume worker.

The worker is honest by design: it only claims "dispatched" when an injected
runner actually accepts the resume request. Without a runner it emits an explicit
skipped result instead of pretending long-horizon execution is complete.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kun.core.db import session_scope
from kun.core.events import emit
from kun.datamodel.events import Event, EventKind
from kun.datamodel.mission import ResumeRequest
from kun.engineering.mission_control import request_resumable_tasks

MissionResumeStatus = Literal["dispatched", "skipped", "failed"]
MissionResumeRunner = Callable[[ResumeRequest], Awaitable[None]]


class MissionResumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    status: MissionResumeStatus
    reason: str = ""


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
                await self.runner(request)
            except Exception as e:
                result = MissionResumeResult(
                    mission_id=request.mission_id,
                    task_id=request.task_id,
                    status="failed",
                    reason=f"{type(e).__name__}: {e}",
                )
            else:
                result = MissionResumeResult(
                    mission_id=request.mission_id,
                    task_id=request.task_id,
                    status="dispatched",
                    reason="runner accepted resume request",
                )
            await _emit_resume_result(tenant_id, result)
            results.append(result)
        return results


async def _emit_resume_result(tenant_id: str, result: MissionResumeResult) -> None:
    event_type: EventKind
    if result.status == "dispatched":
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


__all__ = [
    "MissionResumeResult",
    "MissionResumeRunner",
    "MissionResumeStatus",
    "MissionResumeWorker",
]
