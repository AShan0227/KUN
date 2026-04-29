"""Mission API — long-horizon task control surface."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant
from kun.datamodel.mission import (
    MissionBlockedResult,
    MissionCreate,
    MissionExecutionSummary,
    MissionLedgerAudit,
    MissionMilestone,
    MissionReaperResult,
    MissionReview,
    MissionSnapshot,
    MissionTimeline,
    ResumeRequest,
)
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


@router.post("/reaper/run-once", response_model=list[MissionReaperResult])
async def run_reaper_once(
    queued_stale_after_sec: int = Query(default=900, ge=60, le=86_400),
    running_stale_after_sec: int = Query(default=3600, ge=60, le=604_800),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MissionReaperResult]:
    tenant = current_tenant()
    return await mission_control.reap_stale_mission_tasks(
        tenant_id=tenant.tenant_id,
        queued_stale_after_sec=queued_stale_after_sec,
        running_stale_after_sec=running_stale_after_sec,
        limit=limit,
    )


@router.post("/blocked/run-once", response_model=list[MissionBlockedResult])
async def block_exhausted_once(
    max_attempts: int = Query(default=3, ge=1, le=20),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MissionBlockedResult]:
    tenant = current_tenant()
    return await mission_control.block_exhausted_mission_tasks(
        tenant_id=tenant.tenant_id,
        max_attempts=max_attempts,
        limit=limit,
    )


@router.post("/review/run-once", response_model=list[MissionReview])
async def run_review_once(
    limit: int = Query(default=20, ge=1, le=200),
    timeline_limit: int = Query(default=200, ge=1, le=500),
    min_interval_sec: int = Query(default=3600, ge=0, le=604_800),
) -> list[MissionReview]:
    tenant = current_tenant()
    return await mission_control.review_active_missions(
        tenant_id=tenant.tenant_id,
        limit=limit,
        timeline_limit=timeline_limit,
        min_interval_sec=min_interval_sec,
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


@router.get("/{mission_id}/summary", response_model=MissionExecutionSummary)
async def get_mission_summary(mission_id: str) -> MissionExecutionSummary:
    tenant = current_tenant()
    summary = await mission_control.summarize_mission(
        tenant_id=tenant.tenant_id,
        mission_id=mission_id,
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return summary


@router.get("/{mission_id}/timeline", response_model=MissionTimeline)
async def get_mission_timeline(
    mission_id: str,
    limit: int = Query(default=200, ge=1, le=500),
) -> MissionTimeline:
    tenant = current_tenant()
    timeline = await mission_control.get_mission_timeline(
        tenant_id=tenant.tenant_id,
        mission_id=mission_id,
        limit=limit,
    )
    if timeline is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return timeline


@router.post("/{mission_id}/review", response_model=MissionReview)
async def review_mission(
    mission_id: str,
    timeline_limit: int = Query(default=200, ge=1, le=500),
) -> MissionReview:
    tenant = current_tenant()
    review = await mission_control.review_mission(
        tenant_id=tenant.tenant_id,
        mission_id=mission_id,
        timeline_limit=timeline_limit,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return review


@router.get("/{mission_id}/audit", response_model=MissionLedgerAudit)
async def audit_mission(
    mission_id: str,
    timeline_limit: int = Query(default=500, ge=1, le=1000),
) -> MissionLedgerAudit:
    tenant = current_tenant()
    audit = await mission_control.audit_mission_ledger(
        tenant_id=tenant.tenant_id,
        mission_id=mission_id,
        timeline_limit=timeline_limit,
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return audit


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
