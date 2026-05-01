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
    "preflight_guard",
    "proactive_tool_dispatch",
    "strategy_selected",
    "emergent_switch",
    "execution_mode_selected",
    "role_model_selected",
    "llm_model_selected",
    "context_selected",
    "memory_policy_selected",
    "skill_selected",
    "step_action_selected",
    "anti_gaming_detected",
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


def ticket_from_execution_mode_selection(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    execution_mode: str,
    task_type: str,
    complexity_score: float,
    estimated_cost_usd: float | None,
    mode_override_reason: str = "",
    active_protocol: Any | None = None,
    watchtower_decision: Any | None = None,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Record the final sparse execution depth decision.

    Watchtower/protocol tickets explain their own subsystem choices. This ticket
    is the single "what mode did the task actually run with" receipt, so later
    memory/Qi/NUO credit does not need to infer it from scattered fields.
    """

    protocol_id = str(getattr(active_protocol, "protocol_id", "") or "")
    protocol_version = str(getattr(active_protocol, "version", "") or "")
    watchtower_pack = str(getattr(watchtower_decision, "strategy_pack_id", "") or "")
    watchtower_source = str(getattr(watchtower_decision, "source", "") or "")
    watchtower_reason = str(getattr(watchtower_decision, "reason", "") or "")
    confidence = _float_or_default(getattr(watchtower_decision, "confidence", None), 0.58)
    if active_protocol is not None and confidence < 0.72:
        confidence = 0.72

    source = "default"
    if protocol_id:
        source = "protocol"
    if watchtower_pack:
        source = "watchtower" if watchtower_source != "protocol" else "protocol"
    if mode_override_reason:
        source = "override"

    reason_parts = [f"final execution_mode={execution_mode}", f"source={source}"]
    if protocol_id:
        reason_parts.append(f"protocol={protocol_id}@{protocol_version or 'unknown'}")
    if watchtower_pack:
        reason_parts.append(f"strategy_pack={watchtower_pack}")
    if mode_override_reason:
        reason_parts.append(f"override={mode_override_reason}")
    elif watchtower_reason:
        reason_parts.append(watchtower_reason)

    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="watchtower",
        decision_point="execution_mode_selected",
        source_module="watchtower.execution_mode_policy",
        selected_action=f"{source}:{execution_mode}",
        status="applied",
        reason="; ".join(reason_parts),
        confidence=max(0.0, min(1.0, confidence)),
        risk_level=risk_level,
        cost_estimate_usd=estimated_cost_usd,
        alternatives=["FAST", "SMART", "MAX", "ENSEMBLE"],
        inputs_summary={
            "task_type": task_type,
            "risk_level": risk_level,
            "complexity_score": complexity_score,
            "estimated_cost_usd": estimated_cost_usd,
        },
        evidence={
            "execution_mode": execution_mode,
            "source": source,
            "active_protocol": {
                "protocol_id": protocol_id,
                "version": protocol_version,
                "execution_mode": str(
                    getattr(getattr(active_protocol, "execution", None), "mode", "") or ""
                ),
            }
            if active_protocol is not None
            else {},
            "watchtower_decision": _model_dump_or_value(watchtower_decision)
            if watchtower_decision is not None
            else {},
            "mode_override_reason": mode_override_reason,
        },
        metadata={
            "execution_mode": execution_mode,
            "source": source,
            "protocol_id": protocol_id,
            "strategy_pack_id": watchtower_pack,
            "mode_override_reason": mode_override_reason,
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
    selected_assets = [
        {
            "asset_id": str(getattr(item, "asset_id", "")),
            "asset_kind": str(getattr(item, "asset_kind", "")),
            "relevance_score": float(getattr(item, "relevance_score", 0.0) or 0.0),
            "score_breakdown": _dict_or_empty(getattr(item, "score_breakdown", {})),
            "score_rationale": str(getattr(item, "score_rationale", "") or ""),
            "tags": _string_list(getattr(item, "tags", [])),
            "governance_labels": _string_list(getattr(item, "governance_labels", [])),
            "memory_layer": str(getattr(item, "memory_layer", "") or ""),
        }
        for item in items
        if getattr(item, "asset_id", "")
    ]
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
            "selected_assets": selected_assets,
            "memory_policy": memory_policy or {},
        },
        metadata={
            "asset_ids": asset_ids,
            "asset_count": len(asset_ids),
            "selected_assets": selected_assets,
            "context_limit": context_limit,
            "execution_mode": execution_mode,
            "memory_policy": memory_policy or {},
        },
    )


def ticket_from_memory_policy_selection(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    policy: Any,
    mission_id: str | None = None,
    source_module: str = "memory.policy",
) -> DecisionTicket:
    """Wrap sparse memory retrieval policy as an auditable decision ticket."""

    layers = _string_list(getattr(policy, "layers", []))
    asset_kinds = _string_list(getattr(policy, "asset_kinds", []))
    preferred_tags = _string_list(getattr(policy, "preferred_tags", []))
    avoid_layers = _string_list(getattr(policy, "avoid_layers", []))
    risk_flags = _string_list(getattr(policy, "risk_flags", []))
    depth = str(getattr(policy, "depth", "unknown"))
    use_memory = bool(getattr(policy, "use_memory", False))
    max_items = int(getattr(policy, "max_items", 0) or 0)
    allow_mid_run_retrieval = bool(getattr(policy, "allow_mid_run_retrieval", False))
    status: DecisionStatus = "selected" if use_memory else "skipped"
    policy_dump = _model_dump_or_value(policy)
    selected_action = (
        f"{depth}:{','.join(layers) if layers else 'no_layers'}" if use_memory else f"{depth}:skip"
    )

    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="memory",
        decision_point="memory_policy_selected",
        source_module=source_module,
        selected_action=selected_action,
        status=status,
        reason=str(getattr(policy, "reason", "")),
        confidence=0.74 if use_memory else 0.66,
        risk_level=risk_level,
        cost_estimate_usd=0.0,
        alternatives=avoid_layers,
        inputs_summary={
            "risk_level": risk_level,
            "use_memory": use_memory,
            "depth": depth,
            "max_items": max_items,
        },
        evidence={
            "policy": policy_dump,
            "layers": layers,
            "asset_kinds": asset_kinds,
            "preferred_tags": preferred_tags,
            "avoid_layers": avoid_layers,
            "risk_flags": risk_flags,
        },
        metadata={
            "use_memory": use_memory,
            "depth": depth,
            "layers": layers,
            "asset_kinds": asset_kinds,
            "preferred_tags": preferred_tags,
            "max_items": max_items,
            "allow_mid_run_retrieval": allow_mid_run_retrieval,
            "risk_flags": risk_flags,
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
                    "rank": idx + 1,
                    "skill_id": str(getattr(skill, "skill_id", "")),
                    "description": str(
                        getattr(getattr(skill, "manifest", None), "description", "")
                    ),
                    "version": str(getattr(getattr(skill, "manifest", None), "version", "")),
                    "maturity": str(getattr(getattr(skill, "manifest", None), "maturity", "")),
                    "source_path": str(getattr(skill, "source_path", "")),
                    "auto_trigger_count": len(
                        getattr(getattr(skill, "manifest", None), "auto_trigger_when", []) or []
                    ),
                    "allowed_command_count": len(
                        getattr(getattr(skill, "manifest", None), "allowed_commands", []) or []
                    ),
                    "denied_pattern_count": len(
                        getattr(getattr(skill, "manifest", None), "denied_patterns", []) or []
                    ),
                }
                for idx, skill in enumerate(skills)
            ],
        },
        metadata={"skill_ids": skill_ids, "skill_count": len(skill_ids), "top_k": top_k},
    )


def ticket_from_step_action_selection(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    step_id: int,
    hermes_step: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap Hermes per-step action choice as a decision ticket."""

    action_type = str(getattr(hermes_step, "action_type", "") or "direct_llm")
    payload = _dict_or_empty(getattr(hermes_step, "action_payload", {}))
    confidence = _float_or_default(getattr(hermes_step, "confidence", None), 0.5)
    cost_estimate = _float_or_default(getattr(hermes_step, "cost_estimate_usd", None), 0.0)
    expected_outcome = str(getattr(hermes_step, "expected_outcome", "") or "")
    thought = str(getattr(hermes_step, "thought", "") or "")
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="step",
        decision_point="step_action_selected",
        source_module="interface.hermes",
        selected_action=action_type,
        status="selected",
        reason=thought or f"Hermes selected {action_type}",
        confidence=confidence,
        risk_level=risk_level,
        cost_estimate_usd=cost_estimate,
        inputs_summary={
            "step_id": step_id,
            "risk_level": risk_level,
        },
        evidence={
            "action_type": action_type,
            "action_payload": payload,
            "expected_outcome": expected_outcome,
            "thought": thought,
        },
        metadata={
            "step_id": step_id,
            "action_type": action_type,
            "expected_outcome": expected_outcome,
        },
    )


def ticket_from_preflight_guard(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    report: Any,
    pending_actions: list[Any],
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap pre-start conflict scan + pending approval gate."""

    resources = list(getattr(report, "resources", []) or [])
    conflicts = list(getattr(report, "conflicts", []) or [])
    blocking = bool(getattr(report, "blocking", False))
    action_types = [
        str(getattr(action, "action_type", "")) for action in pending_actions if action is not None
    ]
    resource_names = [
        str(getattr(resource, "resource", "")) for resource in resources if resource is not None
    ]
    status: DecisionStatus = "blocked" if blocking or pending_actions else "allowed"
    selected_action = "pause_for_preflight" if status == "blocked" else "allow_preflight"
    reason_parts: list[str] = []
    if conflicts:
        reason_parts.append(f"{len(conflicts)} resource conflict(s)")
    if pending_actions:
        reason_parts.append(f"{len(pending_actions)} pending approval action(s)")
    if not reason_parts:
        reason_parts.append("no blocking preflight issue")
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="watchtower",
        decision_point="preflight_guard",
        source_module="engineering.concurrency",
        selected_action=selected_action,
        status=status,
        reason="; ".join(reason_parts),
        confidence=0.86 if status == "blocked" else 0.78,
        risk_level=risk_level,
        inputs_summary={
            "resource_count": len(resources),
            "pending_action_count": len(pending_actions),
        },
        evidence={
            "resources": [_model_dump_or_value(resource) for resource in resources],
            "conflicts": [_model_dump_or_value(conflict) for conflict in conflicts],
            "pending_actions": [_model_dump_or_value(action) for action in pending_actions],
        },
        metadata={
            "resource_names": resource_names,
            "conflict_count": len(conflicts),
            "pending_action_types": action_types,
            "pending_action_count": len(pending_actions),
        },
    )


def ticket_from_proactive_tool_dispatch(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    scan_result: Any,
    prompt_excerpt: str = "",
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap proactive tool pre-dispatch and missed trigger decisions."""

    dispatched = list(getattr(scan_result, "dispatched", []) or [])
    missed = list(getattr(scan_result, "missed_opportunities", []) or [])
    dispatched_skills = [
        str(getattr(item, "skill_id", "")) for item in dispatched if getattr(item, "skill_id", "")
    ]
    missed_skills = [
        str(item.get("skill_id", ""))
        for item in missed
        if isinstance(item, dict) and item.get("skill_id")
    ]
    status: DecisionStatus = (
        "applied" if dispatched_skills else "skipped" if missed_skills else "allowed"
    )
    selected_action = (
        ",".join(dispatched_skills)
        if dispatched_skills
        else f"missed:{','.join(missed_skills)}"
        if missed_skills
        else "none"
    )
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="step",
        decision_point="proactive_tool_dispatch",
        source_module="engineering.proactive_tools",
        selected_action=selected_action,
        status=status,
        reason=(
            f"dispatched={len(dispatched_skills)} missed={len(missed_skills)}"
            if dispatched_skills or missed_skills
            else "no proactive trigger fired"
        ),
        confidence=0.76 if dispatched_skills else 0.58,
        risk_level=risk_level,
        inputs_summary={
            "prompt_excerpt": prompt_excerpt[:200],
            "dispatched_count": len(dispatched_skills),
            "missed_count": len(missed_skills),
        },
        evidence={
            "dispatched": [
                {
                    "skill_id": str(getattr(item, "skill_id", "")),
                    "params": _dict_or_empty(getattr(item, "params", {})),
                    "trigger_reason": str(getattr(item, "trigger_reason", "") or ""),
                    "ok": bool(getattr(getattr(item, "result", None), "ok", False)),
                    "error": str(getattr(getattr(item, "result", None), "error", "") or ""),
                }
                for item in dispatched
            ],
            "missed": [dict(item) for item in missed if isinstance(item, dict)],
        },
        metadata={
            "dispatched_skills": dispatched_skills,
            "missed_skills": missed_skills,
            "dispatched_count": len(dispatched_skills),
            "missed_count": len(missed_skills),
        },
    )


def ticket_from_anti_gaming_finding(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    step_id: int,
    finding: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap anti-gaming / fake-completion findings for credit and review."""

    pattern = str(getattr(finding, "pattern", "") or "unknown")
    severity = str(getattr(finding, "severity", "") or "medium")
    confidence = _float_or_default(getattr(finding, "confidence", None), 0.5)
    evidence = _dict_or_empty(getattr(finding, "evidence", {}))
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="step",
        decision_point="anti_gaming_detected",
        source_module="security.anti_gaming",
        selected_action=pattern,
        status="needs_review",
        reason=str(getattr(finding, "reason", "") or pattern),
        confidence=confidence,
        risk_level=risk_level,
        inputs_summary={"step_id": step_id, "severity": severity},
        evidence={
            "pattern": pattern,
            "severity": severity,
            "confidence": confidence,
            "evidence": evidence,
        },
        metadata={
            "step_id": step_id,
            "pattern": pattern,
            "severity": severity,
        },
    )


def ticket_from_emergent_switch(
    *,
    tenant_id: str,
    task_id: str,
    risk_level: str,
    step_id: int,
    signals: list[str],
    evaluation: Any,
    mission_id: str | None = None,
) -> DecisionTicket:
    """Wrap dynamic mid-run path switch evaluation."""

    should_switch = bool(getattr(evaluation, "should_switch", False))
    chosen = getattr(evaluation, "chosen_solution", None)
    solution_id = str(getattr(chosen, "solution_id", "") or "")
    blocked_by = str(getattr(evaluation, "blocked_by", "") or "")
    switch_score = _float_or_default(getattr(evaluation, "switch_score", None), 0.0)
    status: DecisionStatus = "applied" if should_switch else "blocked"
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        phase="watchtower",
        decision_point="emergent_switch",
        source_module="engineering.emergent_switch",
        selected_action=solution_id if should_switch and solution_id else blocked_by or "no_switch",
        status=status,
        reason=str(getattr(evaluation, "reason", "") or blocked_by or "switch evaluated"),
        confidence=max(0.0, min(1.0, switch_score)),
        risk_level=risk_level,
        inputs_summary={"step_id": step_id, "signals": list(signals)},
        evidence={
            "signals": list(signals),
            "switch_score": switch_score,
            "should_switch": should_switch,
            "blocked_by": blocked_by,
            "chosen_solution": _model_dump_or_value(chosen) if chosen is not None else {},
        },
        metadata={
            "step_id": step_id,
            "solution_id": solution_id,
            "blocked_by": blocked_by,
            "switch_score": switch_score,
            "signals": list(signals),
        },
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


def ticket_from_qi_experiment(
    *,
    tenant_id: str,
    target_id: str,
    target_kind: str,
    experiment: Any,
    risk_level: str = "medium",
    task_id: str | None = None,
) -> DecisionTicket:
    """Wrap a Qi exploration artifact as review-only evidence.

    Qi is allowed to search for better strategies, but this ticket makes the
    boundary explicit: experiments are visible to NUO/human/strong judge review
    and are not production actions by themselves.
    """

    proposed_pack_id = str(getattr(experiment, "proposed_pack_id", "") or "")
    status = str(getattr(experiment, "status", "") or "draft")
    requires_human_review = bool(getattr(experiment, "requires_human_review", True))
    requires_strong_review = bool(getattr(experiment, "requires_strong_review", False))
    production_action = bool(getattr(experiment, "production_action", False))
    default_mode = str(getattr(experiment, "default_execution_mode", "") or "")
    risk_watch = _string_list(getattr(experiment, "risk_watch", []))
    promotion_conditions = _string_list(getattr(experiment, "promotion_conditions", []))
    task_patterns = _string_list(getattr(experiment, "task_type_patterns", []))
    effective_risk = (
        "critical"
        if requires_strong_review or any("unauthorized" in item for item in risk_watch)
        else risk_level
    )
    return DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_id or f"qi:{target_id}",
        phase="qi",
        decision_point="qi_experiment",
        source_module="qi.idle_replay",
        selected_action=f"review_only:{proposed_pack_id or target_id}",
        status="needs_review",
        reason=(
            f"Qi proposed review-only {target_kind} {target_id}; "
            f"status={status}; production_action={str(production_action).lower()}"
        ),
        confidence=0.62 if requires_strong_review else 0.68,
        risk_level=effective_risk,
        cost_estimate_usd=0.0,
        inputs_summary={
            "target_id": target_id,
            "target_kind": target_kind,
            "task_type_patterns": task_patterns,
        },
        constraints=[
            "production_action=false",
            "requires_human_review=true"
            if requires_human_review
            else "requires_human_review=false",
            "requires_strong_review=true"
            if requires_strong_review
            else "requires_strong_review=false",
            *promotion_conditions,
        ],
        evidence={
            "target_kind": target_kind,
            "experiment": _model_dump_or_value(experiment),
            "production_action": production_action,
            "requires_human_review": requires_human_review,
            "requires_strong_review": requires_strong_review,
            "promotion_conditions": promotion_conditions,
            "risk_watch": risk_watch,
        },
        metadata={
            "target_id": target_id,
            "target_kind": target_kind,
            "proposed_pack_id": proposed_pack_id,
            "status": status,
            "default_execution_mode": default_mode,
            "production_action": production_action,
            "requires_human_review": requires_human_review,
            "requires_strong_review": requires_strong_review,
        },
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


def _dict_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


__all__ = [
    "DecisionPhase",
    "DecisionPoint",
    "DecisionStatus",
    "DecisionTicket",
    "DecisionTicketRef",
    "ticket_from_anti_gaming_finding",
    "ticket_from_budget_policy",
    "ticket_from_context_selection",
    "ticket_from_delivery_review",
    "ticket_from_emergent_switch",
    "ticket_from_execution_mode_selection",
    "ticket_from_llm_route",
    "ticket_from_memory_policy_selection",
    "ticket_from_preflight_guard",
    "ticket_from_proactive_tool_dispatch",
    "ticket_from_protocol_applied",
    "ticket_from_qi_experiment",
    "ticket_from_route_choice",
    "ticket_from_skill_selection",
    "ticket_from_step_action_selection",
    "ticket_from_validation_tier",
    "ticket_from_value_gate_decision",
    "ticket_from_watchtower_decision",
    "ticket_from_world_policy",
]
