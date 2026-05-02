"""Read-only NUO system governance audit.

This module looks across decision tickets, delivery status, WorldGateway
handler health and scheduler/lane pressure.  It is intentionally an audit
surface only: it never mutates tasks, handlers, context, memory or policy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.delivery_status import DeliveryCapability
from kun.world.handler_health import WorldHandlerHealthCard

GovernanceAuditSeverity = Literal["info", "warn", "error", "critical"]
GovernanceAuditCategory = Literal[
    "decision_coverage",
    "decision_conflict",
    "world_gateway",
    "scheduler",
    "delivery_status",
]

_EXECUTION_MODES = {"FAST", "SMART", "MAX", "ENSEMBLE"}
_BLOCKING_STATUSES = {"blocked", "stopped", "escalated", "failed"}
_TICKET_REQUIRED_EVENT_TYPES = {
    "llm.model_select.consulted",
    "llm.model_select.blocked",
    "llm.route_change.proposed",
    "nuo.governance.recommendation.decided",
}


class SystemGovernanceAuditIssue(BaseModel):
    """One read-only cross-system governance issue surfaced by NUO."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    severity: GovernanceAuditSeverity
    category: GovernanceAuditCategory
    title: str
    detail: str
    suggested_action: str
    task_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class SystemGovernanceAuditReport(BaseModel):
    """Compact governance report that NUO can show independently or in health."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    review_only: bool = True
    summary: dict[str, int] = Field(default_factory=dict)
    issues: list[SystemGovernanceAuditIssue] = Field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return any(item.severity in {"error", "critical"} for item in self.issues)


def run_system_governance_audit(
    *,
    tenant_id: str,
    decision_event_samples: Sequence[Mapping[str, Any]] | None = None,
    world_handlers: list[WorldHandlerHealthCard] | None = None,
    scheduler_summary: dict[str, int] | None = None,
    scheduler_limits: dict[str, int] | None = None,
    delivery_items: list[DeliveryCapability] | None = None,
    delivery_validation_issues: list[str] | None = None,
) -> SystemGovernanceAuditReport:
    """Build a read-only system governance audit report from existing signals."""

    issues: list[SystemGovernanceAuditIssue] = []
    issues.extend(_decision_ticket_coverage_issues(decision_event_samples or []))
    issues.extend(_decision_conflict_issues(decision_event_samples or []))
    issues.extend(_world_handler_governance_issues(world_handlers or []))
    issues.extend(
        _scheduler_governance_issues(
            scheduler_summary=scheduler_summary or {},
            scheduler_limits=scheduler_limits or {},
            world_handlers=world_handlers or [],
        )
    )
    issues.extend(
        _delivery_status_governance_issues(
            delivery_items=delivery_items or [],
            validation_issues=delivery_validation_issues or [],
        )
    )
    issues = _dedupe_issues(issues)
    return SystemGovernanceAuditReport(
        tenant_id=tenant_id,
        summary=_summary(issues),
        issues=issues,
    )


def _decision_ticket_coverage_issues(
    event_samples: Sequence[Mapping[str, Any]],
) -> list[SystemGovernanceAuditIssue]:
    issues: list[SystemGovernanceAuditIssue] = []
    for sample in event_samples:
        event_type = _text(sample.get("event_type"))
        if event_type not in _TICKET_REQUIRED_EVENT_TYPES:
            continue
        payload = _mapping(sample.get("payload"))
        if _decision_ticket_from_payload(payload):
            continue
        task_id = _text(sample.get("task_ref")) or _text(payload.get("task_id"))
        severity: GovernanceAuditSeverity = "error" if event_type.endswith(".blocked") else "warn"
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id=f"decision_missing_ticket:{event_type}:{task_id or 'unknown'}",
                severity=severity,
                category="decision_coverage",
                title="关键决策事件缺少统一 DecisionTicket",
                detail=(
                    f"{event_type} 没有携带 decision_ticket。傩能看到事件，"
                    "但 StateLedger、启和 resource credit 无法稳定追踪这次判断。"
                ),
                suggested_action=(
                    "让该判断点生成统一 DecisionTicket，或在 RuleEngine 事件 payload "
                    "里带上 decision_ticket 字段。"
                ),
                task_id=task_id or None,
                evidence={"event_type": event_type, "payload_keys": sorted(payload.keys())},
            )
        )
    return issues


def _decision_conflict_issues(
    event_samples: Sequence[Mapping[str, Any]],
) -> list[SystemGovernanceAuditIssue]:
    tickets_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in event_samples:
        payload = _mapping(sample.get("payload"))
        ticket = _decision_ticket_from_payload(payload)
        if not ticket:
            continue
        task_id = _text(ticket.get("task_id")) or _text(sample.get("task_ref"))
        if not task_id:
            continue
        tickets_by_task[task_id].append(ticket)

    issues: list[SystemGovernanceAuditIssue] = []
    for task_id, tickets in tickets_by_task.items():
        mode_by_source: dict[str, str] = {}
        blocking_sources: list[str] = []
        delivery_allows = False
        for ticket in tickets:
            source = _ticket_source(ticket)
            mode = _ticket_execution_mode(ticket)
            if mode:
                mode_by_source[source] = mode
            status = _text(ticket.get("status"))
            decision_point = _text(ticket.get("decision_point"))
            if status in _BLOCKING_STATUSES:
                blocking_sources.append(f"{source}:{status}")
            if decision_point == "delivery_review" and status == "allowed":
                delivery_allows = True

        modes = sorted(set(mode_by_source.values()))
        if len(modes) >= 2:
            issues.append(
                SystemGovernanceAuditIssue(
                    issue_id=f"decision_mode_conflict:{task_id}",
                    severity="warn",
                    category="decision_conflict",
                    title="同一个任务出现多个执行档位信号",
                    detail=(
                        f"任务 {task_id} 的决策票据里同时出现 {', '.join(modes)}。"
                        "这不一定是 bug，但需要确认是有意的动态切路，而不是模块各判各的。"
                    ),
                    suggested_action=(
                        "检查该任务的 DecisionTicket / StateLedger：如果是动态切路，保留原因；"
                        "如果不是，把 ExecutionMode、ProtocolRegistry、Watchtower 和 TaskRouter "
                        "统一到同一张决策票据上。"
                    ),
                    task_id=task_id,
                    evidence={"mode_by_source": mode_by_source},
                )
            )
        if blocking_sources and delivery_allows:
            issues.append(
                SystemGovernanceAuditIssue(
                    issue_id=f"decision_blocked_then_delivered:{task_id}",
                    severity="error",
                    category="decision_conflict",
                    title="任务曾被拦截，但后续交付放行",
                    detail=(
                        f"任务 {task_id} 有 blocking 决策 {blocking_sources}，"
                        "但 PreDeliverGate/交付票据后续显示 allowed。"
                    ),
                    suggested_action=(
                        "确认 blocking 决策是否已被明确解除；没有解除记录时，不要把交付结果当作可信完成。"
                    ),
                    task_id=task_id,
                    evidence={"blocking_sources": blocking_sources},
                )
            )
    return issues


def _world_handler_governance_issues(
    cards: list[WorldHandlerHealthCard],
) -> list[SystemGovernanceAuditIssue]:
    issues: list[SystemGovernanceAuditIssue] = []
    for card in cards:
        flags = set(card.risk_flags)
        if card.external_dispatched and (
            card.diagnostics.missing_compensation_description or "missing_compensation" in flags
        ):
            issues.append(
                SystemGovernanceAuditIssue(
                    issue_id=f"world_missing_compensation:{card.action_type}",
                    severity="error",
                    category="world_gateway",
                    title="真实外部动作缺少清楚补偿策略",
                    detail=(f"{card.action_type} 会影响外部世界，但补偿策略为空或过于模糊。"),
                    suggested_action=(
                        "补清楚补偿/回滚描述；补齐前只允许 draft/dry-run 或强制人工审批。"
                    ),
                    evidence={
                        "action_type": card.action_type,
                        "handler_id": card.handler_id,
                        "compensation_strategy": card.compensation_strategy,
                    },
                )
            )
        if card.secret_config_status in {"missing", "half_enabled"}:
            severity: GovernanceAuditSeverity = (
                "error" if card.secret_config_status == "half_enabled" else "warn"
            )
            issues.append(
                SystemGovernanceAuditIssue(
                    issue_id=f"world_secret_config:{card.action_type}",
                    severity=severity,
                    category="world_gateway",
                    title="WorldGateway handler 密钥/白名单配置不完整",
                    detail=(
                        f"{card.action_type} 的配置状态是 {card.secret_config_status}，"
                        f"缺失项：{', '.join(card.missing_env_vars) or '未列出'}。"
                    ),
                    suggested_action=(
                        "在傩的密钥/权限入口补齐租户级配置；半开启状态下不要真实外发。"
                    ),
                    evidence={
                        "action_type": card.action_type,
                        "secret_config_status": card.secret_config_status,
                        "missing_env_vars": card.missing_env_vars,
                    },
                )
            )
        if card.external_dispatched and not card.requires_human_approval:
            issues.append(
                SystemGovernanceAuditIssue(
                    issue_id=f"world_missing_approval_gate:{card.action_type}",
                    severity="critical",
                    category="world_gateway",
                    title="真实外发 handler 缺少人工确认门",
                    detail=f"{card.action_type} 标记为真实外发，但没有 requires_human_approval。",
                    suggested_action="把该 handler 立刻降级为 draft/dry-run，或补强制审批门。",
                    evidence={"action_type": card.action_type, "handler_id": card.handler_id},
                )
            )
    return issues


def _scheduler_governance_issues(
    *,
    scheduler_summary: dict[str, int],
    scheduler_limits: dict[str, int],
    world_handlers: list[WorldHandlerHealthCard],
) -> list[SystemGovernanceAuditIssue]:
    issues: list[SystemGovernanceAuditIssue] = []
    missing_required = int(scheduler_summary.get("missing_required_lanes", 0) or 0)
    pressure = int(scheduler_summary.get("lanes_over_pressure_threshold", 0) or 0)
    if missing_required > 0:
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id="scheduler_missing_required_lanes",
                severity="error",
                category="scheduler",
                title="执行车道配置不完整",
                detail=f"缺少 {missing_required} 个必需 lane，后台治理/外部动作/高风险审批可能互相抢资源。",
                suggested_action="补齐 fast/mission/qi/nuo/world/high_risk 的限流配置。",
                evidence={
                    "scheduler_summary": scheduler_summary,
                    "scheduler_limits": scheduler_limits,
                },
            )
        )
    if pressure > 0:
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id="scheduler_lane_pressure",
                severity="warn",
                category="scheduler",
                title="执行车道有积压压力",
                detail=f"{pressure} 条 lane 超过压力阈值，简单任务可能被复杂任务拖慢。",
                suggested_action="检查 worker 是否接入统一 lane scheduler；必要时扩容对应 lane 或降低后台任务频率。",
                evidence={
                    "scheduler_summary": scheduler_summary,
                    "scheduler_limits": scheduler_limits,
                },
            )
        )
    has_real_world_handler = any(card.external_dispatched for card in world_handlers)
    if has_real_world_handler and not int(scheduler_limits.get("world", 0) or 0):
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id="scheduler_missing_world_lane_for_handlers",
                severity="error",
                category="scheduler",
                title="存在真实外部 handler，但没有 world lane",
                detail="WorldGateway 真实外部动作需要独立车道，否则会和普通任务/高风险审批互相干扰。",
                suggested_action="给 world lane 配置独立并发上限，并让外部动作 executor 走该 lane。",
                evidence={"scheduler_limits": scheduler_limits},
            )
        )
    return issues


def _delivery_status_governance_issues(
    *,
    delivery_items: list[DeliveryCapability],
    validation_issues: list[str],
) -> list[SystemGovernanceAuditIssue]:
    issues: list[SystemGovernanceAuditIssue] = []
    for issue in validation_issues:
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id=f"delivery_validation:{_stable_suffix(issue)}",
                severity="warn",
                category="delivery_status",
                title="能力状态标注存在误导风险",
                detail=issue,
                suggested_action="修正能力状态，或补齐主流程调用和机器证据后再标 ready。",
                evidence={"validation_issue": issue},
            )
        )

    public_incomplete = [
        item.capability_id
        for item in delivery_items
        if item.user_visible and item.status in {"partial", "audit_only", "not_ready"}
    ]
    if public_incomplete:
        issues.append(
            SystemGovernanceAuditIssue(
                issue_id="delivery_public_incomplete_capabilities",
                severity="info",
                category="delivery_status",
                title="用户可见能力仍有半闭环项",
                detail=(
                    f"{len(public_incomplete)} 个用户可见能力还不是 ready。"
                    "这不是错误，但发布/演示时不能说成全部完成。"
                ),
                suggested_action="继续保持交付状态诚实；前端只展示用户能理解的边界，不把 partial 当 ready。",
                evidence={"capability_ids": public_incomplete[:50]},
            )
        )
    return issues


def _decision_ticket_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("decision_ticket")
    if isinstance(nested, Mapping):
        return dict(nested)
    if payload.get("ticket_id") and payload.get("decision_point"):
        return dict(payload)
    return {}


def _ticket_execution_mode(ticket: Mapping[str, Any]) -> str:
    for source in (
        _mapping(ticket.get("metadata")),
        _mapping(ticket.get("evidence")),
        _mapping(ticket.get("policy_result")),
    ):
        mode = _mode_or_empty(source.get("execution_mode"))
        if mode:
            return mode
    selected = _text(ticket.get("selected_action"))
    for part in reversed(selected.replace("/", ":").split(":")):
        mode = _mode_or_empty(part)
        if mode:
            return mode
    return ""


def _ticket_source(ticket: Mapping[str, Any]) -> str:
    source = _text(ticket.get("source_module")) or "unknown"
    point = _text(ticket.get("decision_point")) or "decision"
    return f"{source}.{point}"


def _mode_or_empty(value: Any) -> str:
    text = _text(value).upper()
    return text if text in _EXECUTION_MODES else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stable_suffix(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _summary(issues: list[SystemGovernanceAuditIssue]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    counts["total"] = len(issues)
    for issue in issues:
        counts[issue.severity] += 1
        counts[f"category:{issue.category}"] += 1
    return dict(counts)


def _dedupe_issues(
    issues: list[SystemGovernanceAuditIssue],
) -> list[SystemGovernanceAuditIssue]:
    seen: set[str] = set()
    out: list[SystemGovernanceAuditIssue] = []
    for issue in issues:
        if issue.issue_id in seen:
            continue
        seen.add(issue.issue_id)
        out.append(issue)
    return out


__all__ = [
    "SystemGovernanceAuditIssue",
    "SystemGovernanceAuditReport",
    "run_system_governance_audit",
]
