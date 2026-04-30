"""WorldGateway handler health cards for NUO.

This module turns handler descriptors plus real pending action history into a
plain health card.  It deliberately treats "executed but missing handler" and
"policy blocked" as non-success, so NUO does not overstate real-world ability.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.orm import PendingActionRow
from kun.world.gateway import WorldGateway, WorldHandlerDescriptor, get_world_gateway

HandlerHealthStatus = Literal["ready", "limited", "blocked", "unregistered"]


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
    recommendation: str
    issues: list[str] = Field(default_factory=list)


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
    return build_world_handler_health(
        descriptors=(gateway or get_world_gateway()).handler_descriptors(),
        rows=rows,
    )


def build_world_handler_health(
    *,
    descriptors: list[WorldHandlerDescriptor],
    rows: list[PendingActionRow],
) -> list[WorldHandlerHealthCard]:
    descriptor_by_type = {item.action_type: item for item in descriptors}
    action_types = set(descriptor_by_type) | {row.action_type for row in rows}
    cards = [
        _build_card(action_type, descriptor_by_type.get(action_type), rows)
        for action_type in sorted(action_types)
    ]
    cards.sort(key=lambda item: (_status_rank(item.status), -item.failed_count, item.action_type))
    return cards


def _build_card(
    action_type: str,
    descriptor: WorldHandlerDescriptor | None,
    rows: list[PendingActionRow],
) -> WorldHandlerHealthCard:
    relevant = [row for row in rows if row.action_type == action_type]
    total = len(relevant)
    approved = sum(1 for row in relevant if row.status == "approved")
    rejected = sum(1 for row in relevant if row.status == "rejected")
    failed = sum(1 for row in relevant if _row_failed(row))
    missing = sum(1 for row in relevant if _gateway_payload(row).get("requires_handler") is True)
    policy_blocked = sum(
        1 for row in relevant if _gateway_payload(row).get("gateway_mode") == "policy_blocked"
    )
    executed_success = sum(1 for row in relevant if _row_success(row))
    denominator = max(1, total)
    reject_rate = rejected / denominator
    failure_rate = (failed + missing + policy_blocked) / denominator
    success_rate = executed_success / denominator

    issues: list[str] = []
    if descriptor is None:
        issues.append("没有注册 WorldGateway handler")
    else:
        if descriptor.external_dispatched and descriptor.requires_external_dispatch_confirmation:
            issues.append("真实外发动作必须保留人工确认")
        if not _has_clear_compensation(descriptor.compensation_strategy):
            issues.append("补偿策略不清楚")
        if descriptor.external_dispatched and descriptor.mode == "execute":
            issues.append("真实外发 handler 需要持续审计")
    if missing:
        issues.append(f"最近 {missing} 次没有 handler")
    if policy_blocked:
        issues.append(f"最近 {policy_blocked} 次被策略拦截")
    if failed:
        issues.append(f"最近 {failed} 次执行失败")
    if reject_rate >= 0.3 and total >= 3:
        issues.append("审批拒绝率偏高，可能生成动作质量不够")

    static_risk = _static_risk(descriptor)
    dynamic_risk = _dynamic_risk(failure_rate=failure_rate, reject_rate=reject_rate)
    status = _status(
        descriptor=descriptor,
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
        issues=issues,
    )
    return WorldHandlerHealthCard(
        action_type=action_type,
        handler_id=descriptor.handler_id if descriptor else "",
        status=status,
        mode=descriptor.mode if descriptor else "",
        external_dispatched=bool(descriptor and descriptor.external_dispatched),
        registered=descriptor is not None,
        configured=descriptor is not None,
        requires_human_approval=True
        if descriptor is None
        else bool(descriptor.permissions_required or descriptor.external_dispatched),
        has_compensation=False
        if descriptor is None
        else _has_clear_compensation(descriptor.compensation_strategy),
        static_risk=static_risk,
        dynamic_risk=dynamic_risk,
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
        recommendation=_recommendation(status, issues, descriptor),
        issues=issues,
    )


def summarize_handler_health(cards: list[WorldHandlerHealthCard]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for card in cards:
        counts[card.status] += 1
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


def _status(
    *,
    descriptor: WorldHandlerDescriptor | None,
    static_risk: str,
    dynamic_risk: str,
    issues: list[str],
) -> HandlerHealthStatus:
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
) -> str:
    if status == "unregistered":
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


__all__ = [
    "HandlerHealthStatus",
    "WorldHandlerHealthCard",
    "build_world_handler_health",
    "collect_world_handler_health",
    "summarize_handler_health",
]
