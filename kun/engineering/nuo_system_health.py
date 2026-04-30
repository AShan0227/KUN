"""NUO system-level health collector.

Unlike the light `/nuo/health/summary` endpoint, this report is meant for
system diagnosis.  It gathers real runtime rows, event lag, pending approvals,
delivery-status honesty checks, secret/config safety, and WorldGateway handler
health.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from kun.core.db import session_scope
from kun.core.orm import EventRow, PendingActionRow, RuntimeStateRow, TaskRow
from kun.engineering.concurrency import scan_active_resource_conflicts
from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
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
    active_resource_conflicts: int = 0
    delivery_status_issues: list[str] = Field(default_factory=list)
    secret_audit_summary: dict[str, int] = Field(default_factory=dict)
    secret_audit_items: list[SecretAuditItem] = Field(default_factory=list)
    world_handler_summary: dict[str, int] = Field(default_factory=dict)
    world_handlers: list[WorldHandlerHealthCard] = Field(default_factory=list)
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
        active_resource_conflicts = len(
            await scan_active_resource_conflicts(s, tenant_id=tenant_id)
        )

    delivery_issues = validate_delivery_status(get_v3_delivery_status())
    secret_audit = audit_runtime_secrets()
    world_handlers = await collect_world_handler_health(tenant_id=tenant_id)
    findings = _findings(
        outbox_lag=outbox_lag,
        pending_approvals=pending_approvals,
        stale_runtime_count=stale_runtime_count,
        active_resource_conflicts=active_resource_conflicts,
        delivery_issues=delivery_issues,
        secret_audit_items=secret_audit.items,
        world_handlers=world_handlers,
    )
    return SystemHealthReport(
        tenant_id=tenant_id,
        generated_at=now,
        total_tasks=total_tasks,
        runtime_by_status=runtime_by_status,
        outbox_lag=outbox_lag,
        pending_approvals=pending_approvals,
        stale_runtime_count=stale_runtime_count,
        active_resource_conflicts=active_resource_conflicts,
        delivery_status_issues=delivery_issues,
        secret_audit_summary=secret_audit.summary,
        secret_audit_items=secret_audit.items,
        world_handler_summary=summarize_handler_health(world_handlers),
        world_handlers=world_handlers,
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
    return findings


__all__ = [
    "HealthSeverity",
    "SystemHealthFinding",
    "SystemHealthReport",
    "collect_system_health_report",
]
