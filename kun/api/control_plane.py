"""KUN V6 Control Plane API.

These endpoints expose the strict V6 runtime surface without replacing the old
Mission APIs in one jump.  Qi, Nuo, supervisors, and UI surfaces should use this
router when they need mission state, ready work, runner results, gates, or
progress reports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane import (
    AcceptanceReview,
    CapabilityProfile,
    CapabilityPromotion,
    CapabilityRollback,
    CollaborationResponse,
    CollaborationTicket,
    ControlPlaneProgressReport,
    ControlPlaneRecoveryBundle,
    DaemonServiceClaim,
    DaemonServiceConfig,
    DaemonServiceState,
    DaemonServiceStopRequest,
    ExecutionContract,
    ExternalBehaviorProductionizationRecord,
    ExternalBehaviorSignal,
    FileDaemonServiceStateStore,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    MissionDashboardCard,
    ProductizationAuditReport,
    TaskCockpitView,
    TaskPlan,
    UserProgressSummary,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    audit_control_plane_productization,
    build_capability_candidates_from_signals,
    build_dashboard_card,
    build_recovery_bundle,
    build_task_cockpit_view,
    build_user_progress_summary,
    distill_external_behavior_signals,
    productionize_external_behavior_capabilities,
)

router = APIRouter(prefix="/api/control-plane/v6", tags=["control-plane-v6"])


class SubmitMissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission: Mission
    task_plan: TaskPlan
    execution_contract: ExecutionContract
    working_context: WorkingContext
    work_items: list[WorkItem]
    actor: str = "kun"


class ApplyRunResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: WorkItemResult


class AcceptanceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: AcceptanceReview
    actor: str = "kun"


class SubmitCollaborationTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: CollaborationTicket
    actor: str = "kun"


class CollaborationResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: CollaborationResponse
    actor: str = "kun"


class ExternalBehaviorDistillationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: dict[str, str]


class ExternalBehaviorProductionizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    signals: list[ExternalBehaviorSignal]
    dogfood_validation_refs: list[str]
    regression_refs: list[str]
    supervisor_review_ref: str
    actor: str = "qi"


class ApplyCapabilityPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion: CapabilityPromotion
    actor: str = "qi"


class ApplyCapabilityRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rollback: CapabilityRollback
    actor: str = "qi"


class DaemonServiceStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DaemonServiceState


class DaemonServiceStartClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daemon_id: str = "kun-control-plane-daemon"
    config: DaemonServiceConfig = Field(default_factory=DaemonServiceConfig)
    process_id: int | None = None


class DaemonServiceStartClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: DaemonServiceClaim
    status: DaemonServiceStatusResponse


class DaemonServiceStopRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daemon_id: str = "kun-control-plane-daemon"
    requested_by: str = "kun"
    reason: str = "stop_requested"


class DaemonServiceStopRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: DaemonServiceStopRequest
    pending: bool
    text: str


class DaemonServiceStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DaemonServiceState | None = None
    healthy: bool
    stale: bool = False
    text: str


def get_v6_control_plane(request: Request) -> InMemoryControlPlane:
    """Return the installed V6 control plane runtime for this app."""

    runtime = getattr(request.app.state, "v6_control_plane", None)
    if runtime is None:
        runtime = InMemoryControlPlane()
        request.app.state.v6_control_plane = runtime
    if not isinstance(runtime, InMemoryControlPlane):
        raise HTTPException(status_code=503, detail="invalid v6 control plane runtime")
    return runtime


def get_v6_daemon_service_state_store(
    request: Request,
) -> FileDaemonServiceStateStore | None:
    """Return optional persistent daemon service-state store for this app."""

    store = getattr(request.app.state, "v6_daemon_service_state_store", None)
    if store is None:
        return None
    if isinstance(store, FileDaemonServiceStateStore):
        return store
    if isinstance(store, str | Path):
        return FileDaemonServiceStateStore(store)
    raise HTTPException(status_code=503, detail="invalid v6 daemon service state store")


def get_v6_daemon_service_state(request: Request) -> DaemonServiceState | None:
    """Return optional daemon heartbeat state used by the task cockpit."""

    state = getattr(request.app.state, "v6_daemon_service_state", None)
    if state is None:
        store = get_v6_daemon_service_state_store(request)
        return store.load() if store is not None else None
    if isinstance(state, DaemonServiceState):
        return state
    if isinstance(state, dict):
        return DaemonServiceState.model_validate(state)
    raise HTTPException(status_code=503, detail="invalid v6 daemon service state")


def refresh_v6_collaboration_sla(
    runtime: InMemoryControlPlane,
    mission_id: str,
) -> None:
    """Emit due collaboration reminders before user-facing status reads."""

    runtime.emit_collaboration_sla_reminders(mission_id)


def _daemon_status_response(
    state: DaemonServiceState | None,
    *,
    now: datetime | None = None,
) -> DaemonServiceStatusResponse:
    if state is None:
        return DaemonServiceStatusResponse(
            healthy=False,
            text="后台监督服务还没有写入状态；驾驶舱只能读取任务状态，不能确认无人值守健康度。",
        )
    observed_at = now or datetime.now(UTC)
    stale = state.is_stale(now=observed_at, stale_after=timedelta(minutes=30))
    if stale:
        return DaemonServiceStatusResponse(
            state=state,
            healthy=False,
            stale=True,
            text="后台监督心跳已过期；需要先恢复 daemon，再继续无人值守执行。",
        )
    if state.status == "unhealthy":
        return DaemonServiceStatusResponse(
            state=state,
            healthy=False,
            text="后台监督异常；需要按恢复路径重启或修复。",
        )
    if state.status == "stopped":
        healthy = state.stopped_reason == "idle"
        return DaemonServiceStatusResponse(
            state=state,
            healthy=healthy,
            text="后台监督已正常空闲停止。" if healthy else "后台监督已停止，需要确认是否重启。",
        )
    return DaemonServiceStatusResponse(
        state=state,
        healthy=True,
        text="后台监督服务心跳正常。",
    )


@router.post("/missions", response_model=Mission)
async def submit_mission(request: Request, payload: SubmitMissionRequest) -> Mission:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.submit_mission(
            mission=payload.mission,
            task_plan=payload.task_plan,
            execution_contract=payload.execution_contract,
            working_context=payload.working_context,
            work_items=payload.work_items,
            actor=payload.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/missions", response_model=list[Mission])
async def list_missions(request: Request) -> list[Mission]:
    """List Control Plane missions for cockpit mission selection."""

    runtime = get_v6_control_plane(request)
    return sorted(
        runtime.missions.values(),
        key=lambda mission: mission.mission_id,
        reverse=True,
    )


@router.get("/missions/{mission_id}", response_model=Mission)
async def get_mission(request: Request, mission_id: str) -> Mission:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.missions[mission_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mission not found") from exc


@router.get("/missions/{mission_id}/progress", response_model=ControlPlaneProgressReport)
async def get_progress(request: Request, mission_id: str) -> ControlPlaneProgressReport:
    runtime = get_v6_control_plane(request)
    try:
        refresh_v6_collaboration_sla(runtime, mission_id)
        return runtime.progress_report(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/missions/{mission_id}/progress/user", response_model=UserProgressSummary)
async def get_user_progress(request: Request, mission_id: str) -> UserProgressSummary:
    runtime = get_v6_control_plane(request)
    try:
        refresh_v6_collaboration_sla(runtime, mission_id)
        return build_user_progress_summary(runtime.progress_report(mission_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/missions/{mission_id}/dashboard", response_model=MissionDashboardCard)
async def get_dashboard(request: Request, mission_id: str) -> MissionDashboardCard:
    runtime = get_v6_control_plane(request)
    try:
        refresh_v6_collaboration_sla(runtime, mission_id)
        return build_dashboard_card(runtime.progress_report(mission_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/missions/{mission_id}/cockpit", response_model=TaskCockpitView)
async def get_task_cockpit(request: Request, mission_id: str) -> TaskCockpitView:
    runtime = get_v6_control_plane(request)
    try:
        refresh_v6_collaboration_sla(runtime, mission_id)
        return build_task_cockpit_view(
            runtime,
            mission_id,
            daemon_service_state=get_v6_daemon_service_state(request),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/daemon-service/status", response_model=DaemonServiceStatusResponse)
async def get_daemon_service_status(request: Request) -> DaemonServiceStatusResponse:
    return _daemon_status_response(get_v6_daemon_service_state(request))


@router.put("/daemon-service/status", response_model=DaemonServiceStatusResponse)
async def put_daemon_service_status(
    request: Request,
    payload: DaemonServiceStateRequest,
) -> DaemonServiceStatusResponse:
    request.app.state.v6_daemon_service_state = payload.state
    store = get_v6_daemon_service_state_store(request)
    if store is not None:
        store.save(payload.state)
    return _daemon_status_response(payload.state)


@router.post("/daemon-service/start-claim", response_model=DaemonServiceStartClaimResponse)
async def claim_daemon_service_start(
    request: Request,
    payload: DaemonServiceStartClaimRequest,
) -> DaemonServiceStartClaimResponse:
    store = get_v6_daemon_service_state_store(request)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="daemon service state store is required for durable start claim",
        )
    claim = store.claim_start(
        daemon_id=payload.daemon_id,
        config=payload.config,
        process_id=payload.process_id,
    )
    request.app.state.v6_daemon_service_state = claim.state
    return DaemonServiceStartClaimResponse(
        claim=claim,
        status=_daemon_status_response(claim.state),
    )


@router.post("/daemon-service/stop-request", response_model=DaemonServiceStopRequestResponse)
async def request_daemon_service_stop(
    request: Request,
    payload: DaemonServiceStopRequestPayload,
) -> DaemonServiceStopRequestResponse:
    store = get_v6_daemon_service_state_store(request)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="daemon service state store is required for durable stop request",
        )
    stop_request = store.request_stop(
        daemon_id=payload.daemon_id,
        requested_by=payload.requested_by,
        reason=payload.reason,
    )
    return DaemonServiceStopRequestResponse(
        request=stop_request,
        pending=store.stop_requested(daemon_id=payload.daemon_id),
        text="后台监督停止请求已写入，daemon 下次心跳会安全收尾。",
    )


@router.get(
    "/missions/{mission_id}/recovery-bundle",
    response_model=ControlPlaneRecoveryBundle,
)
async def get_recovery_bundle(
    request: Request,
    mission_id: str,
) -> ControlPlaneRecoveryBundle:
    runtime = get_v6_control_plane(request)
    try:
        return build_recovery_bundle(runtime, mission_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/missions/{mission_id}/productization-audit",
    response_model=ProductizationAuditReport,
)
async def get_productization_audit(
    request: Request,
    mission_id: str,
) -> ProductizationAuditReport:
    runtime = get_v6_control_plane(request)
    try:
        return audit_control_plane_productization(runtime, mission_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/missions/{mission_id}/ready-work-item", response_model=WorkItem | None)
async def get_ready_work_item(request: Request, mission_id: str) -> WorkItem | None:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.next_ready_work_item(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/missions/{mission_id}/collaboration-tickets",
    response_model=list[CollaborationTicket],
)
async def list_collaboration_tickets(
    request: Request,
    mission_id: str,
) -> list[CollaborationTicket]:
    runtime = get_v6_control_plane(request)
    try:
        refresh_v6_collaboration_sla(runtime, mission_id)
        return runtime.list_collaboration_tickets(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/collaboration-tickets", response_model=CollaborationTicket)
async def submit_collaboration_ticket(
    request: Request,
    payload: SubmitCollaborationTicketRequest,
) -> CollaborationTicket:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.record_collaboration_ticket(payload.ticket, actor=payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/collaboration-tickets/{ticket_id}", response_model=CollaborationTicket)
async def get_collaboration_ticket(request: Request, ticket_id: str) -> CollaborationTicket:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.get_collaboration_ticket(ticket_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/collaboration-tickets/{ticket_id}/response", response_model=CollaborationTicket)
async def record_collaboration_response(
    request: Request,
    ticket_id: str,
    payload: CollaborationResponseRequest,
) -> CollaborationTicket:
    runtime = get_v6_control_plane(request)
    if payload.response.ticket_id != ticket_id:
        raise HTTPException(status_code=409, detail="response ticket_id does not match path")
    try:
        return runtime.record_collaboration_response(payload.response, actor=payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/result", response_model=dict[str, Any])
async def apply_run_result(
    request: Request,
    run_id: str,
    payload: ApplyRunResultRequest,
) -> dict[str, Any]:
    runtime = get_v6_control_plane(request)
    try:
        run = runtime.apply_work_item_result(run_id=run_id, result=payload.result)
        work_item = runtime.work_items[run.work_item_id]
        return {
            "run": run.model_dump(mode="json"),
            "work_item": work_item.model_dump(mode="json"),
            "mission": runtime.missions[work_item.mission_id].model_dump(mode="json"),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/gates", response_model=Mission)
async def apply_gate(request: Request, gate: GateEvaluation) -> Mission:
    runtime = get_v6_control_plane(request)
    try:
        return runtime.apply_gate(gate)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/acceptance", response_model=ControlPlaneProgressReport)
async def record_acceptance(
    request: Request,
    payload: AcceptanceReviewRequest,
) -> ControlPlaneProgressReport:
    runtime = get_v6_control_plane(request)
    try:
        runtime.record_acceptance_review(payload.review, actor=payload.actor)
        return runtime.progress_report(payload.review.mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/external-behavior/signals", response_model=dict[str, Any])
async def distill_external_behavior(
    payload: ExternalBehaviorDistillationRequest,
) -> dict[str, Any]:
    """Distill OpenClaw/Hermes source behavior into KUN-native Qi candidates."""

    signals = distill_external_behavior_signals(payload.sources)
    candidates = build_capability_candidates_from_signals(signals)
    return {
        "signals": [signal.model_dump(mode="json") for signal in signals],
        "capability_candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }


@router.post(
    "/external-behavior/productionize",
    response_model=ExternalBehaviorProductionizationRecord,
)
async def productionize_external_behavior(
    request: Request,
    payload: ExternalBehaviorProductionizationRequest,
) -> ExternalBehaviorProductionizationRecord:
    """Promote validated OpenClaw/Hermes behavior samples into KUN defaults."""

    runtime = get_v6_control_plane(request)
    try:
        return productionize_external_behavior_capabilities(
            runtime,
            payload.mission_id,
            payload.signals,
            dogfood_validation_refs=payload.dogfood_validation_refs,
            regression_refs=payload.regression_refs,
            supervisor_review_ref=payload.supervisor_review_ref,
            actor=payload.actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/capability-promotions", response_model=dict[str, Any])
async def apply_capability_promotion(
    request: Request,
    payload: ApplyCapabilityPromotionRequest,
) -> dict[str, Any]:
    runtime = get_v6_control_plane(request)
    profile = runtime.apply_capability_promotion(payload.promotion, actor=payload.actor)
    return {
        "promotion_id": payload.promotion.promotion_id,
        "decision": payload.promotion.decision,
        "target_stage": payload.promotion.target_stage,
        "capability_profile": profile.model_dump(mode="json") if profile else None,
        "default_runtime_enabled": bool(
            profile is not None
            and profile.promotion_stage == "production"
            and profile.runtime_enabled
        ),
    }


@router.post("/capability-rollbacks", response_model=dict[str, Any])
async def apply_capability_rollback(
    request: Request,
    payload: ApplyCapabilityRollbackRequest,
) -> dict[str, Any]:
    runtime = get_v6_control_plane(request)
    try:
        profile = runtime.apply_capability_rollback(payload.rollback, actor=payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "rollback_id": payload.rollback.rollback_id,
        "capability_id": payload.rollback.capability_id,
        "capability_profile": profile.model_dump(mode="json"),
        "default_runtime_enabled": False,
    }


@router.get("/runtime-capabilities/default", response_model=list[CapabilityProfile])
async def list_default_runtime_capabilities(request: Request) -> list[CapabilityProfile]:
    runtime = get_v6_control_plane(request)
    return runtime.list_default_runtime_capabilities()
