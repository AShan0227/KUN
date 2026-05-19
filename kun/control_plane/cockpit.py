"""User-facing task cockpit for KUN V6 Control Plane.

The cockpit is intentionally richer than the compact progress summary: it is
the API/UI contract a normal user should read instead of terminal logs.  It
answers what is happening, what is blocked, what KUN will do next, whether a
human needs to answer, where deliverables are, and whether the latest quality
gate is trustworthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.daemon import DaemonServiceState
from kun.control_plane.progress import (
    QualityGateStatus,
    UserProgressSummary,
    build_user_progress_summary,
)
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.v6 import (
    AcceptanceDecision,
    ArtifactManifest,
    ArtifactRecord,
    CollaborationTicket,
    FailureCategory,
    GateEvaluation,
    MissionStatus,
    TaskType,
    WorkItem,
    WorkItemStatus,
)

CockpitTone = Literal["working", "waiting", "blocked", "ready", "done"]
WorkItemLane = Literal["ready", "running", "waiting", "blocked", "queued", "done"]


class TaskCockpitProgress(BaseModel):
    """Readable progress numbers for dashboard tiles."""

    model_config = ConfigDict(extra="forbid")

    total: int
    done: int = 0
    running: int = 0
    queued: int = 0
    ready: int = 0
    waiting: int = 0
    blocked: int = 0
    failed: int = 0
    percent_complete: int = Field(default=0, ge=0, le=100)


class TaskCockpitPlanSummary(BaseModel):
    """The current task plan slice the cockpit should keep aligned to."""

    model_config = ConfigDict(extra="forbid")

    plan_ref: str | None = None
    version: str | None = None
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    human_confirmation_points: list[str] = Field(default_factory=list)


class TaskCockpitWorkItemCard(BaseModel):
    """One work item rendered for a user-facing dashboard."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str
    lane: WorkItemLane
    title: str
    owner: str
    status: WorkItemStatus
    status_text: str
    expected_output: str = ""
    artifact_manifest_ref: str | None = None
    needs_attention: bool = False


class TaskCockpitQualityGate(BaseModel):
    """Latest quality gate, translated into north-star terms."""

    model_config = ConfigDict(extra="forbid")

    gate_ref: str | None = None
    status: QualityGateStatus = "unknown"
    verdict: str = "unknown"
    stage: str = ""
    next_action: str = ""
    text: str
    result_quality: float | None = None
    evidence_quality: float | None = None
    failure_category: FailureCategory | None = None
    root_cause: str = ""
    responsibility_scope: str = ""
    hard_gate_failures: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)


class TaskCockpitTicketCard(BaseModel):
    """Human/external ticket surface for the cockpit."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    role_needed: str
    type: str
    status: str
    why_needed: str
    recommended_option: str = ""
    deadline: datetime
    risk_if_skipped: str
    output_contract: str


class TaskCockpitCollaboration(BaseModel):
    """Human-in-the-loop state with a clear next action."""

    model_config = ConfigDict(extra="forbid")

    human_needed: bool
    open_ticket_count: int = 0
    next_human_action: str = ""
    tickets: list[TaskCockpitTicketCard] = Field(default_factory=list)


class TaskCockpitDeliverable(BaseModel):
    """A delivery artifact a user can inspect."""

    model_config = ConfigDict(extra="forbid")

    artifact_ref: str
    kind: str
    path_or_uri: str
    access_status: str
    supports: list[str] = Field(default_factory=list)
    source_quality: str = ""


class TaskCockpitArtifactSummary(BaseModel):
    """Where outputs and evidence live."""

    model_config = ConfigDict(extra="forbid")

    manifest_count: int = 0
    delivery_ready: bool = False
    latest_delivery_manifest_ref: str | None = None
    delivery_manifest_refs: list[str] = Field(default_factory=list)
    deliverables: list[TaskCockpitDeliverable] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)


class TaskCockpitDaemonHealth(BaseModel):
    """Background daemon visibility for non-terminal use."""

    model_config = ConfigDict(extra="forbid")

    healthy: bool
    text: str
    service_status: str = "unknown"
    last_heartbeat_at: datetime | None = None
    next_wakeup_at: datetime | None = None
    stopped_reason: str | None = None
    stale: bool = False
    latest_progress_artifact_ref: str | None = None
    progress_artifact_refs: list[str] = Field(default_factory=list)


class TaskCockpitAcceptance(BaseModel):
    """Latest acceptance state, if a deliverable has been reviewed."""

    model_config = ConfigDict(extra="forbid")

    acceptance_ref: str
    decision: AcceptanceDecision
    reviewer: str
    satisfaction: float
    reason: str = ""
    requested_changes: list[str] = Field(default_factory=list)


class TaskCockpitView(BaseModel):
    """Full task cockpit view for API and frontend surfaces."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    objective: str
    owner: str
    task_type: TaskType
    status: MissionStatus
    tone: CockpitTone
    headline: str
    status_text: str
    blocking_reason: str = ""
    next_step: str
    safe_to_continue: bool
    plan: TaskCockpitPlanSummary
    progress: TaskCockpitProgress
    quality_gate: TaskCockpitQualityGate
    collaboration: TaskCockpitCollaboration
    artifacts: TaskCockpitArtifactSummary
    daemon: TaskCockpitDaemonHealth
    work_items: list[TaskCockpitWorkItemCard] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recovery_actions: list[str] = Field(default_factory=list)
    acceptance: TaskCockpitAcceptance | None = None
    technical_refs: list[str] = Field(default_factory=list)


def build_task_cockpit_view(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    daemon_service_state: DaemonServiceState | None = None,
    now: datetime | None = None,
) -> TaskCockpitView:
    """Build the user-facing cockpit from durable Control Plane state."""

    mission = control_plane.missions[mission_id]
    observed_at = now or datetime.now(UTC)
    progress = control_plane.progress_report(mission_id)
    user_summary = build_user_progress_summary(progress)
    plan = _current_plan(control_plane, mission_id=mission_id, version=mission.current_plan_version)
    gate = _latest_gate(control_plane, mission_id)
    work_items = [item for item in control_plane.work_items.values() if item.mission_id == mission_id]
    tickets = _open_tickets(control_plane, mission_id)
    artifacts = _artifact_summary(control_plane, mission_id)
    daemon = _daemon_health(
        control_plane,
        mission_id,
        service_state=daemon_service_state,
        now=observed_at,
    )
    acceptance = _acceptance(control_plane, mission.acceptance_ref)
    risks = _risks(plan=plan, tickets=tickets, gate=gate, user_summary=user_summary)
    return TaskCockpitView(
        mission_id=mission_id,
        objective=mission.objective,
        owner=mission.owner,
        task_type=mission.task_type,
        status=mission.status,
        tone=user_summary.tone,
        headline=_headline(user_summary=user_summary, artifacts=artifacts),
        status_text=user_summary.current_status,
        blocking_reason=user_summary.blocking_reason,
        next_step=user_summary.next_step,
        safe_to_continue=user_summary.safe_to_continue,
        plan=_plan_summary(plan=plan, objective=mission.objective),
        progress=_progress(work_items, ready_ids=set(progress.next_ready_work_item_ids)),
        quality_gate=_quality_gate(gate=gate, summary=user_summary),
        collaboration=_collaboration(tickets=tickets, human_needed=user_summary.human_needed),
        artifacts=artifacts,
        daemon=daemon,
        work_items=_work_item_cards(work_items, ready_ids=set(progress.next_ready_work_item_ids)),
        risks=risks,
        recovery_actions=_recovery_actions(summary=user_summary, gate=gate, work_items=work_items),
        acceptance=acceptance,
        technical_refs=_technical_refs(
            mission_plan_ref=plan.plan_id if plan is not None else None,
            execution_contract_ref=mission.execution_contract_ref,
            working_context_ref=mission.working_context_ref,
            gate_ref=(user_summary.ready_work_item_ids and progress.latest_gate_ref) or progress.latest_gate_ref,
            manifest_refs=mission.artifact_manifest_refs,
            ticket_refs=[ticket.ticket_id for ticket in tickets],
            daemon_ref=daemon.latest_progress_artifact_ref,
            acceptance_ref=mission.acceptance_ref,
        ),
    )


def _current_plan(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    version: str | None,
):
    plans = [
        plan for plan in control_plane.task_plans.values() if plan.mission_id == mission_id
    ]
    if version is not None:
        for plan in plans:
            if plan.version == version:
                return plan
    return max(plans, key=lambda item: item.version, default=None)


def _latest_gate(control_plane: InMemoryControlPlane, mission_id: str) -> GateEvaluation | None:
    return control_plane._latest_gate(mission_id)


def _open_tickets(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[CollaborationTicket]:
    return sorted(
        (
            ticket
            for ticket in control_plane.collaboration_tickets.values()
            if ticket.mission_id == mission_id and ticket.status in {"open", "waiting", "escalated"}
        ),
        key=lambda ticket: (ticket.deadline, ticket.ticket_id),
    )


def _plan_summary(plan, *, objective: str) -> TaskCockpitPlanSummary:
    if plan is None:
        return TaskCockpitPlanSummary(objective=objective)
    return TaskCockpitPlanSummary(
        plan_ref=plan.plan_id,
        version=plan.version,
        objective=plan.objective,
        acceptance_criteria=list(plan.acceptance_criteria),
        constraints=list(plan.constraints),
        risks=list(plan.risk_register),
        open_questions=list(plan.info_gaps or plan.unknowns),
        human_confirmation_points=list(plan.human_confirmation_points),
    )


def _progress(
    work_items: list[WorkItem],
    *,
    ready_ids: set[str],
) -> TaskCockpitProgress:
    total = len(work_items)
    done = sum(1 for item in work_items if item.status in {"done", "partial"})
    running = sum(1 for item in work_items if item.status == "running")
    queued = sum(1 for item in work_items if item.status == "queued")
    waiting = sum(1 for item in work_items if item.status in {"waiting_human", "waiting_external"})
    blocked = sum(
        1
        for item in work_items
        if item.status in {"blocked", "repairing", "rolling_back", "changing_plan", "retrying"}
    )
    failed = sum(1 for item in work_items if item.status == "failed")
    percent = round((done / total) * 100) if total else 0
    return TaskCockpitProgress(
        total=total,
        done=done,
        running=running,
        queued=queued,
        ready=len(ready_ids),
        waiting=waiting,
        blocked=blocked,
        failed=failed,
        percent_complete=percent,
    )


def _work_item_cards(
    work_items: list[WorkItem],
    *,
    ready_ids: set[str],
) -> list[TaskCockpitWorkItemCard]:
    cards = [_work_item_card(item, ready=item.work_item_id in ready_ids) for item in work_items]
    return sorted(cards, key=lambda card: (_lane_order(card.lane), card.work_item_id))


def _work_item_card(item: WorkItem, *, ready: bool) -> TaskCockpitWorkItemCard:
    lane = _work_item_lane(item.status, ready=ready)
    return TaskCockpitWorkItemCard(
        work_item_id=item.work_item_id,
        lane=lane,
        title=_work_item_title(item),
        owner=item.owner,
        status=item.status,
        status_text=_work_item_status_text(item.status, ready=ready),
        expected_output=item.expected_output,
        artifact_manifest_ref=item.artifact_manifest_ref,
        needs_attention=lane in {"waiting", "blocked"},
    )


def _work_item_lane(status: WorkItemStatus, *, ready: bool) -> WorkItemLane:
    if ready:
        return "ready"
    if status == "running":
        return "running"
    if status in {"waiting_human", "waiting_external"}:
        return "waiting"
    if status in {"blocked", "repairing", "rolling_back", "changing_plan", "retrying", "failed"}:
        return "blocked"
    if status in {"done", "partial", "cancelled"}:
        return "done"
    return "queued"


def _lane_order(lane: WorkItemLane) -> int:
    return {
        "running": 0,
        "ready": 1,
        "waiting": 2,
        "blocked": 3,
        "queued": 4,
        "done": 5,
    }[lane]


def _work_item_title(item: WorkItem) -> str:
    if item.expected_output:
        return item.expected_output
    return {
        "execution": "执行工作项",
        "research": "补齐信息和证据",
        "review": "评审结果",
        "test": "运行验证",
        "collaboration": "处理协同",
        "external_worker": "等待外部协作者",
        "repair": "修复阻断",
        "rollback": "回滚风险变更",
        "retest": "同题复测",
        "plan_change": "调整任务方案",
        "merge": "合并结果",
        "governance": "治理和审计",
    }.get(item.type, "推进工作项")


def _work_item_status_text(status: WorkItemStatus, *, ready: bool) -> str:
    if ready:
        return "已准备执行。"
    return {
        "queued": "排队中，等待依赖完成或调度。",
        "running": "正在执行。",
        "waiting_human": "等待人类确认或输入。",
        "waiting_external": "等待外部协作者或外部系统。",
        "blocked": "已阻断，需要先修复。",
        "retrying": "准备重试。",
        "repairing": "正在修复。",
        "rolling_back": "正在回滚。",
        "changing_plan": "正在调整任务方案。",
        "merging": "正在合并多路结果。",
        "done": "已完成。",
        "partial": "部分完成。",
        "failed": "失败，需归因后恢复。",
        "cancelled": "已取消。",
    }[status]


def _quality_gate(
    *,
    gate: GateEvaluation | None,
    summary: UserProgressSummary,
) -> TaskCockpitQualityGate:
    if gate is None:
        return TaskCockpitQualityGate(
            status=summary.quality_gate_status,
            text="还没有质量门禁结论；任务继续执行时会生成可追溯评估。",
        )
    return TaskCockpitQualityGate(
        gate_ref=gate.gate_evaluation_id,
        status=summary.quality_gate_status,
        verdict=gate.north_star_verdict,
        stage=gate.stage,
        next_action=gate.next_action,
        text=_quality_gate_text(gate, summary.quality_gate_status),
        result_quality=gate.result_quality,
        evidence_quality=gate.evidence_quality,
        failure_category=gate.failure_category,
        root_cause=gate.root_cause,
        responsibility_scope=gate.responsibility_scope,
        hard_gate_failures=list(gate.hard_gate_failures),
        evidence_refs=list(gate.evidence_refs),
        test_refs=list(gate.test_refs),
        review_refs=list(gate.review_refs),
    )


def _quality_gate_text(gate: GateEvaluation, status: QualityGateStatus) -> str:
    if status == "pass":
        return "质量门禁已通过，可以进入下一步交付或验收。"
    if status == "invalid":
        return "当前失败属于系统污染、环境或工具阻断，先修复并复测，不算 KUN 能力失败。"
    if status == "needs_repair":
        return "结果质量没有过门禁，需要修复能力、调整方案或复测。"
    if status == "blocked":
        return "任务处于阻断状态，需要先恢复。"
    if gate.north_star_verdict == "partial":
        return "质量门禁只部分通过，需要明确剩余风险后再决定是否交付。"
    return "质量门禁状态未知，需要继续观察或重新评估。"


def _collaboration(
    *,
    tickets: list[CollaborationTicket],
    human_needed: bool,
) -> TaskCockpitCollaboration:
    ticket_cards = [
        TaskCockpitTicketCard(
            ticket_id=ticket.ticket_id,
            role_needed=ticket.role_needed,
            type=ticket.type,
            status=ticket.status,
            why_needed=ticket.why_needed,
            recommended_option=ticket.recommended_option or "",
            deadline=ticket.deadline,
            risk_if_skipped=ticket.risk_if_skipped,
            output_contract=ticket.output_contract,
        )
        for ticket in tickets
    ]
    next_action = ""
    if tickets:
        ticket = tickets[0]
        if ticket.recommended_option:
            next_action = f"等待 {ticket.role_needed} 回复，建议选择：{ticket.recommended_option}。"
        else:
            next_action = f"等待 {ticket.role_needed} 回复：{ticket.output_contract}"
    return TaskCockpitCollaboration(
        human_needed=human_needed,
        open_ticket_count=len(tickets),
        next_human_action=next_action,
        tickets=ticket_cards,
    )


def _artifact_summary(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> TaskCockpitArtifactSummary:
    manifests = [
        manifest
        for manifest in control_plane.artifact_manifests.values()
        if manifest.mission_id == mission_id
    ]
    delivery_manifests = [
        manifest for manifest in manifests if manifest.kind == "delivery" or manifest.supports_delivery
    ]
    latest_delivery = max(delivery_manifests, key=lambda item: item.manifest_id, default=None)
    artifact_refs = _manifest_artifact_refs(delivery_manifests)
    artifacts = [
        control_plane.artifacts[artifact_ref]
        for artifact_ref in artifact_refs
        if artifact_ref in control_plane.artifacts
    ]
    return TaskCockpitArtifactSummary(
        manifest_count=len(manifests),
        delivery_ready=latest_delivery is not None,
        latest_delivery_manifest_ref=latest_delivery.manifest_id if latest_delivery else None,
        delivery_manifest_refs=[manifest.manifest_id for manifest in delivery_manifests],
        deliverables=[_deliverable(artifact) for artifact in artifacts],
        evidence_refs=_unique_ref_list(ref for manifest in manifests for ref in manifest.evidence_refs),
        test_refs=_unique_ref_list(ref for manifest in manifests for ref in manifest.test_refs),
        review_refs=_unique_ref_list(ref for manifest in manifests for ref in manifest.review_refs),
    )


def _manifest_artifact_refs(manifests: list[ArtifactManifest]) -> list[str]:
    refs: list[str] = []
    for manifest in manifests:
        if manifest.primary_artifact_ref:
            refs.append(manifest.primary_artifact_ref)
        refs.extend(manifest.artifact_refs)
    return _unique_ref_list(refs)


def _deliverable(artifact: ArtifactRecord) -> TaskCockpitDeliverable:
    return TaskCockpitDeliverable(
        artifact_ref=artifact.artifact_id,
        kind=artifact.kind,
        path_or_uri=artifact.path_or_uri,
        access_status=artifact.access_status,
        supports=list(artifact.supports),
        source_quality=artifact.source_quality,
    )


def _daemon_health(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    service_state: DaemonServiceState | None,
    now: datetime,
) -> TaskCockpitDaemonHealth:
    refs = sorted(
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if artifact.mission_id == mission_id and "daemon_progress" in artifact.supports
    )
    if service_state is not None:
        stale = service_state.is_stale(now=now, stale_after=timedelta(minutes=30))
        latest_ref = refs[-1] if refs else None
        if stale:
            return TaskCockpitDaemonHealth(
                healthy=False,
                text="后台监督心跳已过期；KUN 应先恢复 daemon，再继续无人值守执行。",
                service_status=service_state.status,
                last_heartbeat_at=service_state.last_heartbeat_at,
                next_wakeup_at=service_state.next_wakeup_at,
                stopped_reason=service_state.stopped_reason,
                stale=True,
                latest_progress_artifact_ref=latest_ref,
                progress_artifact_refs=refs,
            )
        if service_state.status == "unhealthy":
            detail = f"：{service_state.last_error}" if service_state.last_error else ""
            return TaskCockpitDaemonHealth(
                healthy=False,
                text=f"后台监督异常{detail}；需要按恢复路径重启或修复。",
                service_status=service_state.status,
                last_heartbeat_at=service_state.last_heartbeat_at,
                next_wakeup_at=service_state.next_wakeup_at,
                stopped_reason=service_state.stopped_reason,
                latest_progress_artifact_ref=latest_ref,
                progress_artifact_refs=refs,
            )
        if service_state.status == "stopped":
            healthy = service_state.stopped_reason == "idle"
            text = (
                "后台监督已因空闲正常停止，任务状态仍可从持久记录恢复。"
                if healthy
                else "后台监督已停止；如仍有待执行工作项，需要重新启动 daemon。"
            )
            return TaskCockpitDaemonHealth(
                healthy=healthy,
                text=text,
                service_status=service_state.status,
                last_heartbeat_at=service_state.last_heartbeat_at,
                next_wakeup_at=service_state.next_wakeup_at,
                stopped_reason=service_state.stopped_reason,
                latest_progress_artifact_ref=latest_ref,
                progress_artifact_refs=refs,
            )
        return TaskCockpitDaemonHealth(
            healthy=True,
            text="后台监督服务心跳正常，任务可自动醒来、继续执行并从持久状态恢复。",
            service_status=service_state.status,
            last_heartbeat_at=service_state.last_heartbeat_at,
            next_wakeup_at=service_state.next_wakeup_at,
            stopped_reason=service_state.stopped_reason,
            latest_progress_artifact_ref=latest_ref,
            progress_artifact_refs=refs,
        )
    if refs:
        return TaskCockpitDaemonHealth(
            healthy=True,
            text="后台监督已写入最近进度，任务可从持久状态恢复。",
            service_status="progress_artifact_only",
            latest_progress_artifact_ref=refs[-1],
            progress_artifact_refs=refs,
        )
    return TaskCockpitDaemonHealth(
        healthy=False,
        text="还没有后台监督进度记录；任务状态仍可查看，但需要 daemon 写入 heartbeat 后才算完整驾驶舱。",
    )


def _acceptance(
    control_plane: InMemoryControlPlane,
    acceptance_ref: str | None,
) -> TaskCockpitAcceptance | None:
    if acceptance_ref is None or acceptance_ref not in control_plane.acceptance_reviews:
        return None
    review = control_plane.acceptance_reviews[acceptance_ref]
    return TaskCockpitAcceptance(
        acceptance_ref=review.acceptance_id,
        decision=review.decision,
        reviewer=review.reviewer,
        satisfaction=review.satisfaction,
        reason=review.reason,
        requested_changes=list(review.requested_changes),
    )


def _risks(
    *,
    plan,
    tickets: list[CollaborationTicket],
    gate: GateEvaluation | None,
    user_summary: UserProgressSummary,
) -> list[str]:
    risks: list[str] = []
    if plan is not None:
        risks.extend(plan.risk_register)
    risks.extend(ticket.risk_if_skipped for ticket in tickets)
    if user_summary.blocking_reason:
        risks.append(user_summary.blocking_reason)
    if gate is not None:
        risks.extend(gate.hard_gate_failures)
        if gate.root_cause:
            risks.append(gate.root_cause)
    return _unique_ref_list(risks)


def _recovery_actions(
    *,
    summary: UserProgressSummary,
    gate: GateEvaluation | None,
    work_items: list[WorkItem],
) -> list[str]:
    actions: list[str] = []
    if summary.latest_failure_category in {"environment_failure", "tool_failure"}:
        actions.append("先修复系统污染、工具或环境阻断，再自动复测。")
    if summary.latest_failure_category in {"model_quality_failure", "evidence_failure", "plan_failure"}:
        actions.append("更新任务方案或能力候选后复测，质量不过门禁不能交付。")
    if gate is not None and gate.next_action in {"needs_repair", "needs_rollback", "needs_plan_change"}:
        actions.append(f"按门禁动作执行：{gate.next_action}。")
    if any(item.status in {"blocked", "failed"} for item in work_items):
        actions.append("阻断工作项会进入修复、回滚或重跑路径，并保留账本记录。")
    return _unique_ref_list(actions)


def _headline(
    *,
    user_summary: UserProgressSummary,
    artifacts: TaskCockpitArtifactSummary,
) -> str:
    if user_summary.human_needed:
        return "需要人类确认后继续。"
    if user_summary.quality_gate_status == "invalid":
        return "发现系统污染或环境阻断，正在先修系统再复测。"
    if user_summary.quality_gate_status == "needs_repair":
        return "结果质量还没过门禁，正在修复后复测。"
    if artifacts.delivery_ready and user_summary.tone in {"ready", "done"}:
        return "交付物已准备好验收。"
    if user_summary.tone == "done":
        return "任务已收口。"
    if user_summary.tone == "blocked":
        return "任务被阻断，KUN 正在按恢复路径处理。"
    return "KUN 正在推进任务。"


def _technical_refs(
    *,
    mission_plan_ref: str | None,
    execution_contract_ref: str | None,
    working_context_ref: str | None,
    gate_ref: str | None,
    manifest_refs: list[str],
    ticket_refs: list[str],
    daemon_ref: str | None,
    acceptance_ref: str | None,
) -> list[str]:
    refs = [
        mission_plan_ref,
        execution_contract_ref,
        working_context_ref,
        gate_ref,
        daemon_ref,
        acceptance_ref,
        *manifest_refs,
        *ticket_refs,
    ]
    return _unique_ref_list(ref for ref in refs if ref)


def _unique_ref_list(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "TaskCockpitAcceptance",
    "TaskCockpitArtifactSummary",
    "TaskCockpitCollaboration",
    "TaskCockpitDaemonHealth",
    "TaskCockpitDeliverable",
    "TaskCockpitPlanSummary",
    "TaskCockpitProgress",
    "TaskCockpitQualityGate",
    "TaskCockpitTicketCard",
    "TaskCockpitView",
    "TaskCockpitWorkItemCard",
    "build_task_cockpit_view",
]
