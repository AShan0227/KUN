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
    "budget_policy",
    "world_policy",
    "delivery_review",
    "validation_tier_selected",
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


def ticket_from_context_selection(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    execution_mode: str,
    context_limit: int,
    context_pack: Any,
    mission_id: str | None = None,
    memory_policy: dict[str, Any] | None = None,
) -> DecisionTicket:
    """Wrap ContextPacker output as a V4 decision ticket."""

    items = list(getattr(context_pack, "items", []) or [])
    asset_ids = [
        str(getattr(item, "asset_id", "")) for item in items if getattr(item, "asset_id", "")
    ]
    kinds = [str(getattr(item, "asset_kind", "")) for item in items]
    scores = [float(getattr(item, "relevance_score", 0.0) or 0.0) for item in items]
    status: DecisionStatus = "selected" if asset_ids else "skipped"
    reason = (
        f"ContextPacker selected {len(asset_ids)} assets for mode={execution_mode}"
        if asset_ids
        else f"ContextPacker selected no assets for mode={execution_mode}"
    )
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="context",
        decision_point="context_selected",
        source_module="context.packer",
        selected_action=",".join(asset_ids) if asset_ids else "none",
        status=status,
        reason=reason,
        confidence=0.7 if asset_ids else 0.45,
        risk_level=risk_level,
        inputs_summary={
            "execution_mode": execution_mode,
            "context_limit": context_limit,
            "memory_policy": memory_policy or {},
        },
        evidence={
            "asset_ids": asset_ids,
            "asset_kinds": kinds,
            "relevance_scores": scores,
            "memory_policy": memory_policy or {},
        },
        metadata={
            "asset_ids": asset_ids,
            "asset_count": len(asset_ids),
            "context_limit": context_limit,
            "execution_mode": execution_mode,
            "memory_policy": memory_policy or {},
        },
    )


def ticket_from_skill_selection(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    top_k: int,
    skills: list[Any],
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap SkillSelector candidates as a V4 decision ticket."""

    skill_ids = [
        str(getattr(skill, "skill_id", "")) for skill in skills if getattr(skill, "skill_id", "")
    ]
    status: DecisionStatus = "selected" if skill_ids else "skipped"
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="skill",
        decision_point="skill_selected",
        source_module="skills.selector",
        selected_action=",".join(skill_ids) if skill_ids else "none",
        status=status,
        reason=(
            f"SkillSelector selected {len(skill_ids)} candidates"
            if skill_ids
            else "SkillSelector found no matching candidate"
        ),
        confidence=0.72 if skill_ids else 0.4,
        risk_level=risk_level,
        inputs_summary={"top_k": top_k},
        evidence={
            "skills": [
                {
                    "skill_id": str(getattr(skill, "skill_id", "")),
                    "description": str(
                        getattr(getattr(skill, "manifest", None), "description", "")
                    ),
                    "maturity": str(getattr(getattr(skill, "manifest", None), "maturity", "")),
                }
                for skill in skills
            ],
        },
        metadata={"skill_ids": skill_ids, "skill_count": len(skill_ids), "top_k": top_k},
    )


def ticket_from_budget_policy(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    level: str,
    used_usd: float,
    limit_usd: float,
    behavior: dict[str, Any],
    hard_break: bool = False,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap BudgetTracker runtime policy as a V4 decision ticket."""

    usage_ratio = used_usd / max(limit_usd, 1e-6)
    status: DecisionStatus = (
        "blocked" if hard_break else "escalated" if level in {"LOW", "CRITICAL"} else "allowed"
    )
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="watchtower",
        decision_point="budget_policy",
        source_module="engineering.budget_tracker",
        selected_action=level,
        status=status,
        reason=(
            f"Budget level {level}: used ${used_usd:.4f} of ${limit_usd:.4f} ({usage_ratio:.0%})"
        ),
        confidence=0.9,
        risk_level=risk_level,
        cost_estimate_usd=used_usd,
        inputs_summary={
            "used_usd": used_usd,
            "limit_usd": limit_usd,
            "usage_ratio": usage_ratio,
        },
        evidence={
            "level": level,
            "behavior": dict(behavior),
            "hard_break": hard_break,
        },
        metadata={
            "budget_level": level,
            "used_usd": used_usd,
            "limit_usd": limit_usd,
            "usage_ratio": usage_ratio,
            "hard_break": hard_break,
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
    route_debug: dict[str, Any] | None = None,
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
            "route_debug": route_debug or {},
        },
        metadata={
            "step_id": step_id,
            "purpose": purpose,
            "provider": provider,
            "model": model,
            "route_debug": route_debug or {},
        },
    )


def ticket_from_protocol_applied(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    estimated_cost_usd: float | None,
    protocol: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap ProtocolRegistry consume as a V4 decision ticket."""

    protocol_id = str(getattr(protocol, "protocol_id", "unknown"))
    version = str(getattr(protocol, "version", "unknown"))
    execution = getattr(protocol, "execution", None)
    mode = str(getattr(execution, "mode", "SMART"))
    status = str(getattr(protocol, "status", "unknown"))
    skill_chain = list(getattr(protocol, "skill_chain", []) or [])
    verification = list(getattr(protocol, "verification", []) or [])
    trigger = getattr(protocol, "trigger", None)
    confidence = 0.82 if status == "stable" else 0.62 if status == "canary" else 0.5
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="protocol",
        decision_point="protocol_applied",
        source_module="qi.protocol_registry",
        selected_action=f"{protocol_id}:{version}:{mode}",
        status="applied",
        reason=f"ProtocolRegistry applied {status} protocol {protocol_id}@{version}",
        confidence=confidence,
        risk_level=risk_level,
        cost_estimate_usd=estimated_cost_usd,
        inputs_summary={
            "task_id": task_id,
            "risk_level": risk_level,
            "estimated_cost_usd": estimated_cost_usd,
        },
        constraints=[
            f"task_type_pattern={getattr(trigger, 'task_type_pattern', '')}",
            f"risk_levels={','.join(_string_list(getattr(trigger, 'risk_levels', [])))}",
        ],
        evidence={
            "protocol_id": protocol_id,
            "version": version,
            "status": status,
            "execution_mode": mode,
            "expected_cost_usd": getattr(execution, "expected_cost_usd", None),
            "expected_duration_sec": getattr(execution, "expected_duration_sec", None),
            "skill_chain": [
                {
                    "skill": str(getattr(step, "skill", "")),
                    "when": str(getattr(step, "when", "")),
                    "fallback": str(getattr(step, "fallback", "")),
                }
                for step in skill_chain
            ],
            "verification": [
                {
                    "kind": str(getattr(spec, "kind", "")),
                    "required": bool(getattr(spec, "required", False)),
                }
                for spec in verification
            ],
            "reward_weights": getattr(protocol, "reward_weights", {}),
        },
        metadata={
            "protocol_id": protocol_id,
            "version": version,
            "execution_mode": mode,
            "protocol_status": status,
        },
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


def ticket_from_validation_tier(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    complexity_score: float,
    tier: str,
    execution_mode: str,
    mode_override_reason: str = "",
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap ValidationPipeline tier selection as a V4 decision ticket."""

    risk_high = risk_level in {"high", "critical"}
    complexity_high = complexity_score >= 0.5
    reason = (
        f"Validation tier {tier} selected by execution_mode={execution_mode}"
        if mode_override_reason
        else f"Validation tier {tier} selected by risk={risk_level}, "
        f"complexity={complexity_score:.2f}"
    )
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="delivery",
        decision_point="validation_tier_selected",
        source_module="engineering.validation",
        selected_action=tier,
        status="selected",
        reason=reason,
        confidence=0.78,
        risk_level=risk_level,
        inputs_summary={
            "risk_level": risk_level,
            "complexity_score": complexity_score,
            "execution_mode": execution_mode,
            "mode_override_reason": mode_override_reason,
        },
        evidence={
            "tier": tier,
            "risk_high": risk_high,
            "complexity_high": complexity_high,
            "execution_mode": execution_mode,
            "mode_override_reason": mode_override_reason,
        },
        metadata={
            "validation_tier": tier,
            "risk_level": risk_level,
            "complexity_score": complexity_score,
            "execution_mode": execution_mode,
            "mode_override_reason": mode_override_reason,
        },
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
    "ticket_from_budget_policy",
    "ticket_from_context_selection",
    "ticket_from_delivery_review",
    "ticket_from_llm_route",
    "ticket_from_protocol_applied",
    "ticket_from_route_choice",
    "ticket_from_skill_selection",
    "ticket_from_validation_tier",
    "ticket_from_value_gate_decision",
    "ticket_from_watchtower_decision",
    "ticket_from_world_policy",
]
