from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from kun.context.packer import ContextPack, PackedContextItem
from kun.core.emergent_solution import EmergentSolution, EmergentSource
from kun.core.ooda_loop import OODACycle, OODAState
from kun.datamodel.decision_ticket import (
    ticket_from_anti_gaming_finding,
    ticket_from_budget_policy,
    ticket_from_context_selection,
    ticket_from_delivery_review,
    ticket_from_emergent_switch,
    ticket_from_execution_mode_selection,
    ticket_from_llm_route,
    ticket_from_llm_route_governance,
    ticket_from_memory_policy_selection,
    ticket_from_nuo_diagnosis,
    ticket_from_ooda_checkpoint,
    ticket_from_preflight_guard,
    ticket_from_proactive_tool_dispatch,
    ticket_from_protocol_applied,
    ticket_from_qi_experiment,
    ticket_from_route_choice,
    ticket_from_skill_selection,
    ticket_from_step_action_selection,
    ticket_from_validation_tier,
    ticket_from_value_gate_decision,
    ticket_from_watchtower_decision,
    ticket_from_world_policy,
)
from kun.engineering.concurrency import (
    ConflictFinding,
    PendingActionSpec,
    PreConflictReport,
    ResourceIntent,
)
from kun.engineering.proactive_tools import ProactiveDispatch, ProactiveScanResult
from kun.memory.policy import MemoryDepth, MemoryLayer, MemoryPolicyTicket
from kun.security.anti_gaming import GamingFinding
from kun.skills.dispatcher import SkillResult
from kun.watchtower.value_gate import ValueGateDecision


class _Decision:
    strategy_pack_id = "coding"
    strategy_pack_name = "代码任务"
    execution_mode = "MAX"
    context_limit = 3
    skill_hints: ClassVar[list[str]] = ["code_reader"]
    metric_dimensions: ClassVar[list[str]] = ["test_pass_rate"]
    reward_weights: ClassVar[dict[str, float]] = {"quality": 0.8}
    risk_watch: ClassVar[list[str]] = ["regression"]
    alert_flags: ClassVar[list[str]] = []
    reason = "复杂代码任务需要深路径"
    confidence = 0.84

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        return {
            "strategy_pack_id": self.strategy_pack_id,
            "execution_mode": self.execution_mode,
            "mode": mode,
        }


def test_watchtower_decision_ticket_wraps_strategy_choice() -> None:
    ticket = ticket_from_watchtower_decision(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="high",
        estimated_cost_usd=1.2,
        decision=_Decision(),
    )

    assert ticket.ticket_id.startswith("dt-")
    assert ticket.decision_point == "strategy_selected"
    assert ticket.selected_action == "coding:MAX"
    assert ticket.status == "applied"
    assert ticket.metadata["strategy_pack_id"] == "coding"
    assert ticket.ref().ticket_id == ticket.ticket_id


def test_execution_mode_ticket_records_final_sparse_depth() -> None:
    ticket = ticket_from_execution_mode_selection(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        execution_mode="MAX",
        task_type="coding.python",
        complexity_score=0.82,
        estimated_cost_usd=0.45,
        watchtower_decision=_Decision(),
    )

    assert ticket.phase == "watchtower"
    assert ticket.decision_point == "execution_mode_selected"
    assert ticket.selected_action == "watchtower:MAX"
    assert ticket.status == "applied"
    assert ticket.metadata["execution_mode"] == "MAX"
    assert ticket.evidence["watchtower_decision"]["strategy_pack_id"] == "coding"
    assert "FAST" in ticket.alternatives


def test_ooda_checkpoint_ticket_records_outer_loop_state() -> None:
    cycle = OODACycle(task_ref="tk-1", current_state=OODAState.REFLECT)

    ticket = ticket_from_ooda_checkpoint(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        checkpoint="reflect",
        cycle=cycle,
        status="needs_review",
        reason="latest action failed",
        step_id=2,
        evidence={"reflection": {"needs_adjust": True}},
    )

    assert ticket.decision_point == "ooda_checkpoint"
    assert ticket.phase == "step"
    assert ticket.selected_action == "reflect:reflect"
    assert ticket.status == "needs_review"
    assert ticket.metadata["step_id"] == 2
    assert ticket.evidence["reflection"]["needs_adjust"] is True


def test_value_gate_ticket_maps_intervention_status() -> None:
    ticket = ticket_from_value_gate_decision(
        tenant_id="tenant-1",
        task_id="tk-1",
        step_id=2,
        decision=ValueGateDecision(
            decision="escalate",
            reason="value_below_threshold",
            expected_value=0.12,
        ),
    )

    assert ticket.decision_point == "value_gate"
    assert ticket.status == "escalated"
    assert ticket.metadata["step_id"] == 2


def test_route_choice_ticket_wraps_role_and_model_purpose() -> None:
    from kun.brain.router import TaskRouter
    from kun.datamodel.task import Owner, TaskMeta

    owner = Owner(tenant_id="tenant-1")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("write code", owner),
        owner=owner,
        task_type="coding.python",
        risk_level="medium",
        complexity_score=0.6,
        estimated_cost_usd=0.4,
        success_criteria_short="write code",
    )
    choice = TaskRouter().choose(meta)

    ticket = ticket_from_route_choice(
        tenant_id="tenant-1",
        task_id=meta.task_id,
        risk_level=meta.risk_level,
        estimated_cost_usd=meta.estimated_cost_usd,
        choice=choice,
    )

    assert ticket.phase == "routing"
    assert ticket.decision_point == "role_model_selected"
    assert ticket.selected_action == "rt-coder:coding"
    assert ticket.metadata["purpose"] == "coding"


def test_llm_route_ticket_wraps_actual_model_choice() -> None:
    ticket = ticket_from_llm_route(
        tenant_id="tenant-1",
        task_id="tk-1",
        step_id=2,
        purpose="execution",
        provider="codex-mcp",
        model="gpt-5.5",
        tier="top",
        cost_usd=0.12,
        risk_level="medium",
    )

    assert ticket.phase == "step"
    assert ticket.decision_point == "llm_model_selected"
    assert ticket.selected_action == "codex-mcp:gpt-5.5:top"
    assert ticket.evidence["provider"] == "codex-mcp"
    assert ticket.metadata["step_id"] == 2


def test_llm_route_governance_ticket_wraps_pre_call_policy() -> None:
    ticket = ticket_from_llm_route_governance(
        tenant_id="tenant-1",
        task_id="tk-1",
        task_type="coding.python",
        selected_model="top",
        candidate_models=["cheap", "top"],
        selected_score=0.91,
        score_reason="capability_card",
        estimated_cost_usd=0.2,
    )

    assert ticket.phase == "routing"
    assert ticket.decision_point == "llm_model_selected"
    assert ticket.source_module == "watchtower.llm_route_governance"
    assert ticket.selected_action == "top"
    assert ticket.alternatives == ["cheap"]
    assert ticket.evidence["selected_score"] == 0.91


def test_nuo_diagnosis_ticket_wraps_governance_apply_choice() -> None:
    ticket = ticket_from_nuo_diagnosis(
        tenant_id="tenant-1",
        recommendation_id="govern:context_slimming_candidates",
        finding_id="context_slimming_candidates",
        subsystem="context",
        selected_action="dry_run:context_maintenance",
        status="selected",
        reason="NUO dry-run only.",
        risk_level="low",
        can_apply=True,
        dry_run=True,
    )

    assert ticket.phase == "nuo"
    assert ticket.decision_point == "nuo_diagnosis"
    assert ticket.source_module == "engineering.nuo_system_health"
    assert ticket.task_id == "nuo:govern:context_slimming_candidates"
    assert ticket.evidence["can_apply"] is True


def test_context_selection_ticket_records_selected_assets() -> None:
    pack = ContextPack(
        items=[
            PackedContextItem(
                asset_id="asset-1",
                asset_kind="memory",
                relevance_score=0.91,
                title="上次复盘",
            )
        ]
    )

    ticket = ticket_from_context_selection(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        execution_mode="MAX",
        context_limit=3,
        context_pack=pack,
    )

    assert ticket.phase == "context"
    assert ticket.decision_point == "context_selected"
    assert ticket.status == "selected"
    assert ticket.metadata["asset_ids"] == ["asset-1"]
    assert ticket.evidence["asset_kinds"] == ["memory"]
    assert ticket.evidence["selected_assets"][0]["asset_id"] == "asset-1"
    assert ticket.evidence["selected_assets"][0]["relevance_score"] == 0.91


def test_memory_policy_ticket_records_sparse_memory_decision() -> None:
    policy = MemoryPolicyTicket(
        use_memory=True,
        depth=MemoryDepth.TARGETED,
        layers=[MemoryLayer.META_DECISION, MemoryLayer.METHODOLOGY],
        asset_kinds=["memory", "methodology"],
        preferred_tags=["ops", "retention"],
        max_items=2,
        allow_mid_run_retrieval=True,
        avoid_layers=[MemoryLayer.BEHAVIOR],
        risk=False,
        risk_flags=[],
        reason="strategy_ops_prefers_meta_methodology",
    )

    ticket = ticket_from_memory_policy_selection(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        policy=policy,
        source_module="watchtower.decision_plane",
    )

    assert ticket.phase == "memory"
    assert ticket.decision_point == "memory_policy_selected"
    assert ticket.status == "selected"
    assert ticket.selected_action == "targeted:meta_decision,methodology"
    assert ticket.source_module == "watchtower.decision_plane"
    assert ticket.metadata["layers"] == ["meta_decision", "methodology"]
    assert ticket.metadata["max_items"] == 2
    assert ticket.metadata["allow_mid_run_retrieval"] is True


def test_budget_policy_ticket_records_runtime_budget_level() -> None:
    ticket = ticket_from_budget_policy(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="high",
        level="LOW",
        used_usd=0.9,
        limit_usd=1.0,
        behavior={"exploration": "converge_verified_only"},
        hard_break=False,
    )

    assert ticket.phase == "watchtower"
    assert ticket.decision_point == "budget_policy"
    assert ticket.selected_action == "LOW"
    assert ticket.status == "escalated"
    assert ticket.metadata["usage_ratio"] == 0.9


def test_skill_selection_ticket_records_candidate_skills() -> None:
    skill = SimpleNamespace(
        skill_id="lesson_planner",
        manifest=SimpleNamespace(
            description="Plan lessons",
            version="1.2.3",
            maturity="stable",
            auto_trigger_when=[{"pattern": "lesson"}],
            allowed_commands=["python"],
            denied_patterns=["rm -rf"],
        ),
        source_path="skills/lesson/SKILL.md",
    )

    ticket = ticket_from_skill_selection(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="low",
        top_k=3,
        skills=[skill],
    )

    assert ticket.phase == "skill"
    assert ticket.decision_point == "skill_selected"
    assert ticket.status == "selected"
    assert ticket.metadata["skill_ids"] == ["lesson_planner"]
    assert ticket.evidence["skills"][0]["description"] == "Plan lessons"
    assert ticket.evidence["skills"][0]["rank"] == 1
    assert ticket.evidence["skills"][0]["version"] == "1.2.3"
    assert ticket.evidence["skills"][0]["source_path"] == "skills/lesson/SKILL.md"
    assert ticket.evidence["skills"][0]["auto_trigger_count"] == 1


def test_step_action_ticket_records_hermes_action_choice() -> None:
    hermes_step = SimpleNamespace(
        action_type="use_memory",
        action_payload={"query": "过去留存策略"},
        thought="先查元决策记忆",
        expected_outcome="拿到可复用策略",
        confidence=0.82,
        cost_estimate_usd=0.01,
    )

    ticket = ticket_from_step_action_selection(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        step_id=3,
        hermes_step=hermes_step,
    )

    assert ticket.phase == "step"
    assert ticket.decision_point == "step_action_selected"
    assert ticket.selected_action == "use_memory"
    assert ticket.metadata["step_id"] == 3
    assert ticket.evidence["action_payload"]["query"] == "过去留存策略"


def test_preflight_guard_ticket_records_conflicts_and_pending_actions() -> None:
    report = PreConflictReport(
        resources=[ResourceIntent(resource="project:demo", mode="write", reason="same project")],
        conflicts=[
            ConflictFinding(
                task_id="tk-running",
                status="running",
                resource="project:demo",
                existing_mode="write",
                incoming_mode="write",
                reason="same project write",
            )
        ],
    )
    action = PendingActionSpec(action_type="email.draft", target_ref="project:demo")

    ticket = ticket_from_preflight_guard(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        report=report,
        pending_actions=[action],
    )

    assert ticket.phase == "watchtower"
    assert ticket.decision_point == "preflight_guard"
    assert ticket.status == "blocked"
    assert ticket.selected_action == "pause_for_preflight"
    assert ticket.metadata["conflict_count"] == 1
    assert ticket.metadata["pending_action_types"] == ["email.draft"]


def test_proactive_tool_ticket_records_dispatch_and_missed_triggers() -> None:
    scan = ProactiveScanResult(
        dispatched=[
            ProactiveDispatch(
                skill_id="python-exec",
                params={"code": "print(1)"},
                result=SkillResult(skill_id="python-exec", ok=True, output={"stdout": "1"}),
                trigger_reason="keyword:python",
            )
        ],
        missed_opportunities=[
            {
                "skill_id": "ghost-skill",
                "reason": "executor_unregistered",
                "trigger_source": "keyword",
            }
        ],
    )

    ticket = ticket_from_proactive_tool_dispatch(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="low",
        scan_result=scan,
        prompt_excerpt="跑一段 python",
    )

    assert ticket.phase == "step"
    assert ticket.decision_point == "proactive_tool_dispatch"
    assert ticket.status == "applied"
    assert ticket.metadata["dispatched_skills"] == ["python-exec"]
    assert ticket.metadata["missed_skills"] == ["ghost-skill"]
    assert ticket.evidence["dispatched"][0]["ok"] is True


def test_anti_gaming_ticket_records_fake_completion_finding() -> None:
    finding = GamingFinding(
        pattern="fake_completion",
        confidence=0.91,
        reason="claims done without asset",
        severity="high",
        evidence={"has_assets": False},
    )

    ticket = ticket_from_anti_gaming_finding(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        step_id=4,
        finding=finding,
    )

    assert ticket.phase == "step"
    assert ticket.decision_point == "anti_gaming_detected"
    assert ticket.status == "needs_review"
    assert ticket.selected_action == "fake_completion"
    assert ticket.metadata["severity"] == "high"
    assert ticket.evidence["evidence"]["has_assets"] is False


def test_emergent_switch_ticket_records_selected_solution() -> None:
    solution = EmergentSolution(
        task_type="coding.py",
        discovered_by="external_scan",
        source=EmergentSource(kind="github_issue"),
        estimated_outcome_delta=0.3,
        estimated_cost_delta=-0.1,
        status="stable",
    )
    evaluation = SimpleNamespace(
        should_switch=True,
        switch_score=0.33,
        chosen_solution=solution,
        reason="signals=['external_emergent_found'] score=0.33",
        blocked_by="",
    )

    ticket = ticket_from_emergent_switch(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        step_id=2,
        signals=["external_emergent_found"],
        evaluation=evaluation,
    )

    assert ticket.phase == "watchtower"
    assert ticket.decision_point == "emergent_switch"
    assert ticket.status == "applied"
    assert ticket.metadata["solution_id"] == solution.solution_id
    assert ticket.evidence["switch_score"] == 0.33


def test_protocol_applied_ticket_wraps_protocol_registry_choice() -> None:
    from kun.qi.protocol import (
        Protocol,
        ProtocolExecution,
        ProtocolSkillStep,
        ProtocolTrigger,
        ProtocolVerificationSpec,
    )

    protocol = Protocol(
        protocol_id="education.lesson.plan",
        version="1.2.0",
        tenant_id="tenant-1",
        status="stable",
        trigger=ProtocolTrigger(
            task_type_pattern="education.*",
            risk_levels=["low", "medium"],
        ),
        execution=ProtocolExecution(mode="MAX", expected_cost_usd=0.2),
        skill_chain=[ProtocolSkillStep(skill="context.retrieve", when="before_planning")],
        verification=[ProtocolVerificationSpec(kind="rubric", required=True)],
    )

    ticket = ticket_from_protocol_applied(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="medium",
        estimated_cost_usd=0.4,
        protocol=protocol,
    )

    assert ticket.phase == "protocol"
    assert ticket.decision_point == "protocol_applied"
    assert ticket.selected_action == "education.lesson.plan:1.2.0:MAX"
    assert ticket.status == "applied"
    assert ticket.evidence["skill_chain"][0]["skill"] == "context.retrieve"
    assert ticket.metadata["protocol_status"] == "stable"


def test_world_policy_ticket_blocks_missing_handler() -> None:
    ticket = ticket_from_world_policy(
        tenant_id="tenant-1",
        task_id="tk-1",
        action_id="act-1",
        action_type="payment.send",
        risk_level="critical",
        gateway_mode="approval_gate",
        external_dispatched=False,
        requires_handler=True,
        reason="missing handler",
    )

    assert ticket.decision_point == "world_policy"
    assert ticket.status == "blocked"
    assert ticket.metadata["action_type"] == "payment.send"


def test_delivery_review_ticket_maps_needs_review() -> None:
    from kun.engineering.pre_deliver_gate import GateCheckResult, PreDeliverVerdict

    ticket = ticket_from_delivery_review(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="high",
        verdict=PreDeliverVerdict(
            passed=False,
            final_status="needs_review",
            reason_summary="anti gaming finding",
            checks=[
                GateCheckResult(
                    name="anti_gaming.fake_completion",
                    passed=False,
                    severity="high",
                    reason="claimed done without evidence",
                )
            ],
        ),
    )

    assert ticket.phase == "delivery"
    assert ticket.decision_point == "delivery_review"
    assert ticket.status == "needs_review"
    assert ticket.evidence["checks"][0]["name"] == "anti_gaming.fake_completion"


def test_validation_tier_ticket_records_risk_and_mode_context() -> None:
    ticket = ticket_from_validation_tier(
        tenant_id="tenant-1",
        task_id="tk-1",
        risk_level="critical",
        complexity_score=0.8,
        tier="tier3",
        execution_mode="MAX",
        mode_override_reason="protocol forced deep validation",
    )

    assert ticket.phase == "delivery"
    assert ticket.decision_point == "validation_tier_selected"
    assert ticket.selected_action == "tier3"
    assert ticket.status == "selected"
    assert ticket.metadata["validation_tier"] == "tier3"
    assert ticket.evidence["risk_high"] is True
    assert ticket.evidence["complexity_high"] is True


def test_qi_experiment_ticket_keeps_draft_review_only() -> None:
    draft = SimpleNamespace(
        draft_id="spd-1",
        proposed_pack_id="qi_coding_spd_1",
        status="needs_strong_review",
        default_execution_mode="MAX",
        task_type_patterns=["coding.*"],
        risk_watch=["unauthorized_side_effect"],
        promotion_conditions=["human_review_approved", "strong_model_review_passed"],
        requires_human_review=True,
        requires_strong_review=True,
        production_action=False,
    )

    ticket = ticket_from_qi_experiment(
        tenant_id="tenant-1",
        target_id="spd-1",
        target_kind="strategy_pack_draft",
        experiment=draft,
    )

    assert ticket.phase == "qi"
    assert ticket.decision_point == "qi_experiment"
    assert ticket.status == "needs_review"
    assert ticket.selected_action == "review_only:qi_coding_spd_1"
    assert ticket.risk_level == "critical"
    assert ticket.evidence["production_action"] is False
    assert "strong_model_review_passed" in ticket.constraints
