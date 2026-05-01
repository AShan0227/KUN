"""NUO system-level health collector.

Unlike the light `/nuo/health/summary` endpoint, this report is meant for
system diagnosis.  It gathers real runtime rows, event lag, pending approvals,
delivery-status honesty checks, secret/config safety, and WorldGateway handler
health.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from kun.context.maintenance import ContextMaintenanceReport, run_context_maintenance
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.multi_task_scheduler import DEFAULT_LANE_LIMITS, TaskLane
from kun.core.orm import (
    CapabilityCardRow,
    EventRow,
    MissionRow,
    MissionTaskRow,
    PendingActionRow,
    QiProblemSignalRow,
    ResourceCreditRow,
    RuntimeStateRow,
    StateLedgerEntryRow,
    TaskRow,
)
from kun.core.state_ledger import replay_state_ledger_story
from kun.engineering.concurrency import scan_active_resource_conflicts
from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
from kun.engineering.system_coordination import (
    CoordinationIssue,
    collect_coordination_issues,
    summarize_coordination_issues,
    summarize_remediation_plans,
)
from kun.ops.secret_audit import SecretAuditItem, audit_runtime_secrets
from kun.world.handler_health import (
    WorldHandlerHealthCard,
    collect_world_handler_health,
    summarize_handler_health,
)

HealthSeverity = Literal["info", "warn", "error", "critical"]
GovernanceRisk = Literal["low", "medium", "high"]
GovernanceApplyStatus = Literal["applied", "dry_run", "blocked"]
GovernanceApplyRisk = Literal["low", "medium", "high", "unknown"]
_SAFE_CONTEXT_MAINTENANCE_RECOMMENDATION_IDS = {"govern:context_slimming_candidates"}
_SAFE_CONTEXT_MAINTENANCE_HARD_DELETE_AFTER_DAYS = 1_000_000_000


class SystemHealthFinding(BaseModel):
    """One actionable NUO system finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    severity: HealthSeverity
    subsystem: str
    title: str
    detail: str
    suggested_action: str


class SystemGovernanceRecommendation(BaseModel):
    """Conservative remediation advice for a NUO finding.

    This is deliberately not an executor.  Safe actions point at dry-run/apply
    surfaces that already exist; high-risk actions remain advice requiring
    explicit human approval.
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    finding_id: str
    subsystem: str
    title: str
    risk_level: GovernanceRisk
    suggested_action: str
    default_dry_run: bool = True
    can_apply: bool = False
    requires_human_approval: bool = True
    apply_hint: str | None = None


class GovernanceApplyBlockedReason(BaseModel):
    """Structured reason explaining why NUO refused to apply a recommendation."""

    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str


class GovernanceActionTicket(BaseModel):
    """Human-facing ticket for recommendations NUO must not execute automatically."""

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    finding_id: str
    subsystem: str
    title: str
    risk_level: GovernanceRisk
    suggested_action: str
    requires_human_approval: bool
    apply_hint: str | None = None


class GovernanceRecommendationApplyResult(BaseModel):
    """Result of an explicit NUO governance recommendation dry-run/apply request."""

    model_config = ConfigDict(extra="forbid")

    status: GovernanceApplyStatus
    applied: bool
    dry_run: bool
    blocked: bool
    recommendation_id: str
    risk_level: GovernanceApplyRisk
    message: str
    blocked_reason: str | None = None
    blocked_reasons: list[GovernanceApplyBlockedReason] = Field(default_factory=list)
    action_ticket: GovernanceActionTicket | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SystemHealthReport(BaseModel):
    """NUO deep health report."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    total_tasks: int = 0
    runtime_by_status: dict[str, int] = Field(default_factory=dict)
    outbox_lag: int = 0
    pending_approvals: int = 0
    stale_runtime_count: int = 0
    resumable_mission_task_count: int = 0
    mission_resume_worker_enabled: bool = False
    active_resource_conflicts: int = 0
    delivery_status_issues: list[str] = Field(default_factory=list)
    secret_audit_summary: dict[str, int] = Field(default_factory=dict)
    secret_audit_items: list[SecretAuditItem] = Field(default_factory=list)
    world_handler_summary: dict[str, int] = Field(default_factory=dict)
    world_handlers: list[WorldHandlerHealthCard] = Field(default_factory=list)
    compiler_governance_summary: dict[str, int] = Field(default_factory=dict)
    context_maintenance_summary: dict[str, int] = Field(default_factory=dict)
    context_maintenance_error: str | None = None
    state_ledger_audit_summary: dict[str, int] = Field(default_factory=dict)
    skill_health_summary: dict[str, int] = Field(default_factory=dict)
    skill_health_error: str | None = None
    qi_strategy_draft_summary: dict[str, int] = Field(default_factory=dict)
    qi_strategy_draft_error: str | None = None
    multi_lane_scheduler_summary: dict[str, int] = Field(default_factory=dict)
    multi_lane_scheduler_limits: dict[str, int] = Field(default_factory=dict)
    production_risk_summary: dict[str, int] = Field(default_factory=dict)
    production_risk_issues: list[str] = Field(default_factory=list)
    coordination_summary: dict[str, int] = Field(default_factory=dict)
    coordination_remediation_summary: dict[str, int] = Field(default_factory=dict)
    coordination_issues: list[CoordinationIssue] = Field(default_factory=list)
    findings: list[SystemHealthFinding] = Field(default_factory=list)
    governance_recommendations: list[SystemGovernanceRecommendation] = Field(default_factory=list)

    @property
    def worst_severity(self) -> HealthSeverity:
        order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
        if not self.findings:
            return "info"
        return max((finding.severity for finding in self.findings), key=lambda item: order[item])


async def collect_system_health_report(
    *,
    tenant_id: str,
    stale_after: timedelta = timedelta(minutes=30),
) -> SystemHealthReport:
    """Collect a tenant-scoped NUO system health report."""
    now = datetime.now(UTC)
    stale_before = now - stale_after
    async with session_scope(tenant_id=tenant_id) as s:
        runtime_rows = (
            await s.execute(
                select(RuntimeStateRow.status, func.count())
                .where(RuntimeStateRow.tenant_id == tenant_id)
                .group_by(RuntimeStateRow.status)
            )
        ).all()
        runtime_by_status = {str(status): int(count) for status, count in runtime_rows}
        total_tasks = int(
            (
                await s.execute(
                    select(func.count()).select_from(TaskRow).where(TaskRow.tenant_id == tenant_id)
                )
            ).scalar_one()
            or 0
        )
        outbox_lag = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id, EventRow.published_at.is_(None))
                )
            ).scalar_one()
            or 0
        )
        pending_approvals = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(PendingActionRow)
                    .where(
                        PendingActionRow.tenant_id == tenant_id,
                        PendingActionRow.status == "pending_approval",
                    )
                )
            ).scalar_one()
            or 0
        )
        stale_runtime_count = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(RuntimeStateRow)
                    .where(
                        RuntimeStateRow.tenant_id == tenant_id,
                        RuntimeStateRow.status.in_(("queued", "running")),
                        RuntimeStateRow.last_updated < stale_before,
                    )
                )
            ).scalar_one()
            or 0
        )
        max_attempts = int(os.getenv("KUN_MISSION_REAPER_MAX_ATTEMPTS", "3"))
        resumable_mission_task_count = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(MissionTaskRow)
                    .join(RuntimeStateRow, RuntimeStateRow.task_ref == MissionTaskRow.task_id)
                    .join(MissionRow, MissionRow.mission_id == MissionTaskRow.mission_id)
                    .where(
                        MissionTaskRow.tenant_id == tenant_id,
                        RuntimeStateRow.tenant_id == tenant_id,
                        MissionRow.tenant_id == tenant_id,
                        MissionRow.status.in_(("planned", "running", "paused")),
                        MissionTaskRow.status.in_(
                            ("planned", "queued", "running", "paused", "blocked")
                        ),
                        RuntimeStateRow.status == "queued",
                        MissionTaskRow.resume_attempts < max_attempts,
                    )
                )
            ).scalar_one()
            or 0
        )
        active_resource_conflicts = len(
            await scan_active_resource_conflicts(s, tenant_id=tenant_id)
        )

    mission_resume_worker_enabled = os.getenv("KUN_MISSION_RESUME_WORKER_ENABLED", "1") == "1"
    delivery_issues = validate_delivery_status(get_v3_delivery_status())
    secret_audit = audit_runtime_secrets()
    world_handlers = await collect_world_handler_health(tenant_id=tenant_id)
    (
        context_maintenance_summary,
        context_maintenance_error,
    ) = await _collect_context_maintenance_summary(tenant_id=tenant_id)
    compiler_governance_summary = _compiler_governance_summary(context_maintenance_summary)
    state_ledger_audit_summary = await _collect_state_ledger_audit_summary(tenant_id=tenant_id)
    skill_health_summary, skill_health_error = await _collect_skill_health_summary(
        tenant_id=tenant_id
    )
    qi_strategy_draft_summary, qi_strategy_draft_error = await _collect_qi_strategy_draft_summary(
        tenant_id=tenant_id
    )
    multi_lane_scheduler_summary = await _collect_multi_lane_scheduler_summary(tenant_id=tenant_id)
    production_risk_summary, production_risk_issues = _collect_production_risk_summary(
        delivery_issues=delivery_issues,
        secret_audit_summary=secret_audit.summary,
    )
    coordination_issues = await collect_coordination_issues(tenant_id=tenant_id)
    findings = _findings(
        outbox_lag=outbox_lag,
        pending_approvals=pending_approvals,
        stale_runtime_count=stale_runtime_count,
        resumable_mission_task_count=resumable_mission_task_count,
        mission_resume_worker_enabled=mission_resume_worker_enabled,
        active_resource_conflicts=active_resource_conflicts,
        delivery_issues=delivery_issues,
        secret_audit_items=secret_audit.items,
        world_handlers=world_handlers,
        context_maintenance_summary=context_maintenance_summary,
        context_maintenance_error=context_maintenance_error,
        state_ledger_audit_summary=state_ledger_audit_summary,
        skill_health_summary=skill_health_summary,
        skill_health_error=skill_health_error,
        qi_strategy_draft_summary=qi_strategy_draft_summary,
        qi_strategy_draft_error=qi_strategy_draft_error,
        multi_lane_scheduler_summary=multi_lane_scheduler_summary,
        production_risk_summary=production_risk_summary,
        production_risk_issues=production_risk_issues,
        coordination_issues=coordination_issues,
    )
    governance_recommendations = _governance_recommendations(
        findings=findings,
        coordination_issues=coordination_issues,
    )
    return SystemHealthReport(
        tenant_id=tenant_id,
        generated_at=now,
        total_tasks=total_tasks,
        runtime_by_status=runtime_by_status,
        outbox_lag=outbox_lag,
        pending_approvals=pending_approvals,
        stale_runtime_count=stale_runtime_count,
        resumable_mission_task_count=resumable_mission_task_count,
        mission_resume_worker_enabled=mission_resume_worker_enabled,
        active_resource_conflicts=active_resource_conflicts,
        delivery_status_issues=delivery_issues,
        secret_audit_summary=secret_audit.summary,
        secret_audit_items=secret_audit.items,
        world_handler_summary=summarize_handler_health(world_handlers),
        world_handlers=world_handlers,
        compiler_governance_summary=compiler_governance_summary,
        context_maintenance_summary=context_maintenance_summary,
        context_maintenance_error=context_maintenance_error,
        state_ledger_audit_summary=state_ledger_audit_summary,
        skill_health_summary=skill_health_summary,
        skill_health_error=skill_health_error,
        qi_strategy_draft_summary=qi_strategy_draft_summary,
        qi_strategy_draft_error=qi_strategy_draft_error,
        multi_lane_scheduler_summary=multi_lane_scheduler_summary,
        multi_lane_scheduler_limits={str(k): int(v) for k, v in DEFAULT_LANE_LIMITS.items()},
        production_risk_summary=production_risk_summary,
        production_risk_issues=production_risk_issues,
        coordination_summary=summarize_coordination_issues(coordination_issues),
        coordination_remediation_summary=summarize_remediation_plans(coordination_issues),
        coordination_issues=coordination_issues,
        findings=findings,
        governance_recommendations=governance_recommendations,
    )


async def apply_governance_recommendation(
    *,
    tenant_id: str,
    recommendation_id: str,
    dry_run: bool = True,
    max_assets: int = 500,
) -> GovernanceRecommendationApplyResult:
    """Explicitly dry-run/apply one current governance recommendation.

    First-version execution is intentionally tiny: only low-risk context
    maintenance recommendations can run.  Everything else returns a structured
    blocked result plus a ticket for human follow-up.
    """

    report = await collect_system_health_report(tenant_id=tenant_id)
    return await _apply_governance_recommendation_from_queue(
        tenant_id=tenant_id,
        recommendations=report.governance_recommendations,
        recommendation_id=recommendation_id,
        dry_run=dry_run,
        max_assets=max_assets,
    )


async def _apply_governance_recommendation_from_queue(
    *,
    tenant_id: str,
    recommendations: list[SystemGovernanceRecommendation],
    recommendation_id: str,
    dry_run: bool,
    max_assets: int,
) -> GovernanceRecommendationApplyResult:
    recommendation = next(
        (item for item in recommendations if item.recommendation_id == recommendation_id),
        None,
    )
    if recommendation is None:
        return _blocked_apply_result(
            recommendation_id=recommendation_id,
            risk_level="unknown",
            message=f"Governance recommendation {recommendation_id!r} is not in the current queue.",
            reasons=[
                GovernanceApplyBlockedReason(
                    code="recommendation_not_found",
                    detail="Collect a fresh NUO health report and apply an existing recommendation_id.",
                )
            ],
        )

    blocked_reasons = _governance_apply_blocked_reasons(recommendation)
    if blocked_reasons:
        return _blocked_apply_result(
            recommendation_id=recommendation.recommendation_id,
            risk_level=recommendation.risk_level,
            message=(
                "NUO refused to auto-apply this governance recommendation; "
                "use the returned action_ticket for explicit human follow-up."
            ),
            reasons=blocked_reasons,
            action_ticket=_action_ticket_for(recommendation),
        )

    context_report = await run_context_maintenance(
        tenant_id=tenant_id,
        dry_run=dry_run,
        max_assets=max_assets,
        hard_delete_after_days=_SAFE_CONTEXT_MAINTENANCE_HARD_DELETE_AFTER_DAYS,
        merge_duplicates=False,
    )
    status: GovernanceApplyStatus = "dry_run" if dry_run else "applied"
    return GovernanceRecommendationApplyResult(
        status=status,
        applied=not dry_run,
        dry_run=dry_run,
        blocked=False,
        recommendation_id=recommendation.recommendation_id,
        risk_level=recommendation.risk_level,
        message=(
            "Dry-run completed for context maintenance; no state was changed."
            if dry_run
            else "Applied low-risk context maintenance recommendation."
        ),
        details={
            "action": "context_maintenance",
            "max_assets": max_assets,
            "hard_delete_after_days": _SAFE_CONTEXT_MAINTENANCE_HARD_DELETE_AFTER_DAYS,
            "merge_duplicates": False,
            "context_maintenance": _context_maintenance_details(context_report),
        },
    )


def _findings(
    *,
    outbox_lag: int,
    pending_approvals: int,
    stale_runtime_count: int,
    active_resource_conflicts: int,
    delivery_issues: list[str],
    secret_audit_items: list[SecretAuditItem],
    world_handlers: list[WorldHandlerHealthCard],
    context_maintenance_summary: dict[str, int] | None = None,
    context_maintenance_error: str | None = None,
    state_ledger_audit_summary: dict[str, int] | None = None,
    skill_health_summary: dict[str, int] | None = None,
    skill_health_error: str | None = None,
    qi_strategy_draft_summary: dict[str, int] | None = None,
    qi_strategy_draft_error: str | None = None,
    multi_lane_scheduler_summary: dict[str, int] | None = None,
    production_risk_summary: dict[str, int] | None = None,
    production_risk_issues: list[str] | None = None,
    coordination_issues: list[CoordinationIssue] | None = None,
    resumable_mission_task_count: int = 0,
    mission_resume_worker_enabled: bool = False,
) -> list[SystemHealthFinding]:
    findings: list[SystemHealthFinding] = []
    if outbox_lag > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="outbox_lag",
                severity="warn" if outbox_lag < 100 else "error",
                subsystem="events",
                title="事件 outbox 有积压",
                detail=f"还有 {outbox_lag} 条事件未发布。",
                suggested_action="检查 outbox worker / NATS 连接；必要时触发重连或人工排查。",
            )
        )
    if stale_runtime_count > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="stale_runtime",
                severity="error",
                subsystem="mission",
                title="存在卡住的运行时任务",
                detail=f"{stale_runtime_count} 个 queued/running 任务超过阈值没有更新。",
                suggested_action="让 Mission reaper 标记、恢复或升级人工处理。",
            )
        )
    if resumable_mission_task_count > 0 and not mission_resume_worker_enabled:
        findings.append(
            SystemHealthFinding(
                finding_id="mission_resume_worker_disabled",
                severity="warn",
                subsystem="mission",
                title="Mission 有可推进任务，但自动续跑未开启",
                detail=(
                    f"{resumable_mission_task_count} 个 Mission task 已排队，"
                    "但 KUN_MISSION_RESUME_WORKER_ENABLED 被关闭。"
                ),
                suggested_action=(
                    "如果你希望 KUN 自动推进长期任务，保持 "
                    "KUN_MISSION_RESUME_WORKER_ENABLED=1；如需停掉自动续跑，再手动设为 0。"
                ),
            )
        )
    if active_resource_conflicts > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="active_resource_conflict",
                severity="error",
                subsystem="coordination",
                title="运行中的任务存在资源冲突",
                detail=f"{active_resource_conflicts} 个资源冲突仍处于活跃任务中。",
                suggested_action="让守望暂停低优先级任务，或把共享资源操作改为串行队列。",
            )
        )
    if pending_approvals > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="pending_approval",
                severity="info",
                subsystem="world_gateway",
                title="有外部动作等待确认",
                detail=f"{pending_approvals} 个动作需要用户审批。",
                suggested_action="在 NUO 权限入口确认、拒绝或要求重做。",
            )
        )
    for issue in delivery_issues:
        findings.append(
            SystemHealthFinding(
                finding_id=f"delivery:{hashlib.sha256(issue.encode('utf-8')).hexdigest()[:12]}",
                severity="warn",
                subsystem="delivery_status",
                title="能力边界标注不诚实",
                detail=issue,
                suggested_action="修正文档/状态，或补主流程调用和测试后再标 ready。",
            )
        )
    for item in secret_audit_items:
        if item.severity == "ok":
            continue
        findings.append(
            SystemHealthFinding(
                finding_id=f"secret:{item.item_id}",
                severity="error" if item.severity == "blocker" else "warn",
                subsystem=f"secret_audit.{item.area}",
                title=item.title,
                detail=item.detail,
                suggested_action=item.suggested_action,
            )
        )
    for card in world_handlers:
        if card.status in {"blocked", "unregistered"}:
            findings.append(
                SystemHealthFinding(
                    finding_id=f"world:{card.action_type}",
                    severity="error" if card.status == "blocked" else "warn",
                    subsystem="world_gateway",
                    title=f"外部动作 {card.action_type} 不健康",
                    detail="；".join(card.issues) or card.recommendation,
                    suggested_action=card.recommendation,
                )
            )
        elif card.status == "limited" and _world_handler_needs_finding(card):
            findings.append(
                SystemHealthFinding(
                    finding_id=f"world:{card.action_type}",
                    severity="warn",
                    subsystem="world_gateway",
                    title=f"外部动作 {card.action_type} 有风险",
                    detail="；".join(card.issues) or card.recommendation,
                    suggested_action=card.recommendation,
                )
            )
    if context_maintenance_error:
        findings.append(
            SystemHealthFinding(
                finding_id="context_maintenance_error",
                severity="warn",
                subsystem="context",
                title="Context / memory 瘦身体检失败",
                detail=context_maintenance_error,
                suggested_action="检查 AssetStore 后端和 context maintenance 配置；不要在无法体检时盲目积累记忆。",
            )
        )
    context_summary = context_maintenance_summary or {}
    context_hard_delete = int(context_summary.get("hard_deleted", 0) or 0)
    context_soft_forget = int(context_summary.get("soft_forgotten", 0) or 0)
    context_compress = int(context_summary.get("compressed", 0) or 0)
    context_duplicates = int(context_summary.get("duplicate_candidates", 0) or 0)
    context_compiler_review = int(context_summary.get("compiler_review", 0) or 0)
    if context_hard_delete > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="context_hard_delete_candidates",
                severity="warn",
                subsystem="context",
                title="Context / memory 有可硬删除的长期未用资产",
                detail=f"dry-run 发现 {context_hard_delete} 个长期未用且非永久资产。",
                suggested_action="先查看 /nuo/health/context-maintenance/run?dry_run=true，确认后再执行真实瘦身。",
            )
        )
    if context_soft_forget + context_compress + context_duplicates > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="context_slimming_candidates",
                severity="info",
                subsystem="context",
                title="Context / memory 有可瘦身项",
                detail=(
                    f"可压缩 {context_compress}，可软遗忘 {context_soft_forget}，"
                    f"重复候选 {context_duplicates}。"
                ),
                suggested_action="先用 dry-run 看明细，再决定是否让傩执行压缩、软遗忘或人工合并重复资产。",
            )
        )
    if context_compiler_review > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="compiler_asset_review_candidates",
                severity="warn",
                subsystem="compiler",
                title="编译资产需要复核",
                detail=f"dry-run 发现 {context_compiler_review} 个编译资产有风险、来源或 profile 缺口。",
                suggested_action="检查 compiler 输出来源、风险标记和 profile；必要时重新编译或从 Context 中降权。",
            )
        )
    context_recompile = int(context_summary.get("compiler_recompile_recommended", 0) or 0)
    if context_recompile > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="compiler_recompile_candidates",
                severity="warn",
                subsystem="compiler",
                title="编译资产建议重新编译",
                detail=f"dry-run 发现 {context_recompile} 个编译资产质量低或受限。",
                suggested_action=(
                    "先运行 kun compiler recompile-candidates dry-run 查看来源；"
                    "确认 allowed_root / URL 白名单后再显式 apply。"
                ),
            )
        )
    audit_summary = state_ledger_audit_summary or {}
    drift_count = int(audit_summary.get("drift", 0) or 0)
    missing_history = int(audit_summary.get("missing_history", 0) or 0)
    if drift_count > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="state_ledger_drift",
                severity="error",
                subsystem="state_ledger",
                title="状态账本快照和事件回放不一致",
                detail=(
                    f"抽检 {audit_summary.get('checked', 0)} 个任务，"
                    f"{drift_count} 个出现状态或成本漂移。"
                ),
                suggested_action=(
                    "打开 /api/blackboard/state-ledger/{task_id}/audit 定位漂移；"
                    "必要时用 EventRow 回放修正当前快照。"
                ),
            )
        )
    elif missing_history > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="state_ledger_missing_history",
                severity="warn",
                subsystem="state_ledger",
                title="部分状态账本缺少可回放历史",
                detail=(
                    f"抽检 {audit_summary.get('checked', 0)} 个任务，"
                    f"{missing_history} 个当前快照没有对应 EventRow 历史。"
                ),
                suggested_action="检查事件写入链路，避免只有当前状态、没有长期审计依据。",
            )
        )
    if skill_health_error:
        findings.append(
            SystemHealthFinding(
                finding_id="skill_health_error",
                severity="warn",
                subsystem="skill",
                title="Skill 治理信号采集失败",
                detail=skill_health_error,
                suggested_action="检查 SkillRegistry / dispatcher / capability card 读路径；不要把 skill 清单当成真实可执行能力。",
            )
        )
    skill_summary = skill_health_summary or {}
    manifest_without_executor = int(skill_summary.get("manifest_without_executor", 0) or 0)
    weak_skill_cards = int(skill_summary.get("weak_capability_cards", 0) or 0)
    unused_manifest_skills = int(skill_summary.get("unused_manifest_skills", 0) or 0)
    if manifest_without_executor > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="skill_manifest_without_executor",
                severity="warn",
                subsystem="skill",
                title="部分 skill 只有说明书，没有可执行入口",
                detail=f"{manifest_without_executor} 个 SkillRegistry manifest 没有 dispatcher executor。",
                suggested_action="补 executor、降级为 context-only skill，或在选择器里降低其执行权重。",
            )
        )
    if weak_skill_cards > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="skill_weak_capability_cards",
                severity="warn",
                subsystem="skill",
                title="部分 skill 能力卡可靠性偏低",
                detail=f"{weak_skill_cards} 张 skill capability card 仍是冷启动或可靠性低于阈值。",
                suggested_action="把这些 skill 放入评测/回放；不要因存在 manifest 就优先调用。",
            )
        )
    if unused_manifest_skills > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="skill_unused_manifest",
                severity="info",
                subsystem="skill",
                title="存在长期没有贡献信用的 skill",
                detail=f"{unused_manifest_skills} 个 manifest skill 尚未出现在 skill resource credit 中。",
                suggested_action="让 Qi/idle replay 生成覆盖样本，或把低价值 skill 标记为候选瘦身对象。",
            )
        )
    if qi_strategy_draft_error:
        findings.append(
            SystemHealthFinding(
                finding_id="qi_strategy_draft_error",
                severity="warn",
                subsystem="qi",
                title="Qi 策略草案治理信号采集失败",
                detail=qi_strategy_draft_error,
                suggested_action="检查 methodology AssetStore；Qi 草案不可见时不要升级生产策略。",
            )
        )
    qi_summary = qi_strategy_draft_summary or {}
    qi_prod_actions = int(qi_summary.get("production_action_true", 0) or 0)
    qi_needs_review = int(qi_summary.get("needs_strong_review", 0) or 0)
    qi_needs_evidence = int(qi_summary.get("review_needs_evidence", 0) or 0)
    qi_ready = int(qi_summary.get("review_ready_for_human_review", 0) or 0)
    if qi_prod_actions > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="qi_strategy_draft_production_action",
                severity="critical",
                subsystem="qi",
                title="Qi 策略草案含生产动作标记",
                detail=f"{qi_prod_actions} 个 Qi StrategyPack 草案带 production_action=true。",
                suggested_action="立即阻止这些草案进入 Watchtower 生产路由；改成 review_only 并要求人工复核。",
            )
        )
    if qi_needs_review + qi_needs_evidence > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="qi_strategy_drafts_need_review",
                severity="warn",
                subsystem="qi",
                title="Qi 策略草案需要证据或强评审",
                detail=f"强评审 {qi_needs_review}，缺证据 {qi_needs_evidence}。",
                suggested_action="继续让 idle replay / lab replay 补证据；只有人工批准后才允许进入 canary 或 shadow rollout。",
            )
        )
    elif qi_ready > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="qi_strategy_drafts_ready_for_review",
                severity="info",
                subsystem="qi",
                title="Qi 有策略草案等待人工复核",
                detail=f"{qi_ready} 个 StrategyPack 草案已标记 ready_for_human_review。",
                suggested_action="在 NUO/人工评审入口批准、驳回或要求补证据；不要静默推广。",
            )
        )
    lane_summary = multi_lane_scheduler_summary or {}
    missing_lane_count = int(lane_summary.get("missing_required_lanes", 0) or 0)
    pressure_count = int(lane_summary.get("lanes_over_pressure_threshold", 0) or 0)
    if missing_lane_count > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="scheduler_missing_required_lanes",
                severity="error",
                subsystem="scheduler",
                title="多车道调度器缺少必需 lane",
                detail=f"{missing_lane_count} 个 V5 必需 lane 未出现在默认限流配置中。",
                suggested_action="补齐 fast/mission/qi/nuo/world/high_risk lane 后再承接后台治理和真实外部动作。",
            )
        )
    if pressure_count > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="scheduler_lane_pressure",
                severity="warn",
                subsystem="scheduler",
                title="部分执行 lane 有积压压力",
                detail=f"{pressure_count} 条 lane 的活跃任务数超过 lane 限流的压力阈值。",
                suggested_action="检查是否有 worker 未接入 MultiTaskScheduler，或为对应 lane 单独扩容/降频。",
            )
        )
    prod_summary = production_risk_summary or {}
    prod_safety = int(prod_summary.get("production_safety_issues", 0) or 0)
    deployment_partial = int(prod_summary.get("partial_or_not_ready_capabilities", 0) or 0)
    if prod_safety > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="production_safety_issues",
                severity="critical",
                subsystem="production",
                title="生产部署安全门未通过",
                detail="；".join(production_risk_issues or []),
                suggested_action="先修生产密钥、默认租户、数据库/对象存储凭证等部署 blocker；傩不能自动修改这些高风险配置。",
            )
        )
    elif deployment_partial > 0:
        findings.append(
            SystemHealthFinding(
                finding_id="deployment_partial_capabilities",
                severity="info",
                subsystem="production",
                title="仍有能力不能对外宣称 ready",
                detail=f"{deployment_partial} 个能力仍是 partial/audit_only/not_ready。",
                suggested_action="保持交付状态诚实；上线说明里不要把半闭环能力写成已完成。",
            )
        )
    for coordination_issue in coordination_issues or []:
        findings.append(
            SystemHealthFinding(
                finding_id=f"coordination:{coordination_issue.issue_id}",
                severity=coordination_issue.severity,
                subsystem=coordination_issue.subsystem,
                title=coordination_issue.title,
                detail=coordination_issue.detail,
                suggested_action=coordination_issue.suggested_action,
            )
        )
    return findings


def _governance_recommendations(
    *,
    findings: list[SystemHealthFinding],
    coordination_issues: list[CoordinationIssue] | None = None,
) -> list[SystemGovernanceRecommendation]:
    """Turn findings into conservative, explicit remediation posture."""

    by_finding_id = {finding.finding_id: finding for finding in findings}
    recommendations: list[SystemGovernanceRecommendation] = []
    for finding in findings:
        risk_level = _risk_for_finding(finding)
        can_apply = False
        requires_human = risk_level != "low"
        apply_hint: str | None = None
        suggested_action = finding.suggested_action
        if finding.finding_id == "context_slimming_candidates":
            risk_level = "low"
            can_apply = True
            requires_human = False
            apply_hint = (
                "POST /api/nuo/health/governance/apply?"
                "recommendation_id=govern:context_slimming_candidates&dry_run=true"
            )
        elif finding.finding_id == "compiler_recompile_candidates":
            risk_level = "medium"
            apply_hint = "kun compiler recompile-candidates --dry-run"
        elif finding.finding_id == "compiler_asset_review_candidates":
            risk_level = "medium"
            apply_hint = "POST /api/nuo/health/context-maintenance/run?dry_run=true"
        elif finding.finding_id.startswith("world:"):
            risk_level = "high" if finding.severity in {"error", "critical"} else "medium"
            apply_hint = "POST /api/nuo/actions/handlers/auto-quarantine?dry_run=true"
        elif finding.finding_id.startswith("qi_"):
            can_apply = False
            requires_human = True
        elif finding.finding_id in {"production_safety_issues", "scheduler_missing_required_lanes"}:
            risk_level = "high"
            can_apply = False
            requires_human = True

        recommendations.append(
            SystemGovernanceRecommendation(
                recommendation_id=f"govern:{finding.finding_id}",
                finding_id=finding.finding_id,
                subsystem=finding.subsystem,
                title=finding.title,
                risk_level=risk_level,
                suggested_action=suggested_action,
                default_dry_run=True,
                can_apply=can_apply,
                requires_human_approval=requires_human,
                apply_hint=apply_hint,
            )
        )

    for issue in coordination_issues or []:
        plan = issue.remediation_plan
        if plan is None:
            continue
        finding_id = f"coordination:{issue.issue_id}"
        matched_finding = by_finding_id.get(finding_id)
        recommendations.append(
            SystemGovernanceRecommendation(
                recommendation_id=plan.plan_id,
                finding_id=finding_id,
                subsystem=issue.subsystem,
                title=matched_finding.title if matched_finding else issue.title,
                risk_level=plan.risk_level,
                suggested_action=plan.reason,
                default_dry_run=True,
                can_apply=plan.can_auto_execute,
                requires_human_approval=not plan.can_auto_execute or plan.risk_level != "low",
                apply_hint=plan.suggested_command,
            )
        )
    return _dedupe_recommendations(recommendations)


async def _collect_context_maintenance_summary(
    *,
    tenant_id: str,
    max_assets: int = 200,
) -> tuple[dict[str, int], str | None]:
    """Run a dry-run context slimming audit for NUO's deep health report."""

    try:
        report = await run_context_maintenance(
            tenant_id=tenant_id,
            dry_run=True,
            max_assets=max_assets,
        )
    except Exception as exc:
        return {}, str(exc)
    return (
        {
            "total_seen": report.total_seen,
            "compressed": report.compressed,
            "soft_forgotten": report.soft_forgotten,
            "hard_deleted": report.hard_deleted,
            "duplicate_candidates": report.duplicate_candidates,
            "compiler_review": report.compiler_review,
            "compiler_recompile_recommended": report.compiler_recompile_recommended,
            "low_value_marked": report.low_value_marked,
            "stale_or_risky_marked": report.stale_or_risky_marked,
            "kept": report.kept,
        },
        None,
    )


def _compiler_governance_summary(context_summary: dict[str, int]) -> dict[str, int]:
    return {
        "compiler_review": int(context_summary.get("compiler_review", 0) or 0),
        "compiler_recompile_recommended": int(
            context_summary.get("compiler_recompile_recommended", 0) or 0
        ),
        "compiler_governance_findings": int(context_summary.get("compiler_review", 0) or 0)
        + int(context_summary.get("compiler_recompile_recommended", 0) or 0),
    }


async def _collect_skill_health_summary(
    *,
    tenant_id: str,
) -> tuple[dict[str, int], str | None]:
    """Collect skill governance signals without using external discovery."""

    try:
        from kun.context.storage import get_store
        from kun.skills.dispatcher import list_registered
        from kun.skills.loader import get_registry

        registry = get_registry()
        manifest_skill_ids = set(registry.names())
        executor_skill_ids = set(list_registered())
        skill_assets = await get_store().list(tenant_id=tenant_id, asset_kind="skill", limit=1000)
        skill_credit_rows: list[Any] = []
        capability_rows: list[Any] = []
        async with session_scope(tenant_id=tenant_id) as s:
            skill_credit_rows = list(
                (
                    await s.execute(
                        select(ResourceCreditRow).where(
                            ResourceCreditRow.tenant_id == tenant_id,
                            ResourceCreditRow.resource_kind == "skill",
                        )
                    )
                )
                .scalars()
                .all()
            )
            capability_rows = list(
                (
                    await s.execute(
                        select(CapabilityCardRow).where(
                            CapabilityCardRow.tenant_id == tenant_id,
                            CapabilityCardRow.entity_type == "skill",
                        )
                    )
                )
                .scalars()
                .all()
            )
        credited_skill_ids = {
            str(getattr(row, "resource_id", "") or "").removeprefix("skill:")
            for row in skill_credit_rows
        }
        cold_cards = [
            row for row in capability_rows if str(getattr(row, "maturity", "")) == "cold_start"
        ]
        weak_cards = [
            row
            for row in capability_rows
            if str(getattr(row, "maturity", "")) == "cold_start"
            or float(getattr(row, "overall_reliability", 0.0) or 0.0) < 0.45
        ]
        return (
            {
                "manifest_skills": len(manifest_skill_ids),
                "registered_executors": len(executor_skill_ids),
                "skill_assets": len(skill_assets),
                "skill_credit_rows": len(skill_credit_rows),
                "skill_capability_cards": len(capability_rows),
                "manifest_without_executor": len(manifest_skill_ids - executor_skill_ids),
                "executor_without_manifest": len(executor_skill_ids - manifest_skill_ids),
                "unused_manifest_skills": len(manifest_skill_ids - credited_skill_ids),
                "cold_start_capability_cards": len(cold_cards),
                "weak_capability_cards": len(weak_cards),
            },
            None,
        )
    except Exception as exc:
        return {}, str(exc)


async def _collect_qi_strategy_draft_summary(
    *,
    tenant_id: str,
) -> tuple[dict[str, int], str | None]:
    """Summarize review-only Qi StrategyPack drafts and open problem queue."""

    try:
        from kun.context.storage import get_store

        assets = await get_store().list(tenant_id=tenant_id, asset_kind="methodology", limit=1000)
        draft_assets = [
            asset
            for asset in assets
            if asset.l1_metadata.get("source") == "qi.idle_replay.strategy_pack_draft"
        ]
        review_counts: Counter[str] = Counter()
        rollout_counts: Counter[str] = Counter()
        needs_strong_review = 0
        production_action_true = 0
        for asset in draft_assets:
            metadata = asset.l1_metadata
            review_status = str(metadata.get("qi_review_status") or "unreviewed")
            rollout_status = str(metadata.get("qi_rollout_plan_status") or "unplanned")
            review_counts[review_status] += 1
            rollout_counts[rollout_status] += 1
            draft_payload = metadata.get("strategy_pack_draft")
            if isinstance(draft_payload, dict) and draft_payload.get("requires_strong_review"):
                needs_strong_review += 1
            if metadata.get("production_action") is True:
                production_action_true += 1
        open_problem_signals = 0
        try:
            async with session_scope(tenant_id=tenant_id) as s:
                open_problem_signals = int(
                    (
                        await s.execute(
                            select(func.count())
                            .select_from(QiProblemSignalRow)
                            .where(
                                QiProblemSignalRow.tenant_id == tenant_id,
                                QiProblemSignalRow.status == "open",
                            )
                        )
                    ).scalar_one()
                    or 0
                )
        except Exception:
            open_problem_signals = 0
        summary = {
            "drafts": len(draft_assets),
            "needs_strong_review": needs_strong_review,
            "production_action_true": production_action_true,
            "open_problem_signals": open_problem_signals,
        }
        for status, count in review_counts.items():
            summary[f"review_{status}"] = int(count)
        for status, count in rollout_counts.items():
            summary[f"rollout_{status}"] = int(count)
        return summary, None
    except Exception as exc:
        return {}, str(exc)


async def _collect_multi_lane_scheduler_summary(
    *,
    tenant_id: str,
    limit: int = 500,
) -> dict[str, int]:
    """Estimate durable task pressure by V5 scheduler lane.

    The live scheduler is app-state, while NUO deep health can run from CLI or
    idle-batch without an ASGI request.  This collector therefore audits the
    required lane configuration plus active DB tasks, and the API dashboard
    remains the source for exact in-memory queue depth.
    """

    required_lanes: set[str] = {"fast", "mission", "qi", "nuo", "world", "high_risk"}
    summary: dict[str, int] = {
        "configured_lanes": len(DEFAULT_LANE_LIMITS),
        "missing_required_lanes": len(required_lanes - set(DEFAULT_LANE_LIMITS)),
        "active_tasks_sampled": 0,
        "lanes_over_pressure_threshold": 0,
    }
    active_by_lane: Counter[TaskLane] = Counter()
    try:
        async with session_scope(tenant_id=tenant_id) as s:
            rows = (
                await s.execute(
                    select(TaskRow, RuntimeStateRow)
                    .join(
                        RuntimeStateRow,
                        (RuntimeStateRow.task_ref == TaskRow.task_id)
                        & (RuntimeStateRow.tenant_id == TaskRow.tenant_id),
                    )
                    .where(
                        TaskRow.tenant_id == tenant_id,
                        RuntimeStateRow.tenant_id == tenant_id,
                        RuntimeStateRow.status.in_(("queued", "running", "paused")),
                    )
                    .limit(limit)
                )
            ).all()
        for task, _runtime in rows:
            lane = _heuristic_lane_from_task_row(task)
            active_by_lane[lane] += 1
            summary["active_tasks_sampled"] += 1
        pressure = 0
        for active_lane, count in active_by_lane.items():
            summary[f"active_{active_lane}"] = int(count)
            lane_limit = int(DEFAULT_LANE_LIMITS[active_lane])
            if count > lane_limit * 5:
                pressure += 1
        summary["lanes_over_pressure_threshold"] = pressure
    except Exception:
        summary["collect_error"] = 1
    return summary


def _collect_production_risk_summary(
    *,
    delivery_issues: list[str],
    secret_audit_summary: dict[str, int],
) -> tuple[dict[str, int], list[str]]:
    cfg = settings()
    production_issues = cfg.production_safety_issues()
    delivery_items = get_v3_delivery_status()
    partial_or_missing = sum(1 for item in delivery_items if item.status != "ready")
    return (
        {
            "env_is_production": 1 if cfg.env == "production" else 0,
            "production_safety_issues": len(production_issues),
            "delivery_validation_issues": len(delivery_issues),
            "partial_or_not_ready_capabilities": partial_or_missing,
            "secret_blockers": int(secret_audit_summary.get("blocker", 0) or 0),
            "secret_warnings": int(secret_audit_summary.get("warn", 0) or 0),
        },
        production_issues,
    )


async def _collect_state_ledger_audit_summary(
    *,
    tenant_id: str,
    limit: int = 20,
    history_limit: int = 100,
) -> dict[str, int]:
    """Sample persisted current snapshots and compare them with EventRow replay.

    傩体检不在这里做完整事件溯源，只做低成本抽检：当前快照是否还有
    对应事件、状态/成本有没有明显漂移、回放是否有缺口。
    """

    summary = {
        "checked": 0,
        "missing_history": 0,
        "status_drift": 0,
        "cost_drift": 0,
        "history_gap": 0,
        "drift": 0,
    }
    try:
        async with session_scope(tenant_id=tenant_id) as s:
            rows = (
                (
                    await s.execute(
                        select(StateLedgerEntryRow)
                        .where(
                            StateLedgerEntryRow.tenant_id == tenant_id,
                            StateLedgerEntryRow.status.in_(("queued", "running", "paused")),
                        )
                        .order_by(StateLedgerEntryRow.updated_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                events = (
                    (
                        await s.execute(
                            select(EventRow)
                            .where(
                                EventRow.tenant_id == tenant_id,
                                EventRow.task_ref == row.task_id,
                            )
                            .order_by(EventRow.occurred_at.desc())
                            .limit(history_limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                summary["checked"] += 1
                story = replay_state_ledger_story(
                    row.task_id,
                    [_event_history_item(event) for event in events],
                    history_limit_reached=len(events) >= history_limit,
                )
                if not story.get("event_count"):
                    summary["missing_history"] += 1
                    continue
                row_status = str(row.status or "")
                replay_status = str(story.get("status") or "")
                drifted = False
                if (
                    row_status
                    and replay_status
                    and replay_status != "unknown"
                    and row_status != replay_status
                ):
                    summary["status_drift"] += 1
                    drifted = True
                snapshot = row.snapshot_json if isinstance(row.snapshot_json, dict) else {}
                snapshot_cost = _float_value(snapshot.get("cost_so_far_usd"))
                replay_cost = _float_value(story.get("total_cost_usd"))
                if abs(snapshot_cost - replay_cost) > 0.01:
                    summary["cost_drift"] += 1
                    drifted = True
                gaps = story.get("gaps")
                if isinstance(gaps, list) and gaps:
                    summary["history_gap"] += 1
                if drifted:
                    summary["drift"] += 1
    except Exception:
        # NUO 体检不能因为审计失败拖垮主健康面板；真正异常会进日志。
        return summary
    return summary


def _event_history_item(event: EventRow) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat(),
        "task_id": event.task_ref,
        "summary": event.subject[:200],
        "payload": event.payload if isinstance(event.payload, dict) else {},
    }


def _float_value(value: object) -> float:
    try:
        if isinstance(value, int | float | str):
            return round(float(value), 6)
        return 0.0
    except (TypeError, ValueError):
        return 0.0


def _world_handler_needs_finding(card: WorldHandlerHealthCard) -> bool:
    """Keep low-noise WorldGateway warnings, but surface real side-effect risk."""
    return (
        card.external_dispatched
        or not card.configured
        or not card.has_compensation
        or card.failure_rate >= 0.1
        or card.missing_handler_count > 0
        or card.policy_blocked_count > 0
    )


def _heuristic_lane_from_task_row(task: Any) -> TaskLane:
    task_type = str(getattr(task, "task_type", "") or "").lower()
    risk = str(getattr(task, "risk_level", "") or "").lower()
    spec = getattr(task, "spec_json", None)
    spec_json = spec if isinstance(spec, dict) else {}
    mode = str(spec_json.get("execution_mode") or "").upper()
    required_skills = spec_json.get("required_skills") or []
    required_tools = spec_json.get("required_tools") or []
    if not isinstance(required_skills, list):
        required_skills = []
    if not isinstance(required_tools, list):
        required_tools = []
    merged_refs = " ".join(str(item).lower() for item in [*required_skills, *required_tools])

    if risk in {"high", "critical"} or mode == "ENSEMBLE":
        return "high_risk"
    if task_type.startswith("mission") or "mission" in task_type:
        return "mission"
    if task_type.startswith("qi") or "experiment" in task_type or "lab" in task_type:
        return "qi"
    if task_type.startswith("nuo") or "diagnose" in task_type or "maintenance" in task_type:
        return "nuo"
    if (
        task_type.startswith("world")
        or "external" in task_type
        or "world_request" in merged_refs
        or "world-gateway" in merged_refs
        or "email.send" in merged_refs
    ):
        return "world"
    return "fast"


def _risk_for_finding(finding: SystemHealthFinding) -> GovernanceRisk:
    if finding.severity in {"critical", "error"}:
        return "high"
    if finding.severity == "warn":
        return "medium"
    return "low"


def _dedupe_recommendations(
    items: list[SystemGovernanceRecommendation],
) -> list[SystemGovernanceRecommendation]:
    seen: set[str] = set()
    out: list[SystemGovernanceRecommendation] = []
    for item in items:
        if item.recommendation_id in seen:
            continue
        seen.add(item.recommendation_id)
        out.append(item)
    return out


def _governance_apply_blocked_reasons(
    recommendation: SystemGovernanceRecommendation,
) -> list[GovernanceApplyBlockedReason]:
    reasons: list[GovernanceApplyBlockedReason] = []
    if recommendation.risk_level != "low":
        reasons.append(
            GovernanceApplyBlockedReason(
                code="risk_level_not_low",
                detail=f"risk_level={recommendation.risk_level} is not eligible for automatic apply.",
            )
        )
    if recommendation.requires_human_approval:
        reasons.append(
            GovernanceApplyBlockedReason(
                code="requires_human_approval",
                detail="The recommendation requires explicit human approval outside this apply queue.",
            )
        )
    if not recommendation.can_apply:
        reasons.append(
            GovernanceApplyBlockedReason(
                code="can_apply_false",
                detail="The recommendation is advisory only and cannot be applied by NUO.",
            )
        )
    if recommendation.recommendation_id not in _SAFE_CONTEXT_MAINTENANCE_RECOMMENDATION_IDS:
        reasons.append(
            GovernanceApplyBlockedReason(
                code="unsupported_apply_action",
                detail="This first apply queue only executes safe context maintenance actions.",
            )
        )
    return reasons


def _blocked_apply_result(
    *,
    recommendation_id: str,
    risk_level: GovernanceApplyRisk,
    message: str,
    reasons: list[GovernanceApplyBlockedReason],
    action_ticket: GovernanceActionTicket | None = None,
) -> GovernanceRecommendationApplyResult:
    return GovernanceRecommendationApplyResult(
        status="blocked",
        applied=False,
        dry_run=False,
        blocked=True,
        recommendation_id=recommendation_id,
        risk_level=risk_level,
        message=message,
        blocked_reason=reasons[0].code if reasons else None,
        blocked_reasons=reasons,
        action_ticket=action_ticket,
    )


def _action_ticket_for(recommendation: SystemGovernanceRecommendation) -> GovernanceActionTicket:
    return GovernanceActionTicket(
        recommendation_id=recommendation.recommendation_id,
        finding_id=recommendation.finding_id,
        subsystem=recommendation.subsystem,
        title=recommendation.title,
        risk_level=recommendation.risk_level,
        suggested_action=recommendation.suggested_action,
        requires_human_approval=recommendation.requires_human_approval,
        apply_hint=recommendation.apply_hint,
    )


def _context_maintenance_details(report: ContextMaintenanceReport) -> dict[str, Any]:
    return {
        "tenant_id": report.tenant_id,
        "dry_run": report.dry_run,
        "total_seen": report.total_seen,
        "compressed": report.compressed,
        "soft_forgotten": report.soft_forgotten,
        "hard_deleted": report.hard_deleted,
        "duplicate_candidates": report.duplicate_candidates,
        "duplicate_merged": report.duplicate_merged,
        "compiler_review": report.compiler_review,
        "compiler_recompile_recommended": report.compiler_recompile_recommended,
        "low_value_marked": report.low_value_marked,
        "stale_or_risky_marked": report.stale_or_risky_marked,
        "kept": report.kept,
    }


__all__ = [
    "GovernanceActionTicket",
    "GovernanceApplyBlockedReason",
    "GovernanceApplyRisk",
    "GovernanceApplyStatus",
    "GovernanceRecommendationApplyResult",
    "GovernanceRisk",
    "HealthSeverity",
    "SystemGovernanceRecommendation",
    "SystemHealthFinding",
    "SystemHealthReport",
    "apply_governance_recommendation",
    "collect_system_health_report",
]
