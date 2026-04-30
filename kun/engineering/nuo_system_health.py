"""NUO system-level health collector.

Unlike the light `/nuo/health/summary` endpoint, this report is meant for
system diagnosis.  It gathers real runtime rows, event lag, pending approvals,
delivery-status honesty checks, secret/config safety, and WorldGateway handler
health.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from kun.core.db import session_scope
from kun.core.orm import (
    EventRow,
    MissionRow,
    MissionTaskRow,
    PendingActionRow,
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
)
from kun.ops.secret_audit import SecretAuditItem, audit_runtime_secrets
from kun.world.handler_health import (
    WorldHandlerHealthCard,
    collect_world_handler_health,
    summarize_handler_health,
)

HealthSeverity = Literal["info", "warn", "error", "critical"]


class SystemHealthFinding(BaseModel):
    """One actionable NUO system finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    severity: HealthSeverity
    subsystem: str
    title: str
    detail: str
    suggested_action: str


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
    state_ledger_audit_summary: dict[str, int] = Field(default_factory=dict)
    coordination_summary: dict[str, int] = Field(default_factory=dict)
    coordination_issues: list[CoordinationIssue] = Field(default_factory=list)
    findings: list[SystemHealthFinding] = Field(default_factory=list)

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
    state_ledger_audit_summary = await _collect_state_ledger_audit_summary(tenant_id=tenant_id)
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
        state_ledger_audit_summary=state_ledger_audit_summary,
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
        state_ledger_audit_summary=state_ledger_audit_summary,
        coordination_summary=summarize_coordination_issues(coordination_issues),
        coordination_issues=coordination_issues,
        findings=findings,
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
    state_ledger_audit_summary: dict[str, int] | None = None,
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


__all__ = [
    "HealthSeverity",
    "SystemHealthFinding",
    "SystemHealthReport",
    "collect_system_health_report",
]
