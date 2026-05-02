"""NUO system coordination checks.

These checks look for contradictions between KUN subsystems. A single module
can be healthy while the whole workflow is stuck: approved actions not picked
up by the executor, paused tasks with no visible gate, or quarantined handlers
still receiving pending actions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.orm import PendingActionRow, RuntimeStateRow, WorldHandlerControlRow

CoordinationSeverity = Literal["info", "warn", "error", "critical"]
RemediationKind = Literal[
    "trigger_executor",
    "reject_or_restore_handler",
    "resume_or_fail_task",
    "manual_review",
]
RemediationRisk = Literal["low", "medium", "high"]


class CoordinationRemediationPlan(BaseModel):
    """Dry-run remediation ticket for one coordination issue.

    This is deliberately a plan, not an executor.  NUO can surface it to users
    and future workers can consume it, but this module never mutates task state.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    issue_id: str
    kind: RemediationKind
    risk_level: RemediationRisk
    can_auto_execute: bool = False
    reason: str
    target_task_id: str | None = None
    target_action_id: str | None = None
    target_action_type: str | None = None
    suggested_command: str | None = None


class CoordinationIssue(BaseModel):
    """One cross-subsystem inconsistency found by NUO."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    severity: CoordinationSeverity
    subsystem: str = "coordination"
    title: str
    detail: str
    suggested_action: str
    task_id: str | None = None
    action_id: str | None = None
    action_type: str | None = None
    remediation_plan: CoordinationRemediationPlan | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


async def collect_coordination_issues(
    *,
    tenant_id: str,
    stale_after: timedelta = timedelta(minutes=5),
    limit: int = 200,
) -> list[CoordinationIssue]:
    """Collect cross-module coordination issues for one tenant."""

    now = datetime.now(UTC)
    async with session_scope(tenant_id=tenant_id) as s:
        pending_rows = list(
            (
                await s.execute(
                    select(PendingActionRow)
                    .where(
                        PendingActionRow.tenant_id == tenant_id,
                        PendingActionRow.status.in_(("pending_approval", "approved")),
                    )
                    .order_by(PendingActionRow.updated_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        runtime_rows = list(
            (
                await s.execute(
                    select(RuntimeStateRow)
                    .where(
                        RuntimeStateRow.tenant_id == tenant_id,
                        RuntimeStateRow.status == "paused",
                    )
                    .order_by(RuntimeStateRow.last_updated.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        control_rows = list(
            (
                await s.execute(
                    select(WorldHandlerControlRow).where(
                        WorldHandlerControlRow.tenant_id == tenant_id,
                        WorldHandlerControlRow.status.in_(("quarantined", "disabled")),
                    )
                )
            )
            .scalars()
            .all()
        )
    return coordination_issues_from_rows(
        pending_rows=pending_rows,
        runtime_rows=runtime_rows,
        control_rows=control_rows,
        now=now,
        stale_after=stale_after,
    )


def coordination_issues_from_rows(
    *,
    pending_rows: list[Any],
    runtime_rows: list[Any],
    control_rows: list[Any],
    now: datetime | None = None,
    stale_after: timedelta = timedelta(minutes=5),
) -> list[CoordinationIssue]:
    """Pure classifier for unit tests and NUO report generation."""

    now = now or datetime.now(UTC)
    open_actions_by_task: dict[str, list[Any]] = defaultdict(list)
    issues: list[CoordinationIssue] = []
    controls = {
        str(row.action_type): str(row.status)
        for row in control_rows
        if str(getattr(row, "status", "")) in {"quarantined", "disabled"}
    }

    for row in pending_rows:
        task_ref = str(row.task_ref)
        action_type = str(row.action_type)
        status = str(row.status)
        open_actions_by_task[task_ref].append(row)
        updated_at = _as_aware(getattr(row, "updated_at", None)) or now
        age = now - updated_at
        control_status = controls.get(action_type)
        if control_status is not None:
            issues.append(
                CoordinationIssue(
                    issue_id=f"handler_control_pending:{row.action_id}",
                    severity="error" if control_status == "disabled" else "warn",
                    title="外部动作排队，但 handler 已被傩限制",
                    detail=(
                        f"{action_type} 当前是 {control_status}，"
                        f"但动作 {row.action_id} 仍处于 {status}。"
                    ),
                    suggested_action=(
                        "先拒绝/取消这批待处理动作，或确认恢复 handler 后再重新审批。"
                    ),
                    task_id=task_ref,
                    action_id=str(row.action_id),
                    action_type=action_type,
                    evidence={"handler_control_status": control_status, "action_status": status},
                )
            )
        if status == "approved" and age >= stale_after:
            issues.append(
                CoordinationIssue(
                    issue_id=f"approved_action_stale:{row.action_id}",
                    severity="error",
                    title="外部动作已批准，但执行器没有及时处理",
                    detail=(
                        f"动作 {row.action_id} 已批准 {int(age.total_seconds())} 秒，"
                        "仍未进入 executed/cancelled。"
                    ),
                    suggested_action="检查待审批动作执行器 / cron worker，必要时手动触发执行。",
                    task_id=task_ref,
                    action_id=str(row.action_id),
                    action_type=action_type,
                    evidence={"age_sec": int(age.total_seconds()), "action_status": status},
                )
            )

    for runtime in runtime_rows:
        task_id = str(runtime.task_ref)
        blob = dict(getattr(runtime, "blob", None) or {})
        if open_actions_by_task.get(task_id):
            continue
        if _has_resume_marker(blob):
            continue
        issues.append(
            CoordinationIssue(
                issue_id=f"paused_without_gate:{task_id}",
                severity="warn",
                title="任务暂停了，但没有可见的待确认动作",
                detail=f"任务 {task_id} 是 paused，但 pending approval 队列里没有对应动作。",
                suggested_action=(
                    "检查 RuntimeState.blob 的暂停原因；如果是脏状态，让 reaper 或人工恢复/失败化。"
                ),
                task_id=task_id,
                evidence={"runtime_status": "paused", "blob_keys": sorted(blob.keys())[:20]},
            )
        )
    return _attach_remediation_plans(_dedupe_issues(issues))


def summarize_coordination_issues(items: list[CoordinationIssue]) -> dict[str, int]:
    counts: Counter[str] = Counter(str(item.severity) for item in items)
    return {
        "total": len(items),
        "info": int(counts.get("info", 0)),
        "warn": int(counts.get("warn", 0)),
        "error": int(counts.get("error", 0)),
        "critical": int(counts.get("critical", 0)),
    }


def remediation_plan_for_issue(issue: CoordinationIssue) -> CoordinationRemediationPlan:
    """Build a conservative dry-run remediation plan for one issue."""

    if issue.issue_id.startswith("approved_action_stale:"):
        low_risk = issue.action_type in {"email.draft", "local_file.write", "webhook.dry_run"}
        return CoordinationRemediationPlan(
            plan_id=f"remediate:{issue.issue_id}",
            issue_id=issue.issue_id,
            kind="trigger_executor",
            risk_level="low" if low_risk else "high",
            can_auto_execute=low_risk,
            reason=(
                "动作已经批准但执行器没有消费；低风险动作可由后台 executor 重试，"
                "真实外发/删除/支付类动作必须先人工确认执行器和 handler 状态。"
            ),
            target_task_id=issue.task_id,
            target_action_id=issue.action_id,
            target_action_type=issue.action_type,
            suggested_command="uv run python -m kun.cli world pending-actions execute-once",
        )
    if issue.issue_id.startswith("handler_control_pending:"):
        return CoordinationRemediationPlan(
            plan_id=f"remediate:{issue.issue_id}",
            issue_id=issue.issue_id,
            kind="reject_or_restore_handler",
            risk_level="high",
            can_auto_execute=False,
            reason=(
                "handler 已被禁用或隔离时仍有待处理动作；自动执行会绕过傩的安全控制，"
                "必须先拒绝动作，或人工确认恢复 handler 后重新审批。"
            ),
            target_task_id=issue.task_id,
            target_action_id=issue.action_id,
            target_action_type=issue.action_type,
        )
    if issue.issue_id.startswith("paused_without_gate:"):
        return CoordinationRemediationPlan(
            plan_id=f"remediate:{issue.issue_id}",
            issue_id=issue.issue_id,
            kind="resume_or_fail_task",
            risk_level="medium",
            can_auto_execute=False,
            reason=(
                "任务暂停但没有可见审批门，可能是脏运行态；需要 reaper 或人工查看暂停原因，"
                "再决定恢复、失败化，或补一个用户确认动作。"
            ),
            target_task_id=issue.task_id,
        )
    return CoordinationRemediationPlan(
        plan_id=f"remediate:{issue.issue_id}",
        issue_id=issue.issue_id,
        kind="manual_review",
        risk_level="medium",
        can_auto_execute=False,
        reason="未知协同问题类型，先进入人工复核，不自动执行。",
        target_task_id=issue.task_id,
        target_action_id=issue.action_id,
        target_action_type=issue.action_type,
    )


def summarize_remediation_plans(items: list[CoordinationIssue]) -> dict[str, int]:
    """Summarize remediation plan risk and automation posture."""

    plans = [item.remediation_plan for item in items if item.remediation_plan is not None]
    risk_counts: Counter[str] = Counter(str(plan.risk_level) for plan in plans)
    kind_counts: Counter[str] = Counter(str(plan.kind) for plan in plans)
    return {
        "total": len(plans),
        "auto_executable": sum(1 for plan in plans if plan.can_auto_execute),
        "manual_required": sum(1 for plan in plans if not plan.can_auto_execute),
        "low_risk": int(risk_counts.get("low", 0)),
        "medium_risk": int(risk_counts.get("medium", 0)),
        "high_risk": int(risk_counts.get("high", 0)),
        "trigger_executor": int(kind_counts.get("trigger_executor", 0)),
        "reject_or_restore_handler": int(kind_counts.get("reject_or_restore_handler", 0)),
        "resume_or_fail_task": int(kind_counts.get("resume_or_fail_task", 0)),
        "manual_review": int(kind_counts.get("manual_review", 0)),
    }


def _has_resume_marker(blob: dict[str, Any]) -> bool:
    return bool(
        blob.get("resume_request")
        or blob.get("pending_action_resume")
        or blob.get("mission_resume")
        or blob.get("resume_ready")
    )


def _as_aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _dedupe_issues(items: list[CoordinationIssue]) -> list[CoordinationIssue]:
    seen: set[str] = set()
    out: list[CoordinationIssue] = []
    for item in items:
        if item.issue_id in seen:
            continue
        seen.add(item.issue_id)
        out.append(item)
    return out


def _attach_remediation_plans(items: list[CoordinationIssue]) -> list[CoordinationIssue]:
    for item in items:
        item.remediation_plan = remediation_plan_for_issue(item)
    return items


__all__ = [
    "CoordinationIssue",
    "CoordinationRemediationPlan",
    "collect_coordination_issues",
    "coordination_issues_from_rows",
    "remediation_plan_for_issue",
    "summarize_coordination_issues",
    "summarize_remediation_plans",
]
