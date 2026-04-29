"""Mission API — long-horizon task control surface."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant
from kun.datamodel.mission import MissionCreate, MissionMilestone, MissionSnapshot, ResumeRequest
from kun.engineering import mission_control
from kun.engineering.mission_worker import MissionResumeResult, MissionResumeWorker

router = APIRouter(prefix="/api/missions", tags=["missions"])


class AttachTaskRequest(BaseModel):
    task_id: str
    role: str = "primary"
    sequence_no: int = Field(default=0, ge=0)
    checkpoint: dict[str, Any] = Field(default_factory=dict)


@router.post("", response_model=MissionSnapshot)
async def create_mission(payload: MissionCreate) -> MissionSnapshot:
    tenant = current_tenant()
    return await mission_control.create_mission(
        payload,
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
    )


@router.get("", response_model=list[MissionSnapshot])
async def list_missions(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MissionSnapshot]:
    tenant = current_tenant()
    return await mission_control.list_missions(
        tenant_id=tenant.tenant_id,
        status=status,
        limit=limit,
    )


@router.post("/resume-requests", response_model=list[ResumeRequest])
async def request_resume(
    limit: int = Query(default=20, ge=1, le=100),
    max_attempts: int = Query(default=3, ge=1, le=20),
) -> list[ResumeRequest]:
    tenant = current_tenant()
    return await mission_control.request_resumable_tasks(
        tenant_id=tenant.tenant_id,
        limit=limit,
        max_attempts=max_attempts,
    )


@router.post("/resume-worker/run-once", response_model=list[MissionResumeResult])
async def run_resume_worker_once(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    max_attempts: int = Query(default=3, ge=1, le=20),
) -> list[MissionResumeResult]:
    tenant = current_tenant()
    maybe_worker = getattr(request.app.state, "mission_resume_worker", None)
    if maybe_worker is None:
        raise HTTPException(status_code=503, detail="mission resume worker is not installed")
    worker = cast(MissionResumeWorker, maybe_worker)
    return await worker.run_once(
        tenant_id=tenant.tenant_id,
        limit=limit,
        max_attempts=max_attempts,
    )


@router.get("/{mission_id}", response_model=MissionSnapshot)
async def get_mission(mission_id: str) -> MissionSnapshot:
    tenant = current_tenant()
    mission = await mission_control.get_mission(
        tenant_id=tenant.tenant_id,
        mission_id=mission_id,
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return mission


@router.post("/{mission_id}/tasks", response_model=MissionSnapshot)
async def attach_task(mission_id: str, payload: AttachTaskRequest) -> MissionSnapshot:
    tenant = current_tenant()
    try:
        return await mission_control.attach_task_to_mission(
            tenant_id=tenant.tenant_id,
            mission_id=mission_id,
            task_id=payload.task_id,
            role=payload.role,
            sequence_no=payload.sequence_no,
            checkpoint=payload.checkpoint,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{mission_id}/milestones", response_model=MissionSnapshot)
async def record_milestone(
    mission_id: str,
    payload: MissionMilestone,
) -> MissionSnapshot:
    tenant = current_tenant()
    try:
        return await mission_control.record_milestone(
            payload,
            tenant_id=tenant.tenant_id,
            mission_id=mission_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{mission_id}/refresh", response_model=MissionSnapshot)
async def refresh_mission(mission_id: str) -> MissionSnapshot:
    tenant = current_tenant()
    try:
        return await mission_control.refresh_mission_task_statuses(
            tenant_id=tenant.tenant_id,
            mission_id=mission_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


__all__ = ["router"]
