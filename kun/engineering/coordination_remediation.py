"""Safe NUO coordination remediation runner.

NUO can already detect cross-module coordination problems. This module is the
small bridge that lets those findings become a controlled remediation pass:
dry-run by default, and only explicitly enabled low-risk plans may call the
side-effect executor.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.action_executor import ActionExecutionResult, execute_approved_action_once
from kun.engineering.system_coordination import (
    CoordinationIssue,
    collect_coordination_issues,
)

RemediationMode = Literal["dry_run", "auto_low_risk"]


class CoordinationRemediationAttempt(BaseModel):
    """One remediation decision made by NUO."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    plan_id: str | None = None
    kind: str = "manual_review"
    risk_level: str = "medium"
    action_id: str | None = None
    action_type: str | None = None
    decision: Literal["planned", "executed", "noop", "skipped", "blocked"] = "planned"
    reason: str
    execution: dict[str, object] | None = None


class CoordinationRemediationReport(BaseModel):
    """Summary from one coordination remediation pass."""

    model_config = ConfigDict(extra="forbid")

    mode: RemediationMode
    issues: int = 0
    planned: int = 0
    executed: int = 0
    noop: int = 0
    skipped: int = 0
    blocked: int = 0
    production_action: bool = False
    attempts: list[CoordinationRemediationAttempt] = Field(default_factory=list)


async def run_coordination_remediation(
    *,
    tenant_id: str,
    mode: RemediationMode | None = None,
    stale_after: timedelta = timedelta(minutes=5),
    limit: int = 50,
) -> CoordinationRemediationReport:
    """Run one safe coordination remediation pass.

    Default mode is dry-run. Set ``KUN_COORDINATION_REMEDIATION_MODE=auto_low_risk``
    to let NUO trigger approved low-risk actions that were already allowed by
    the human/approval gate. Real external, high-risk, or ambiguous actions stay
    manual.
    """

    selected_mode = mode or configured_coordination_remediation_mode()
    issues = await collect_coordination_issues(
        tenant_id=tenant_id,
        stale_after=stale_after,
        limit=limit,
    )
    attempts: list[CoordinationRemediationAttempt] = []
    for issue in issues:
        attempt = await _attempt_issue(
            tenant_id=tenant_id,
            issue=issue,
            mode=selected_mode,
        )
        attempts.append(attempt)
    return _summarize_report(mode=selected_mode, issues=len(issues), attempts=attempts)


def configured_coordination_remediation_mode() -> RemediationMode:
    raw = os.getenv("KUN_COORDINATION_REMEDIATION_MODE", "dry_run").strip().lower()
    if raw == "auto_low_risk":
        return "auto_low_risk"
    return "dry_run"


async def _attempt_issue(
    *,
    tenant_id: str,
    issue: CoordinationIssue,
    mode: RemediationMode,
) -> CoordinationRemediationAttempt:
    plan = issue.remediation_plan
    if plan is None:
        return CoordinationRemediationAttempt(
            issue_id=issue.issue_id,
            decision="skipped",
            reason="没有明确处置计划，保留给人工复核。",
        )
    base = {
        "issue_id": issue.issue_id,
        "plan_id": plan.plan_id,
        "kind": plan.kind,
        "risk_level": plan.risk_level,
        "action_id": plan.target_action_id,
        "action_type": plan.target_action_type,
    }
    if plan.kind != "trigger_executor":
        return CoordinationRemediationAttempt(
            **base,
            decision="skipped",
            reason="这类问题不能由后台自动执行，必须人工判断。",
        )
    if not plan.can_auto_execute or plan.risk_level != "low":
        return CoordinationRemediationAttempt(
            **base,
            decision="blocked",
            reason="不是低风险自动处置计划，高风险或真实外发必须人工确认。",
        )
    if not plan.target_action_id:
        return CoordinationRemediationAttempt(
            **base,
            decision="skipped",
            reason="缺少 action_id，无法安全幂等执行。",
        )
    if mode == "dry_run":
        return CoordinationRemediationAttempt(
            **base,
            decision="planned",
            reason="dry-run：已识别为可自动处理的低风险动作，但当前只演练不执行。",
        )

    result = await execute_approved_action_once(
        tenant_id=tenant_id,
        action_id=plan.target_action_id,
    )
    if result is None:
        return CoordinationRemediationAttempt(
            **base,
            decision="noop",
            reason="执行器没有拿到 approved 动作，可能已被其他 worker 处理。",
        )
    return CoordinationRemediationAttempt(
        **base,
        decision="executed" if result.action_status == "executed" else "blocked",
        reason=result.message,
        execution=_execution_payload(result),
    )


def _execution_payload(result: ActionExecutionResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": result.action_id,
        "task_ref": result.task_ref,
        "action_status": result.action_status,
        "message": result.message,
    }
    if result.task_status is not None:
        payload["task_status"] = result.task_status
    if result.gateway_result is not None:
        payload["gateway_mode"] = result.gateway_result.gateway_mode
        payload["external_dispatched"] = result.gateway_result.external_dispatched
        payload["capability_status"] = result.gateway_result.capability_status
    return payload


def _summarize_report(
    *,
    mode: RemediationMode,
    issues: int,
    attempts: list[CoordinationRemediationAttempt],
) -> CoordinationRemediationReport:
    return CoordinationRemediationReport(
        mode=mode,
        issues=issues,
        planned=sum(1 for item in attempts if item.decision == "planned"),
        executed=sum(1 for item in attempts if item.decision == "executed"),
        noop=sum(1 for item in attempts if item.decision == "noop"),
        skipped=sum(1 for item in attempts if item.decision == "skipped"),
        blocked=sum(1 for item in attempts if item.decision == "blocked"),
        production_action=mode == "auto_low_risk"
        and any(item.decision == "executed" for item in attempts),
        attempts=attempts,
    )


__all__ = [
    "CoordinationRemediationAttempt",
    "CoordinationRemediationReport",
    "configured_coordination_remediation_mode",
    "run_coordination_remediation",
]
