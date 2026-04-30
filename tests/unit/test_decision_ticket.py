from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from kun.context.packer import ContextPack, PackedContextItem
from kun.datamodel.decision_ticket import (
    ticket_from_budget_policy,
    ticket_from_context_selection,
    ticket_from_delivery_review,
    ticket_from_llm_route,
    ticket_from_protocol_applied,
    ticket_from_route_choice,
    ticket_from_skill_selection,
    ticket_from_validation_tier,
    ticket_from_value_gate_decision,
    ticket_from_watchtower_decision,
    ticket_from_world_policy,
)
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
            maturity="stable",
        ),
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
