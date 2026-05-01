"""WorldGateway handler health cards for NUO.

This module turns handler descriptors plus real pending action history into a
plain health card.  It deliberately treats "executed but missing handler" and
"policy blocked" as non-success, so NUO does not overstate real-world ability.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from kun.core.db import session_scope
from kun.core.orm import EventRow, PendingActionRow, WorldActionExecutionRow
from kun.world.gateway import WorldGateway, WorldHandlerDescriptor, get_world_gateway
from kun.world.handler_control import WorldHandlerControl, load_world_handler_controls
from kun.world.tenant_env import env_for_tenant, missing_required_world_env

HandlerHealthStatus = Literal["ready", "limited", "blocked", "unregistered"]
SecretConfigStatus = Literal["not_required", "configured", "missing", "half_enabled"]

EXPECTED_REAL_WORLD_HANDLERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "email.send": (
        "KUN_WORLD_EMAIL_SEND_ENABLED",
        (
            "KUN_WORLD_SMTP_HOST",
            "KUN_WORLD_SMTP_FROM",
            "KUN_WORLD_EMAIL_ALLOWED_DOMAINS",
        ),
    ),
    "enterprise_api.post": (
        "KUN_WORLD_API_POST_ENABLED",
        ("KUN_WORLD_API_ALLOWED_HOSTS",),
    ),
    "browser.execute": (
        "KUN_WORLD_BROWSER_EXECUTE_ENABLED",
        ("KUN_WORLD_BROWSER_ALLOWED_HOSTS",),
    ),
}


class WorldHandlerEventStats(BaseModel):
    """Recent EventRow-derived signal for one WorldGateway action type."""

    model_config = ConfigDict(extra="forbid")

    total_events: int = 0
    failure_events: int = 0
    exception_events: int = 0
    blocked_events: int = 0
    latest_failure_event_type: str = ""
    latest_failure_at: Any | None = None
    event_types: dict[str, int] = Field(default_factory=dict)


class WorldHandlerDiagnostics(BaseModel):
    """Explicit NUO diagnostic flags for a WorldGateway handler."""

    model_config = ConfigDict(extra="forbid")

    missing_compensation_description: bool = False
    real_external_or_high_risk: bool = False
    missing_tenant_key_config: bool = False
    has_failure_or_exception_events: bool = False
    failure_event_count: int = 0
    exception_event_count: int = 0
    blocked_event_count: int = 0


class WorldHandlerHealthCard(BaseModel):
    """NUO-facing health card for one WorldGateway action type."""

    model_config = ConfigDict(extra="forbid")

    action_type: str
    handler_id: str = ""
    status: HandlerHealthStatus
    mode: str = ""
    external_dispatched: bool = False
    registered: bool = False
    configured: bool = False
    requires_human_approval: bool = True
    has_compensation: bool = False
    static_risk: Literal["low", "medium", "high"] = "medium"
    dynamic_risk: Literal["low", "medium", "high"] = "low"
    risk_score: float = 0.0
    risk_flags: list[str] = Field(default_factory=list)
    secret_config_status: SecretConfigStatus = "not_required"
    diagnostics: WorldHandlerDiagnostics = Field(default_factory=WorldHandlerDiagnostics)
    event_stats: WorldHandlerEventStats = Field(default_factory=WorldHandlerEventStats)
    total_seen: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    executed_count: int = 0
    failed_count: int = 0
    missing_handler_count: int = 0
    policy_blocked_count: int = 0
    success_rate: float = 0.0
    failure_rate: float = 0.0
    approval_reject_rate: float = 0.0
    compensation_strategy: str = ""
    control_status: Literal["enabled", "quarantined", "disabled"] = "enabled"
    control_reason: str = ""
    recommendation: str
    issues: list[str] = Field(default_factory=list)
    missing_env_vars: list[str] = Field(default_factory=list)
    setup_steps: list[str] = Field(default_factory=list)


async def collect_world_handler_health(
    *,
    tenant_id: str,
    gateway: WorldGateway | None = None,
    history_limit: int = 500,
) -> list[WorldHandlerHealthCard]:
    """Collect handler health from registry + tenant-scoped action history."""
    async with session_scope(tenant_id=tenant_id) as s:
        result = await s.execute(
            # Newest rows are most useful for health.  We only need a bounded
            # recent window so the NUO panel stays light.
            select(PendingActionRow)
            .where(PendingActionRow.tenant_id == tenant_id)
            .order_by(PendingActionRow.updated_at.desc())
            .limit(history_limit)
        )
        rows = list(result.scalars().all())
        execution_result = await s.execute(
            select(WorldActionExecutionRow)
            .where(WorldActionExecutionRow.tenant_id == tenant_id)
            .order_by(WorldActionExecutionRow.updated_at.desc())
            .limit(history_limit)
        )
        executions = list(execution_result.scalars().all())
        event_result = await s.execute(
            select(EventRow)
            .where(
                EventRow.tenant_id == tenant_id,
                or_(
                    EventRow.event_type.like("task.pending_action.%"),
                    EventRow.event_type.like("world.%"),
                    EventRow.event_type.like("world_gateway.%"),
                ),
            )
            .order_by(EventRow.occurred_at.desc())
            .limit(history_limit)
        )
        events = list(event_result.scalars().all())
        controls = await load_world_handler_controls(s, tenant_id=tenant_id)
    return build_world_handler_health(
        descriptors=(gateway or get_world_gateway()).handler_descriptors(),
        rows=rows,
        executions=executions,
        events=events,
        tenant_id=tenant_id,
        controls=controls,
    )


def build_world_handler_health(
    *,
    descriptors: list[WorldHandlerDescriptor],
    rows: list[PendingActionRow],
    executions: list[WorldActionExecutionRow] | None = None,
    events: list[EventRow] | None = None,
    tenant_id: str = "",
    controls: dict[str, WorldHandlerControl] | None = None,
) -> list[WorldHandlerHealthCard]:
    descriptor_by_type = {item.action_type: item for item in descriptors}
    execution_rows = executions or []
    event_rows = events or []
    action_types = (
        set(descriptor_by_type)
        | {row.action_type for row in rows}
        | {row.action_type for row in execution_rows}
        | {_event_action_type(row) for row in event_rows if _event_action_type(row)}
        | set(EXPECTED_REAL_WORLD_HANDLERS)
        | set(controls or {})
    )
    cards = [
        _build_card(
            action_type,
            descriptor_by_type.get(action_type),
            rows,
            execution_rows,
            event_rows,
            tenant_id=tenant_id,
            control=(controls or {}).get(action_type),
        )
        for action_type in sorted(action_types)
    ]
    cards.sort(key=lambda item: (_status_rank(item.status), -item.failed_count, item.action_type))
    return cards


def _build_card(
    action_type: str,
    descriptor: WorldHandlerDescriptor | None,
    rows: list[PendingActionRow],
    executions: list[WorldActionExecutionRow],
    events: list[EventRow],
    *,
    tenant_id: str = "",
    control: WorldHandlerControl | None = None,
) -> WorldHandlerHealthCard:
    relevant = [row for row in rows if row.action_type == action_type]
    relevant_executions = [row for row in executions if row.action_type == action_type]
    event_stats = _event_stats(action_type, events)
    effective_tenant_id = tenant_id or (
        relevant[0].tenant_id
        if relevant
        else (relevant_executions[0].tenant_id if relevant_executions else "")
    )
    total = max(len(relevant), len(relevant_executions))
    approved = sum(1 for row in relevant if row.status == "approved")
    rejected = sum(1 for row in relevant if row.status == "rejected")
    missing = (
        sum(1 for row in relevant_executions if row.requires_handler)
        if relevant_executions
        else sum(1 for row in relevant if _gateway_payload(row).get("requires_handler") is True)
    )
    policy_blocked = (
        sum(1 for row in relevant_executions if row.gateway_mode == "policy_blocked")
        if relevant_executions
        else sum(
            1 for row in relevant if _gateway_payload(row).get("gateway_mode") == "policy_blocked"
        )
    )
    failed = (
        sum(1 for row in relevant_executions if _execution_failed(row))
        if relevant_executions
        else sum(1 for row in relevant if _legacy_failure(row))
    )
    executed_success = (
        sum(1 for row in relevant_executions if _execution_success(row))
        if relevant_executions
        else sum(1 for row in relevant if _row_success(row))
    )
    denominator = max(1, total)
    reject_rate = rejected / denominator
    failure_rate = failed / denominator
    success_rate = executed_success / denominator

    issues: list[str] = []
    if control is not None and control.status in {"quarantined", "disabled"}:
        label = "隔离" if control.status == "quarantined" else "禁用"
        issues.append(f"傩已持久化{label}这个 handler: {control.reason or '未填写原因'}")
    config_issues = _expected_config_issues(action_type, tenant_id=effective_tenant_id)
    if descriptor is None:
        issues.append("没有注册 WorldGateway handler")
        issues.extend(config_issues)
    else:
        if descriptor.external_dispatched:
            issues.append("真实外发风险高：会影响外部系统，必须人工确认和审计")
            if not descriptor.requires_external_dispatch_confirmation:
                issues.append("真实外发 handler 没声明二次外发确认")
            if not descriptor.permissions_required:
                issues.append("真实外发 handler 没声明权限要求")
            issues.extend(config_issues)
        if not _has_clear_compensation(descriptor.compensation_strategy):
            issues.append("补偿策略不清楚")
    if missing:
        issues.append(f"最近 {missing} 次没有 handler")
    if policy_blocked:
        issues.append(f"最近 {policy_blocked} 次被策略拦截")
    if failed:
        issues.append(f"最近 {failed} 次执行失败")
    if event_stats.failure_events or event_stats.exception_events:
        issues.append(
            "EventRow 记录到失败/异常事件: "
            f"failure={event_stats.failure_events}, exception={event_stats.exception_events}"
        )
    if total >= 3 and failure_rate >= 0.25:
        issues.append(f"失败率高 ({failure_rate:.0%})，不要继续自动执行")
    elif total >= 3 and failure_rate >= 0.1:
        issues.append(f"失败率偏高 ({failure_rate:.0%})，需要复盘 handler 或上游动作生成")
    if reject_rate >= 0.3 and total >= 3:
        issues.append("审批拒绝率偏高，可能生成动作质量不够")

    static_risk = _static_risk(descriptor)
    dynamic_risk = _dynamic_risk(failure_rate=failure_rate, reject_rate=reject_rate)
    secret_config_status = _secret_config_status(action_type, tenant_id=effective_tenant_id)
    risk_flags = _risk_flags(
        descriptor=descriptor,
        configured=not config_issues,
        has_compensation=False
        if descriptor is None
        else _has_clear_compensation(descriptor.compensation_strategy),
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
        failure_rate=failure_rate,
        missing_handler_count=missing,
        policy_blocked_count=policy_blocked,
        event_stats=event_stats,
        control=control,
        secret_config_status=secret_config_status,
    )
    risk_score = _risk_score(
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
        risk_flags=risk_flags,
    )
    status = _status(
        descriptor=descriptor,
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
        issues=issues,
        control=control,
    )
    missing_env_vars = _expected_missing_env_vars(action_type, tenant_id=effective_tenant_id)
    has_compensation = (
        False if descriptor is None else _has_clear_compensation(descriptor.compensation_strategy)
    )
    diagnostics = _diagnostics(
        descriptor=descriptor,
        static_risk=static_risk,
        secret_config_status=secret_config_status,
        has_compensation=has_compensation,
        event_stats=event_stats,
    )
    setup_steps = _setup_steps(
        action_type=action_type,
        descriptor=descriptor,
        control=control,
        missing_env_vars=missing_env_vars,
        issues=issues,
        failure_rate=failure_rate,
        reject_rate=reject_rate,
    )
    return WorldHandlerHealthCard(
        action_type=action_type,
        handler_id=descriptor.handler_id if descriptor else "",
        status=status,
        mode=descriptor.mode if descriptor else "",
        external_dispatched=bool(descriptor and descriptor.external_dispatched),
        registered=descriptor is not None,
        configured=descriptor is not None and not config_issues,
        requires_human_approval=True
        if descriptor is None
        else bool(descriptor.permissions_required or descriptor.external_dispatched),
        has_compensation=has_compensation,
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
        risk_score=risk_score,
        risk_flags=risk_flags,
        secret_config_status=secret_config_status,
        diagnostics=diagnostics,
        event_stats=event_stats,
        total_seen=total,
        approved_count=approved,
        rejected_count=rejected,
        executed_count=executed_success,
        failed_count=failed,
        missing_handler_count=missing,
        policy_blocked_count=policy_blocked,
        success_rate=round(success_rate, 4),
        failure_rate=round(failure_rate, 4),
        approval_reject_rate=round(reject_rate, 4),
        compensation_strategy=descriptor.compensation_strategy if descriptor else "",
        control_status=control.status if control else "enabled",
        control_reason=control.reason if control else "",
        recommendation=_recommendation(status, issues, descriptor, control),
        issues=issues,
        missing_env_vars=missing_env_vars,
        setup_steps=setup_steps,
    )


def summarize_handler_health(cards: list[WorldHandlerHealthCard]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    counts["total"] = len(cards)
    for card in cards:
        counts[card.status] += 1
        if card.external_dispatched:
            counts["external_dispatched"] += 1
        if card.external_dispatched and not card.has_compensation:
            counts["missing_compensation"] += 1
        if card.external_dispatched and not card.configured:
            counts["missing_external_config"] += 1
        if card.static_risk == "high":
            counts["high_static_risk"] += 1
        if card.dynamic_risk == "high":
            counts["high_dynamic_risk"] += 1
        if card.failed_count > 0:
            counts["recent_failures"] += 1
        if card.event_stats.failure_events > 0 or card.event_stats.exception_events > 0:
            counts["failure_or_exception_events"] += 1
            counts["failure_events"] += card.event_stats.failure_events
            counts["exception_events"] += card.event_stats.exception_events
        if card.event_stats.blocked_events > 0:
            counts["blocked_events"] += card.event_stats.blocked_events
        if card.missing_handler_count > 0:
            counts["missing_handler"] += 1
        if card.policy_blocked_count > 0:
            counts["policy_blocked"] += 1
        if card.risk_score >= 0.8:
            counts["critical_handler_risk"] += 1
        for flag in card.risk_flags:
            counts[f"risk_flag:{flag}"] += 1
    return dict(counts)


def _gateway_payload(row: PendingActionRow) -> dict[str, Any]:
    executor = row.payload.get("executor")
    if not isinstance(executor, dict):
        return {}
    gateway = executor.get("gateway")
    return dict(gateway) if isinstance(gateway, dict) else {}


def _row_success(row: PendingActionRow) -> bool:
    gateway = _gateway_payload(row)
    if row.status != "executed":
        return False
    if gateway.get("requires_handler") is True:
        return False
    if gateway.get("gateway_mode") == "policy_blocked":
        return False
    return gateway.get("capability_status") in {
        "supported_execute",
        "supported_draft",
        "supported_dry_run",
        "supported_plan",
    }


def _row_failed(row: PendingActionRow) -> bool:
    executor = row.payload.get("executor")
    if row.status == "cancelled":
        return True
    return isinstance(executor, dict) and executor.get("status") == "failed"


def _legacy_failure(row: PendingActionRow) -> bool:
    gateway = _gateway_payload(row)
    return (
        _row_failed(row)
        or gateway.get("requires_handler") is True
        or gateway.get("gateway_mode") == "policy_blocked"
    )


def _execution_success(row: WorldActionExecutionRow) -> bool:
    if row.status != "executed":
        return False
    if row.requires_handler:
        return False
    if row.gateway_mode == "policy_blocked":
        return False
    return row.capability_status in {
        "supported_execute",
        "supported_draft",
        "supported_dry_run",
        "supported_plan",
    }


def _execution_failed(row: WorldActionExecutionRow) -> bool:
    return row.status in {"blocked", "failed", "cancelled"}


def _has_clear_compensation(strategy: str) -> bool:
    compact = strategy.strip()
    if not compact:
        return False
    vague = ("需要人工确认补偿方式", "人工确认补偿", "TBD", "todo")
    return not any(item.lower() in compact.lower() for item in vague)


def _static_risk(descriptor: WorldHandlerDescriptor | None) -> Literal["low", "medium", "high"]:
    if descriptor is None:
        return "medium"
    if descriptor.external_dispatched:
        return "high"
    if descriptor.mode == "execute":
        return "medium"
    return "low"


def _dynamic_risk(*, failure_rate: float, reject_rate: float) -> Literal["low", "medium", "high"]:
    if failure_rate >= 0.25 or reject_rate >= 0.5:
        return "high"
    if failure_rate >= 0.1 or reject_rate >= 0.3:
        return "medium"
    return "low"


def _secret_config_status(action_type: str, *, tenant_id: str = "") -> SecretConfigStatus:
    expected = EXPECTED_REAL_WORLD_HANDLERS.get(action_type)
    if expected is None:
        return "not_required"
    enable_env, required_envs = expected
    enabled = _env_truthy(env_for_tenant(tenant_id, enable_env))
    missing_required = missing_required_world_env(required_envs, tenant_id=tenant_id)
    present_required = [
        name for name in required_envs if _env_present_for_tenant(name, tenant_id=tenant_id)
    ]
    if enabled and not missing_required:
        return "configured"
    if (not enabled and present_required) or (enabled and missing_required):
        return "half_enabled"
    return "missing"


def _risk_flags(
    *,
    descriptor: WorldHandlerDescriptor | None,
    configured: bool,
    has_compensation: bool,
    static_risk: str,
    dynamic_risk: str,
    failure_rate: float,
    missing_handler_count: int,
    policy_blocked_count: int,
    event_stats: WorldHandlerEventStats,
    control: WorldHandlerControl | None,
    secret_config_status: SecretConfigStatus,
) -> list[str]:
    flags: list[str] = []
    if descriptor is None:
        flags.append("unregistered")
    elif descriptor.external_dispatched:
        flags.append("external_dispatch")
        if not descriptor.requires_external_dispatch_confirmation:
            flags.append("missing_external_confirmation")
        if not descriptor.permissions_required:
            flags.append("missing_permissions")
    if not configured:
        flags.append("missing_config")
    if secret_config_status == "missing":
        flags.append("missing_secret_config")
    elif secret_config_status == "half_enabled":
        flags.append("half_enabled_secret_config")
    if descriptor is not None and descriptor.external_dispatched and not has_compensation:
        flags.append("missing_compensation")
    if static_risk == "high":
        flags.append("high_static_risk")
    if dynamic_risk == "high":
        flags.append("high_dynamic_risk")
    elif dynamic_risk == "medium":
        flags.append("medium_dynamic_risk")
    if failure_rate >= 0.25:
        flags.append("high_failure_rate")
    elif failure_rate >= 0.1:
        flags.append("elevated_failure_rate")
    if missing_handler_count > 0:
        flags.append("missing_handler")
    if policy_blocked_count > 0:
        flags.append("policy_blocked")
    if event_stats.failure_events > 0 or event_stats.exception_events > 0:
        flags.append("failure_or_exception_events")
    if event_stats.exception_events > 0:
        flags.append("exception_events")
    if control is not None and control.status in {"quarantined", "disabled"}:
        flags.append(f"control_{control.status}")
    return list(dict.fromkeys(flags))


def _risk_score(
    *,
    static_risk: str,
    dynamic_risk: str,
    risk_flags: list[str],
) -> float:
    score = {"low": 0.1, "medium": 0.35, "high": 0.6}.get(static_risk, 0.35)
    score += {"low": 0.0, "medium": 0.15, "high": 0.3}.get(dynamic_risk, 0.0)
    weights = {
        "external_dispatch": 0.08,
        "missing_external_confirmation": 0.08,
        "missing_permissions": 0.08,
        "missing_config": 0.12,
        "missing_secret_config": 0.12,
        "half_enabled_secret_config": 0.16,
        "missing_compensation": 0.16,
        "high_failure_rate": 0.16,
        "elevated_failure_rate": 0.08,
        "missing_handler": 0.12,
        "policy_blocked": 0.1,
        "failure_or_exception_events": 0.08,
        "exception_events": 0.08,
        "control_quarantined": 0.25,
        "control_disabled": 0.3,
    }
    score += sum(weights.get(flag, 0.0) for flag in risk_flags)
    return round(min(1.0, score), 4)


def _status(
    *,
    descriptor: WorldHandlerDescriptor | None,
    static_risk: str,
    dynamic_risk: str,
    issues: list[str],
    control: WorldHandlerControl | None = None,
) -> HandlerHealthStatus:
    if control is not None and control.status in {"quarantined", "disabled"}:
        return "blocked"
    if descriptor is None:
        return "unregistered"
    if dynamic_risk == "high":
        return "blocked"
    if static_risk == "high" or dynamic_risk == "medium" or issues:
        return "limited"
    return "ready"


def _recommendation(
    status: HandlerHealthStatus,
    issues: list[str],
    descriptor: WorldHandlerDescriptor | None,
    control: WorldHandlerControl | None = None,
) -> str:
    if control is not None and control.status in {"quarantined", "disabled"}:
        return "先通过 NUO restore 恢复 handler；恢复前所有真实外发都会被拦截。"
    if status == "unregistered":
        if issues:
            return "先补 handler 或配置缺失环境变量；未补齐前不要执行这种外部动作。"
        return "不要执行这种外部动作；先补 handler 或改成草稿/dry-run。"
    if status == "blocked":
        return "暂停自动执行，必须人工确认并排查失败原因。"
    if status == "limited":
        if descriptor and descriptor.external_dispatched:
            return "保留人工确认；不要自动外发；先补齐补偿和失败复盘。"
        return "可继续使用，但傩要持续观察这些问题：" + "；".join(issues[:3])
    return "可正常使用；保持审计和抽样复查。"


def _status_rank(status: HandlerHealthStatus) -> int:
    return {"blocked": 0, "unregistered": 1, "limited": 2, "ready": 3}[status]


def _expected_config_issues(action_type: str, *, tenant_id: str = "") -> list[str]:
    expected = EXPECTED_REAL_WORLD_HANDLERS.get(action_type)
    if expected is None:
        return []
    enable_env, required_envs = expected
    issues: list[str] = []
    enabled = _env_truthy(env_for_tenant(tenant_id, enable_env))
    if not enabled:
        present_required = [
            name for name in required_envs if _env_present_for_tenant(name, tenant_id=tenant_id)
        ]
        if present_required:
            issues.append(
                f"真实外发半启用：已配置 {', '.join(present_required)}，但未启用 {enable_env}=true"
            )
        else:
            issues.append(f"未启用 {enable_env}=true")
    missing = missing_required_world_env(required_envs, tenant_id=tenant_id)
    if missing:
        prefix = "真实外发半启用：" if enabled else ""
        if tenant_id:
            issues.append(
                prefix
                + "缺少全局或租户级环境变量: "
                + ", ".join(missing)
                + f" (tenant={tenant_id})"
            )
        else:
            issues.append(prefix + "缺少全局或任意租户级环境变量: " + ", ".join(missing))
    return issues


def _expected_missing_env_vars(action_type: str, *, tenant_id: str = "") -> list[str]:
    expected = EXPECTED_REAL_WORLD_HANDLERS.get(action_type)
    if expected is None:
        return []
    enable_env, required_envs = expected
    missing: list[str] = []
    if not _env_truthy(env_for_tenant(tenant_id, enable_env)):
        missing.append(enable_env)
    missing.extend(missing_required_world_env(required_envs, tenant_id=tenant_id))
    return missing


def _diagnostics(
    *,
    descriptor: WorldHandlerDescriptor | None,
    static_risk: str,
    secret_config_status: SecretConfigStatus,
    has_compensation: bool,
    event_stats: WorldHandlerEventStats,
) -> WorldHandlerDiagnostics:
    return WorldHandlerDiagnostics(
        missing_compensation_description=descriptor is not None and not has_compensation,
        real_external_or_high_risk=bool(descriptor and descriptor.external_dispatched)
        or static_risk == "high",
        missing_tenant_key_config=secret_config_status in {"missing", "half_enabled"},
        has_failure_or_exception_events=bool(
            event_stats.failure_events or event_stats.exception_events
        ),
        failure_event_count=event_stats.failure_events,
        exception_event_count=event_stats.exception_events,
        blocked_event_count=event_stats.blocked_events,
    )


def _event_stats(action_type: str, events: list[EventRow]) -> WorldHandlerEventStats:
    relevant = [row for row in events if _event_matches_action(row, action_type)]
    event_types: dict[str, int] = defaultdict(int)
    failure_events = 0
    exception_events = 0
    blocked_events = 0
    latest_failure: EventRow | None = None
    for row in relevant:
        event_types[row.event_type] += 1
        failed = _event_is_failure(row)
        exceptional = _event_is_exception(row)
        if failed:
            failure_events += 1
        if exceptional:
            exception_events += 1
        if _event_is_blocked(row):
            blocked_events += 1
        if (failed or exceptional) and (
            latest_failure is None or row.occurred_at > latest_failure.occurred_at
        ):
            latest_failure = row
    return WorldHandlerEventStats(
        total_events=len(relevant),
        failure_events=failure_events,
        exception_events=exception_events,
        blocked_events=blocked_events,
        latest_failure_event_type=latest_failure.event_type if latest_failure else "",
        latest_failure_at=latest_failure.occurred_at if latest_failure else None,
        event_types=dict(sorted(event_types.items())),
    )


def _event_matches_action(row: EventRow, action_type: str) -> bool:
    event_action_type = _event_action_type(row)
    if event_action_type:
        return event_action_type == action_type
    return action_type in row.subject


def _event_action_type(row: EventRow) -> str:
    payload = row.payload if isinstance(row.payload, dict) else {}
    candidates = [
        payload.get("action_type"),
        payload.get("world_action_type"),
        _nested_payload_value(payload, "world_action", "action_type"),
        _nested_payload_value(payload, "gateway", "action_type"),
        _nested_payload_value(payload, "decision_ticket", "action_type"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _nested_payload_value(payload: dict[str, Any], key: str, nested_key: str) -> Any:
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return None
    return nested.get(nested_key)


def _event_is_failure(row: EventRow) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    event_type = row.event_type.lower()
    status = str(payload.get("status") or payload.get("action_status") or "").lower()
    capability = str(payload.get("capability_status") or "").lower()
    return (
        "failed" in event_type
        or "failure" in event_type
        or "error" in event_type
        or status in {"failed", "cancelled"}
        or capability == "preview_failed"
        or bool(payload.get("error"))
    )


def _event_is_exception(row: EventRow) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    if "exception" in row.event_type.lower():
        return True
    for key, value in payload.items():
        key_text = str(key).lower()
        value_text = str(value).lower()
        if "exception" in key_text or "traceback" in key_text:
            return True
        if "exception" in value_text or "traceback" in value_text:
            return True
    return False


def _event_is_blocked(row: EventRow) -> bool:
    payload = row.payload if isinstance(row.payload, dict) else {}
    return (
        "blocked" in row.event_type.lower()
        or str(payload.get("status") or "").lower() == "blocked"
        or str(payload.get("executor_mode") or "").lower() == "policy_blocked"
    )


def _setup_steps(
    *,
    action_type: str,
    descriptor: WorldHandlerDescriptor | None,
    control: WorldHandlerControl | None,
    missing_env_vars: list[str],
    issues: list[str],
    failure_rate: float,
    reject_rate: float,
) -> list[str]:
    steps: list[str] = []
    if control is not None and control.status in {"quarantined", "disabled"}:
        steps.append("先在傩里恢复 handler，或拒绝/取消相关待处理动作。")
    if missing_env_vars:
        steps.append("补齐配置：" + ", ".join(missing_env_vars))
        steps.append("如果写的是环境变量，重启 API；如果写的是 secret-store，刷新傩体检。")
    if descriptor is None and action_type in EXPECTED_REAL_WORLD_HANDLERS:
        steps.append("确认 WorldGateway 注册表出现这个 action_type 后，再允许真实外发。")
    if descriptor is not None and descriptor.external_dispatched:
        steps.append("保留人工审批和二次外发确认，不要直接自动发送/调用。")
    if any("补偿策略不清楚" in issue for issue in issues):
        steps.append("补清楚失败后的补偿办法，例如更正邮件、撤销接口或人工回滚流程。")
    if failure_rate >= 0.1 or reject_rate >= 0.3:
        steps.append("先复盘最近失败/拒绝样本，再决定是否恢复自动化。")
    return _dedupe_steps(steps)


def _dedupe_steps(steps: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for step in steps:
        compact = step.strip()
        if not compact or compact in seen:
            continue
        seen.add(compact)
        deduped.append(compact)
    return deduped


def _env_present_for_tenant(env_name: str, *, tenant_id: str = "") -> bool:
    if tenant_id:
        return env_for_tenant(tenant_id, env_name) is not None
    return env_for_tenant("", env_name) is not None


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "EXPECTED_REAL_WORLD_HANDLERS",
    "HandlerHealthStatus",
    "WorldHandlerDiagnostics",
    "WorldHandlerEventStats",
    "WorldHandlerHealthCard",
    "build_world_handler_health",
    "collect_world_handler_health",
    "summarize_handler_health",
]
