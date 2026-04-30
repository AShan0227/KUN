"""NUO automatic quarantine recommendations for WorldGateway handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kun.core.db import session_scope
from kun.world.handler_control import set_world_handler_control
from kun.world.handler_health import WorldHandlerHealthCard, collect_world_handler_health


class WorldHandlerAutoControlDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str
    recommended_status: str = "enabled"
    applied: bool = False
    can_auto_apply: bool = False
    requires_human_confirmation: bool = True
    risk_level: str = "medium"
    data_quality: str = "partial"
    reason: str = ""
    risk_summary: dict[str, object] = Field(default_factory=dict)
    evidence: dict[str, object] = Field(default_factory=dict)


class WorldHandlerAutoControlReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    dry_run: bool = True
    decisions: list[WorldHandlerAutoControlDecision] = Field(default_factory=list)
    applied_count: int = 0


async def run_world_handler_auto_quarantine(
    *,
    tenant_id: str,
    dry_run: bool = True,
    min_seen: int = 3,
    failure_threshold: float = 0.25,
    cards: list[WorldHandlerHealthCard] | None = None,
) -> WorldHandlerAutoControlReport:
    """Recommend or apply persistent quarantine for unsafe handlers."""

    health_cards = (
        cards if cards is not None else await collect_world_handler_health(tenant_id=tenant_id)
    )
    decisions = [
        decision
        for card in health_cards
        if (
            decision := _decision_for_card(
                card, min_seen=min_seen, failure_threshold=failure_threshold
            )
        )
        is not None
    ]
    applied = 0
    if not dry_run and decisions:
        async with session_scope(tenant_id=tenant_id) as s:
            for decision in decisions:
                if not decision.can_auto_apply:
                    continue
                await set_world_handler_control(
                    s,
                    tenant_id=tenant_id,
                    action_type=decision.action_type,
                    status="quarantined",
                    reason=decision.reason,
                    source="nuo.auto_quarantine",
                    metadata=decision.evidence,
                )
                decision.applied = True
                applied += 1
    return WorldHandlerAutoControlReport(
        tenant_id=tenant_id,
        dry_run=dry_run,
        decisions=decisions,
        applied_count=applied,
    )


def _decision_for_card(
    card: WorldHandlerHealthCard,
    *,
    min_seen: int,
    failure_threshold: float,
) -> WorldHandlerAutoControlDecision | None:
    if card.control_status in {"quarantined", "disabled"}:
        return None
    reasons: list[str] = []
    if card.registered and card.total_seen >= min_seen and card.failure_rate >= failure_threshold:
        reasons.append(f"失败率 {card.failure_rate:.0%} 超过阈值 {failure_threshold:.0%}")
    if card.external_dispatched and not card.has_compensation:
        reasons.append("真实外发 handler 缺少清晰补偿策略")
    if card.external_dispatched and not card.configured:
        reasons.append("真实外发 handler 配置不完整")
    if card.status == "blocked" and card.registered and card.total_seen >= min_seen:
        reasons.append("NUO 当前体检状态为 blocked")
    if not reasons:
        return None
    risk_level = _decision_risk_level(card)
    can_auto_apply = risk_level == "low"
    recommended_status = "quarantined" if can_auto_apply else "review_required"
    if not can_auto_apply:
        reasons.append("这类问题需要你确认，傩只提醒，不会自动关掉")
    return WorldHandlerAutoControlDecision(
        action_type=card.action_type,
        recommended_status=recommended_status,
        can_auto_apply=can_auto_apply,
        requires_human_confirmation=not can_auto_apply,
        risk_level=risk_level,
        data_quality=_data_quality(card),
        reason="；".join(dict.fromkeys(reasons)),
        risk_summary=_risk_summary(card),
        evidence={
            "status": card.status,
            "total_seen": card.total_seen,
            "failure_rate": card.failure_rate,
            "configured": card.configured,
            "external_dispatched": card.external_dispatched,
            "has_compensation": card.has_compensation,
            "issues": card.issues,
        },
    )


def _decision_risk_level(card: WorldHandlerHealthCard) -> str:
    if card.external_dispatched or card.static_risk == "high":
        return "high"
    if card.dynamic_risk == "high" or card.static_risk == "medium":
        return "medium"
    return "low"


def _data_quality(card: WorldHandlerHealthCard) -> str:
    return "complete" if card.total_seen > 0 else "partial"


def _risk_summary(card: WorldHandlerHealthCard) -> dict[str, object]:
    return {
        "failure_rate": card.failure_rate,
        "failure_rate_status": "complete" if card.total_seen > 0 else "partial",
        "missing_compensation": not card.has_compensation,
        "external_dispatch_risk": card.external_dispatched,
        "missing_secrets": not card.configured,
        "missing_handler_count": card.missing_handler_count,
        "policy_blocked_count": card.policy_blocked_count,
        "total_seen": card.total_seen,
    }


__all__ = [
    "WorldHandlerAutoControlDecision",
    "WorldHandlerAutoControlReport",
    "run_world_handler_auto_quarantine",
]
