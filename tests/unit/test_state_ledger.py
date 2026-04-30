from __future__ import annotations

from types import SimpleNamespace

from kun.context.packer import ContextPack, PackedContextItem
from kun.core.state_ledger import StateLedger
from kun.datamodel.decision_ticket import (
    ticket_from_context_selection,
    ticket_from_llm_route,
    ticket_from_protocol_applied,
    ticket_from_skill_selection,
    ticket_from_validation_tier,
)
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.qi.protocol import Protocol, ProtocolExecution, ProtocolTrigger
from kun.watchtower.decision_plane import WatchtowerDecisionPlane


def test_state_ledger_tracks_decision_runtime_and_step() -> None:
    owner = Owner(tenant_id="tenant-1", user_id="user-1")
    task_ref = TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("设计学习计划", owner),
            task_type="education.lesson",
            risk_level="low",
            complexity_score=0.4,
            owner=owner,
            estimated_cost_usd=0.2,
            success_criteria_short="设计学习计划",
        ),
        spec=TaskSpec(goal_detail="给用户设计一套可执行的学习计划"),
    )
    ledger = StateLedger()

    ledger.record_task_created(task_ref, tenant_id=owner.tenant_id)
    decision = WatchtowerDecisionPlane().decide(task_ref)
    ledger.record_decision(task_ref.meta.task_id, decision)

    runtime = RuntimeState(task_ref=task_ref.meta.task_id, total_planned_steps=2, status="running")
    ledger.record_running(task_ref.meta.task_id, runtime=runtime)
    step = StepRecord(
        step_id=1,
        skill_used="lesson_planner",
        cost_usd_equivalent=0.03,
        tokens_in=10,
        tokens_out=12,
    )
    runtime.accumulate_step(step)
    ledger.record_step_completed(
        task_ref.meta.task_id,
        runtime=runtime,
        step=step,
        provider="stub",
        model="stub-model",
        tier="cheap",
    )

    snapshot = ledger.snapshot(task_ref.meta.task_id)

    assert snapshot is not None
    assert snapshot.current_goal == "给用户设计一套可执行的学习计划"
    assert snapshot.strategy_pack_id == "education"
    assert snapshot.execution_mode == decision.execution_mode
    assert snapshot.context_limit == decision.context_limit
    assert snapshot.current_step == 1
    assert snapshot.total_steps == 2
    assert snapshot.current_skill == "lesson_planner"
    assert snapshot.current_model == "stub-model"
    assert snapshot.cost_so_far_usd == 0.03
    assert snapshot.tokens_so_far == 22
    assert [event.kind for event in snapshot.recent_events] == [
        "task.created",
        "watchtower.decision",
        "task.started",
        "task.step.completed",
    ]


def test_state_ledger_active_snapshots_are_tenant_scoped() -> None:
    ledger = StateLedger()
    owner_a = Owner(tenant_id="tenant-a", user_id="user-a")
    owner_b = Owner(tenant_id="tenant-b", user_id="user-b")
    task_a = _task_ref(owner_a, "任务 A")
    task_b = _task_ref(owner_b, "任务 B")

    ledger.record_task_created(task_a, tenant_id=owner_a.tenant_id)
    ledger.record_task_created(task_b, tenant_id=owner_b.tenant_id)

    active_a = ledger.active_snapshots(tenant_id="tenant-a")

    assert [entry.task_id for entry in active_a] == [task_a.meta.task_id]


def test_state_ledger_applies_protocol_and_llm_route_tickets_to_current_view() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "设计课程")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)
    protocol = Protocol(
        protocol_id="education.lesson.plan",
        version="1.0.0",
        tenant_id=owner.tenant_id,
        status="stable",
        trigger=ProtocolTrigger(task_type_pattern="product.*"),
        execution=ProtocolExecution(mode="MAX"),
    )

    protocol_ticket = ticket_from_protocol_applied(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        risk_level="low",
        estimated_cost_usd=0.1,
        protocol=protocol,
    )
    ledger.record_decision_ticket(protocol_ticket)
    llm_ticket = ticket_from_llm_route(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        step_id=1,
        purpose="execution",
        provider="codex-mcp",
        model="gpt-5.5",
        tier="top",
        cost_usd=0.02,
    )
    ledger.record_decision_ticket(llm_ticket)

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.execution_mode == "MAX"
    assert snapshot.current_action == "应用任务协议 education.lesson.plan"
    assert snapshot.current_provider == "codex-mcp"
    assert snapshot.current_model == "gpt-5.5"
    assert snapshot.current_tier == "top"
    assert snapshot.decision_ticket_ids == [protocol_ticket.ticket_id, llm_ticket.ticket_id]


def test_state_ledger_applies_validation_tier_ticket_to_current_view() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "验证交付")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    ticket = ticket_from_validation_tier(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        risk_level="high",
        complexity_score=0.7,
        tier="tier3",
        execution_mode="SMART",
    )
    ledger.record_decision_ticket(ticket)

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.current_tier == "tier3"
    assert snapshot.decision_reason.startswith("Validation tier tier3")
    assert snapshot.latest_decision_ticket["decision_point"] == "validation_tier_selected"


def test_state_ledger_applies_context_and_skill_selection_tickets() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "选择上下文和技能")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    context_ticket = ticket_from_context_selection(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        risk_level="low",
        execution_mode="SMART",
        context_limit=1,
        context_pack=ContextPack(
            items=[
                PackedContextItem(
                    asset_id="asset-1",
                    asset_kind="memory",
                    relevance_score=0.8,
                )
            ]
        ),
    )
    skill_ticket = ticket_from_skill_selection(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        risk_level="low",
        top_k=3,
        skills=[
            SimpleNamespace(
                skill_id="lesson_planner",
                manifest=SimpleNamespace(description="Plan lessons", maturity="stable"),
            )
        ],
    )
    ledger.record_decision_ticket(context_ticket)
    ledger.record_decision_ticket(skill_ticket)

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.context_asset_ids == ["asset-1"]
    assert snapshot.skill_hints == ["lesson_planner"]
    assert snapshot.decision_ticket_ids == [context_ticket.ticket_id, skill_ticket.ticket_id]


def test_state_ledger_records_world_action_execution() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "写一份草稿")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)
    ledger.record_paused(
        task.meta.task_id,
        reason="等待审批",
        pending_confirmations=["act-1", "act-2"],
    )

    ledger.record_world_action_executed(
        task.meta.task_id,
        action_id="act-1",
        action_type="email.draft",
        gateway_mode="handler_drafted",
        external_dispatched=False,
        requires_handler=False,
        handler_id="email.draft.v1",
        artifact_ref="/safe/draft.json",
        message="Email draft created. It was not sent.",
    )

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.pending_confirmations == ["act-2"]
    assert snapshot.pending_reason == ""
    assert snapshot.current_action == "World action email.draft: handler_drafted"
    trail = snapshot.recent_events[-1]
    assert trail.kind == "world.action.executed"
    assert trail.data["handler_id"] == "email.draft.v1"
    assert trail.data["external_dispatched"] is False


def test_state_ledger_clears_action_type_confirmation_and_resumes() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "生成邮件草稿")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)
    ledger.record_paused(
        task.meta.task_id,
        reason="等待审批",
        pending_confirmations=["email.draft"],
    )

    ledger.record_world_action_executed(
        task.meta.task_id,
        action_id="act-1",
        action_type="email.draft",
        gateway_mode="handler_drafted",
        external_dispatched=False,
        requires_handler=False,
        message="Email draft created. It was not sent.",
    )
    ledger.record_resumed(task.meta.task_id, reason="all_pending_actions_executed")

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.status == "queued"
    assert snapshot.pending_confirmations == []
    assert snapshot.pending_reason == ""
    assert snapshot.recent_events[-1].kind == "task.resumed"


def test_state_ledger_records_missing_world_handler_as_pending_reason() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "发送邮件")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    ledger.record_world_action_executed(
        task.meta.task_id,
        action_id="act-1",
        action_type="message.send",
        gateway_mode="approval_gate",
        external_dispatched=False,
        requires_handler=True,
        message="No handler is attached yet.",
    )

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.pending_reason == "No handler is attached yet."
    assert snapshot.recent_events[-1].data["requires_handler"] is True


def _task_ref(owner: Owner, title: str) -> TaskRef:
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint(title, owner),
            task_type="product.ops",
            owner=owner,
            success_criteria_short=title,
        ),
        spec=TaskSpec(goal_detail=title),
    )
