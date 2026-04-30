"""V4 Decision Ticket.

DecisionTicket is the shared envelope for important KUN choices.  It does not
replace WatchtowerDecision, ValueGateDecision, router decisions, or
WorldGateway policy decisions.  It wraps them so StateLedger, MemoryWriteback,
NUO, and Qi can all see one traceable decision shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id

DecisionPhase = Literal[
    "intake",
    "protocol",
    "watchtower",
    "planning",
    "routing",
    "context",
    "skill",
    "step",
    "world",
    "delivery",
    "memory",
    "qi",
    "nuo",
]

DecisionPoint = Literal[
    "protocol_applied",
    "strategy_selected",
    "role_model_selected",
    "llm_model_selected",
    "context_selected",
    "skill_selected",
    "value_gate",
    "world_policy",
    "delivery_review",
    "memory_writeback",
    "qi_experiment",
    "nuo_diagnosis",
]

DecisionStatus = Literal[
    "selected",
    "applied",
    "allowed",
    "blocked",
    "skipped",
    "stopped",
    "escalated",
    "needs_review",
    "failed",
]


class DecisionTicketRef(BaseModel):
    """Small reference safe to store in event payloads or memory metadata."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    decision_point: DecisionPoint
    phase: DecisionPhase
    selected_action: str
    status: DecisionStatus
    reason: str = ""


class DecisionTicket(BaseModel):
    """Traceable envelope for one important system decision."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(default_factory=lambda: new_id("decision"))
    tenant_id: str
    task_id: str
    mission_id: str | None = None
    phase: DecisionPhase
    decision_point: DecisionPoint
    source_module: str
    selected_action: str
    status: DecisionStatus = "selected"
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    risk_level: str = "low"
    cost_estimate_usd: float | None = None
    alternatives: list[str] = Field(default_factory=list)
    inputs_summary: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    policy_result: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def ref(self) -> DecisionTicketRef:
        return DecisionTicketRef(
            ticket_id=self.ticket_id,
            decision_point=self.decision_point,
            phase=self.phase,
            selected_action=self.selected_action,
            status=self.status,
            reason=self.reason,
        )

    def event_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def ticket_from_watchtower_decision(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    estimated_cost_usd: float | None,
    decision: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap an existing WatchtowerDecision without changing its API."""

    strategy_pack_id = str(getattr(decision, "strategy_pack_id", "default"))
    execution_mode = str(getattr(decision, "execution_mode", "SMART"))
    reason = str(getattr(decision, "reason", ""))
    confidence = _float_or_default(getattr(decision, "confidence", 0.5), 0.5)
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="watchtower",
        decision_point="strategy_selected",
        source_module="watchtower.decision_plane",
        selected_action=f"{strategy_pack_id}:{execution_mode}",
        status="applied",
        reason=reason,
        confidence=confidence,
        risk_level=risk_level,
        cost_estimate_usd=estimated_cost_usd,
        inputs_summary={
            "task_id": task_id,
            "risk_level": risk_level,
            "estimated_cost_usd": estimated_cost_usd,
        },
        constraints=_string_list(getattr(decision, "risk_watch", [])),
        evidence={
            "watchtower_decision": _model_dump_or_value(decision),
            "strategy_pack_id": strategy_pack_id,
            "strategy_pack_name": str(getattr(decision, "strategy_pack_name", "")),
            "execution_mode": execution_mode,
            "context_limit": getattr(decision, "context_limit", None),
            "skill_hints": _string_list(getattr(decision, "skill_hints", [])),
            "metric_dimensions": _string_list(getattr(decision, "metric_dimensions", [])),
            "reward_weights": getattr(decision, "reward_weights", {}),
            "alert_flags": _string_list(getattr(decision, "alert_flags", [])),
        },
        metadata={
            "strategy_pack_id": strategy_pack_id,
            "execution_mode": execution_mode,
        },
    )


def ticket_from_value_gate_decision(
    *,
    tenant_id: str,
    task_id: str,
    step_id: int,
    decision: Any,
    risk_level: str = "low",
) -> DecisionTicket:
    action = str(getattr(decision, "decision", "continue"))
    status: DecisionStatus = {
        "continue": "allowed",
        "skip": "skipped",
        "stop": "stopped",
        "escalate": "escalated",
    }.get(action, "selected")  # type: ignore[assignment]
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        phase="step",
        decision_point="value_gate",
        source_module="watchtower.value_gate",
        selected_action=action,
        status=status,
        reason=str(getattr(decision, "reason", "")),
        confidence=_float_or_default(getattr(decision, "expected_value", 0.5), 0.5),
        risk_level=risk_level,
        cost_estimate_usd=None,
        inputs_summary={"step_id": step_id},
        evidence=_model_dump_or_value(decision),
        metadata={"step_id": step_id},
    )


def ticket_from_route_choice(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    estimated_cost_usd: float | None,
    choice: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap TaskRouter choice as a V4 decision ticket."""

    role_template_id = str(getattr(choice, "role_template_id", "rt-default"))
    purpose = str(getattr(choice, "purpose", "execution"))
    profile = getattr(choice, "task_profile", None)
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="routing",
        decision_point="role_model_selected",
        source_module="brain.task_router",
        selected_action=f"{role_template_id}:{purpose}",
        status="selected",
        reason=f"TaskRouter selected role_template={role_template_id}, purpose={purpose}",
        confidence=0.65,
        risk_level=risk_level,
        cost_estimate_usd=estimated_cost_usd,
        inputs_summary={
            "task_id": task_id,
            "risk_level": risk_level,
            "estimated_cost_usd": estimated_cost_usd,
        },
        evidence={
            "role_template_id": role_template_id,
            "purpose": purpose,
            "task_profile": _model_dump_or_value(profile),
        },
        metadata={
            "role_template_id": role_template_id,
            "purpose": purpose,
        },
    )


def ticket_from_llm_route(
    *,
    tenant_id: str,
    task_id: str,
    step_id: int,
    purpose: str,
    provider: str,
    model: str,
    tier: str,
    cost_usd: float,
    risk_level: str = "low",
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap the actual LLM provider/model selected for one execution step."""

    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="step",
        decision_point="llm_model_selected",
        source_module="interface.llm.router",
        selected_action=f"{provider}:{model}:{tier}",
        status="applied",
        reason=f"purpose={purpose}; actual_provider={provider}; tier={tier}",
        confidence=0.75,
        risk_level=risk_level,
        cost_estimate_usd=cost_usd,
        inputs_summary={"step_id": step_id, "purpose": purpose},
        evidence={
            "provider": provider,
            "model": model,
            "tier": tier,
            "cost_usd_equivalent": cost_usd,
        },
        metadata={"step_id": step_id, "purpose": purpose, "provider": provider, "model": model},
    )


def ticket_from_delivery_review(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    verdict: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap PreDeliverGate verdict as a V4 decision ticket."""

    final_status = str(getattr(verdict, "final_status", "done"))
    passed = bool(getattr(verdict, "passed", False))
    status: DecisionStatus = (
        "allowed"
        if passed and final_status == "done"
        else "needs_review"
        if final_status == "needs_review"
        else "failed"
    )
    checks = list(getattr(verdict, "checks", []) or [])
    failed_checks = [check for check in checks if not bool(getattr(check, "passed", False))]
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="delivery",
        decision_point="delivery_review",
        source_module="engineering.pre_deliver_gate",
        selected_action=final_status,
        status=status,
        reason=str(getattr(verdict, "reason_summary", "")),
        confidence=0.85 if passed else 0.55,
        risk_level=risk_level,
        inputs_summary={
            "check_count": len(checks),
            "fail_count": len(failed_checks),
        },
        evidence={
            "passed": passed,
            "final_status": final_status,
            "checks": [
                {
                    "name": str(getattr(check, "name", "")),
                    "passed": bool(getattr(check, "passed", False)),
                    "severity": str(getattr(check, "severity", "")),
                    "reason": str(getattr(check, "reason", "")),
                }
                for check in checks
            ],
        },
        metadata={"final_status": final_status, "passed": passed},
    )


def ticket_from_world_policy(
    *,
    tenant_id: str,
    task_id: str,
    action_id: str,
    action_type: str,
    risk_level: str,
    gateway_mode: str,
    external_dispatched: bool,
    requires_handler: bool,
    policy: dict[str, Any] | None = None,
    reason: str = "",
) -> DecisionTicket:
    allowed = not requires_handler and gateway_mode != "policy_blocked"
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        phase="world",
        decision_point="world_policy",
        source_module="world.gateway",
        selected_action=f"{action_type}:{gateway_mode}",
        status="allowed" if allowed else "blocked",
        reason=reason or gateway_mode,
        confidence=0.8 if allowed else 0.55,
        risk_level=risk_level,
        inputs_summary={"action_id": action_id, "action_type": action_type},
        evidence={
            "gateway_mode": gateway_mode,
            "external_dispatched": external_dispatched,
            "requires_handler": requires_handler,
        },
        policy_result=policy or {},
        metadata={"action_id": action_id, "action_type": action_type},
    )


def _model_dump_or_value(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "DecisionPhase",
    "DecisionPoint",
    "DecisionStatus",
    "DecisionTicket",
    "DecisionTicketRef",
    "ticket_from_delivery_review",
    "ticket_from_llm_route",
    "ticket_from_route_choice",
    "ticket_from_value_gate_decision",
    "ticket_from_watchtower_decision",
    "ticket_from_world_policy",
]
