from __future__ import annotations

from types import SimpleNamespace

from kun.context.packer import ContextPack, PackedContextItem
from kun.core.ooda_loop import OODACycle, OODAState
from kun.core.state_ledger import StateLedger, StateLedgerEntry, replay_state_ledger_story
from kun.datamodel.decision_ticket import (
    ticket_from_budget_policy,
    ticket_from_context_selection,
    ticket_from_emergent_switch,
    ticket_from_execution_mode_selection,
    ticket_from_llm_route,
    ticket_from_memory_policy_selection,
    ticket_from_ooda_checkpoint,
    ticket_from_preflight_guard,
    ticket_from_protocol_applied,
    ticket_from_skill_selection,
    ticket_from_step_action_selection,
    ticket_from_validation_tier,
)
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.concurrency import PendingActionSpec, PreConflictReport, ResourceIntent
from kun.memory.policy import MemoryDepth, MemoryLayer, MemoryPolicyTicket
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


def test_state_ledger_consumes_sparse_decision_tickets() -> None:
    owner = Owner(tenant_id="tenant-1", user_id="user-1")
    task_ref = TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("复杂产品运营", owner),
            task_type="mission.product_ops",
            risk_level="medium",
            complexity_score=0.7,
            owner=owner,
            estimated_cost_usd=0.8,
            success_criteria_short="推进产品运营任务",
        ),
        spec=TaskSpec(goal_detail="持续推进一个产品运营任务"),
    )
    ledger = StateLedger()
    ledger.record_task_created(task_ref, tenant_id=owner.tenant_id)

    mode_ticket = ticket_from_execution_mode_selection(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        execution_mode="MAX",
        task_type=task_ref.meta.task_type,
        complexity_score=task_ref.meta.complexity_score,
        estimated_cost_usd=task_ref.meta.estimated_cost_usd,
    )
    memory_ticket = ticket_from_memory_policy_selection(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        policy=MemoryPolicyTicket(
            use_memory=True,
            depth=MemoryDepth.DEEP,
            layers=[MemoryLayer.TASK_RESULT, MemoryLayer.META_DECISION],
            max_items=4,
            reason="long mission needs process and meta-decision memory",
        ),
    )
    hermes_ticket = ticket_from_step_action_selection(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        step_id=1,
        hermes_step=SimpleNamespace(
            action_type="call_skill",
            action_payload={"skill_id": "product_ops"},
            confidence=0.8,
            thought="需要调用产品运营 skill",
        ),
    )
    preflight_ticket = ticket_from_preflight_guard(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        report=PreConflictReport(
            resources=[
                ResourceIntent(
                    resource="external:email",
                    mode="write",
                    reason="可能需要联系外部协作者",
                )
            ],
            conflicts=[],
            blocking=False,
        ),
        pending_actions=[
            PendingActionSpec(
                action_type="email.send",
                target_ref="partner@example.com",
                payload={"description": "发送外部邮件"},
            )
        ],
    )
    switch_ticket = ticket_from_emergent_switch(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        step_id=1,
        signals=["path_cost_spike"],
        evaluation=SimpleNamespace(
            should_switch=False,
            blocked_by="not_enough_evidence",
            switch_score=0.4,
            reason="证据不足，暂不切路",
        ),
    )
    ooda_ticket = ticket_from_ooda_checkpoint(
        tenant_id=owner.tenant_id,
        task_id=task_ref.meta.task_id,
        risk_level=task_ref.meta.risk_level,
        checkpoint="reflect",
        cycle=OODACycle(task_ref=task_ref.meta.task_id, current_state=OODAState.REFLECT),
        status="needs_review",
        reason="budget drift",
        step_id=1,
    )

    for ticket in (
        mode_ticket,
        memory_ticket,
        hermes_ticket,
        preflight_ticket,
        switch_ticket,
        ooda_ticket,
    ):
        ledger.record_decision_ticket(ticket)

    snapshot = ledger.snapshot(task_ref.meta.task_id)

    assert snapshot is not None
    assert snapshot.execution_mode == "MAX"
    assert snapshot.status == "paused"
    assert snapshot.pending_reason == "1 pending approval action(s)"
    assert "preflight_guard_blocked" in snapshot.alert_flags
    assert "emergent_switch_blocked" in snapshot.alert_flags
    assert "ooda:needs_review" in snapshot.alert_flags
    assert snapshot.current_action == "OODA reflect: reflect"
    assert snapshot.latest_decision_ticket is not None
    assert snapshot.latest_decision_ticket["decision_point"] == "ooda_checkpoint"
    assert snapshot.recent_events[-1].kind == "decision.ticket"


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


def test_state_ledger_persists_and_updates_current_snapshot() -> None:
    store = _DictStateLedgerStore()
    ledger = StateLedger(store=store)
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "持久账本", task_id="task-persist")

    ledger.record_task_created(task, tenant_id=owner.tenant_id)
    ledger.record_current_action(
        task.meta.task_id,
        step_id=2,
        description="写入长期状态账本",
        skill_hint="state_ledger",
    )

    persisted = store.get(task_id=task.meta.task_id, tenant_id=owner.tenant_id)

    assert persisted is not None
    assert persisted.current_step == 2
    assert persisted.current_action == "写入长期状态账本"
    assert persisted.current_skill == "state_ledger"


def test_state_ledger_persistent_snapshots_are_tenant_scoped_after_rebuild() -> None:
    store = _DictStateLedgerStore()
    owner_a = Owner(tenant_id="tenant-a", user_id="user-a")
    owner_b = Owner(tenant_id="tenant-b", user_id="user-b")
    task_a = _task_ref(owner_a, "租户 A", task_id="task-shared")
    task_b = _task_ref(owner_b, "租户 B", task_id="task-shared")
    ledger_a = StateLedger(store=store)
    ledger_b = StateLedger(store=store)

    ledger_a.record_task_created(task_a, tenant_id=owner_a.tenant_id)
    ledger_a.record_paused(task_a.meta.task_id, reason="tenant-a-only")
    ledger_b.record_task_created(task_b, tenant_id=owner_b.tenant_id)

    rebuilt = StateLedger(store=store)
    snapshot_a = rebuilt.snapshot("task-shared", tenant_id="tenant-a")
    snapshot_b = rebuilt.snapshot("task-shared", tenant_id="tenant-b")
    active_a = rebuilt.active_snapshots(tenant_id="tenant-a")

    assert snapshot_a is not None
    assert snapshot_b is not None
    assert snapshot_a.pending_reason == "tenant-a-only"
    assert snapshot_b.pending_reason == ""
    assert [entry.tenant_id for entry in active_a] == ["tenant-a"]


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
    assert snapshot.latest_decision_ticket is not None
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


def test_state_ledger_applies_budget_policy_ticket_to_cost_view() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "预算控制")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    ticket = ticket_from_budget_policy(
        tenant_id=owner.tenant_id,
        task_id=task.meta.task_id,
        risk_level="medium",
        level="CRITICAL",
        used_usd=1.2,
        limit_usd=1.0,
        behavior={"exploration": "halt"},
        hard_break=True,
    )
    ledger.record_decision_ticket(ticket)

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.cost_so_far_usd == 1.2
    assert snapshot.decision_reason.startswith("Budget level CRITICAL")
    assert snapshot.latest_decision_ticket is not None
    assert snapshot.latest_decision_ticket["decision_point"] == "budget_policy"


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


def test_state_ledger_records_blocked_world_action_as_current_risk() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "发送邮件")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    ledger.record_world_action_blocked(
        task.meta.task_id,
        action_id="act-1",
        action_type="email.send",
        gateway_mode="missing_idempotency_key",
        external_dispatched=False,
        requires_handler=False,
        capability_status="supported_execute",
        message="真实外发缺少幂等键，任务继续暂停。",
    )

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.status == "paused"
    assert snapshot.current_action == "World action email.send: blocked by missing_idempotency_key"
    assert snapshot.pending_confirmations == ["act-1"]
    assert snapshot.pending_reason == "真实外发缺少幂等键，任务继续暂停。"
    assert "world_action_blocked:email.send" in snapshot.alert_flags
    assert snapshot.recent_events[-1].kind == "world.action.blocked"
    assert snapshot.recent_events[-1].data["capability_status"] == "supported_execute"


def test_state_ledger_records_failed_world_action_as_current_risk() -> None:
    ledger = StateLedger()
    owner = Owner(tenant_id="tenant-a", user_id="user-a")
    task = _task_ref(owner, "调用外部 API")
    ledger.record_task_created(task, tenant_id=owner.tenant_id)

    ledger.record_world_action_failed(
        task.meta.task_id,
        action_id="act-2",
        action_type="enterprise_api.post",
        error="timeout",
    )

    snapshot = ledger.snapshot(task.meta.task_id)

    assert snapshot is not None
    assert snapshot.status == "paused"
    assert snapshot.current_action == "World action enterprise_api.post: execution failed"
    assert snapshot.pending_confirmations == ["act-2"]
    assert snapshot.pending_reason == "外部动作执行失败：timeout"
    assert "world_action_failed:enterprise_api.post" in snapshot.alert_flags
    assert snapshot.recent_events[-1].kind == "world.action.failed"


def test_state_ledger_records_code_change_from_placeholder() -> None:
    ledger = StateLedger()

    ledger.record_code_change(
        "task-code-1",
        tenant_id="tenant-code",
        path="kun/foo.py",
        mode="dry_run",
        phase="done",
        ok=True,
        applied=False,
        rolled_back=False,
        checks_passed=True,
        reason="验证候选代码路径",
        bytes_changed=42,
    )

    snapshot = ledger.snapshot("task-code-1")

    assert snapshot is not None
    assert snapshot.tenant_id == "tenant-code"
    assert snapshot.current_action == "CodeCapability dry_run kun/foo.py: 通过"
    assert snapshot.decision_reason == "验证候选代码路径"
    assert snapshot.recent_events[-1].kind == "code.change.proposed"
    assert snapshot.recent_events[-1].data["bytes_changed"] == 42


def test_state_ledger_records_credit_assignment_summary() -> None:
    ledger = StateLedger()

    ledger.record_credit_assignment(
        "task-credit-1",
        task_outcome="success",
        step_count=3,
        critical_path_step_ids=[1, 3],
        total_immediate_reward=1.75,
        resource_count=4,
        resource_kind_summaries=[
            {
                "resource_kind": "skill",
                "total_delta": 0.8,
                "mean_delta": 0.4,
                "positive_count": 2,
                "negative_count": 0,
                "resource_count": 2,
                "top_resource_keys": ["skill:writer", "skill:reviewer"],
            },
            {
                "resource_kind": "context",
                "total_delta": 0.3,
                "mean_delta": 0.3,
                "positive_count": 1,
                "negative_count": 0,
                "resource_count": 1,
                "top_resource_keys": ["memory:lesson-1"],
            },
            {
                "resource_kind": "model",
                "total_delta": -0.1,
                "mean_delta": -0.1,
                "positive_count": 0,
                "negative_count": 1,
                "resource_count": 1,
            },
        ],
    )

    snapshot = ledger.snapshot("task-credit-1")

    assert snapshot is not None
    assert snapshot.credit_assignment_count == 1
    assert snapshot.credit_assignment_summary == {
        "task_outcome": "success",
        "step_count": 3,
        "critical_path_step_ids": [1, 3],
        "total_immediate_reward": 1.75,
        "resource_count": 4,
        "resource_kind_count": 3,
        "top_resource_kinds": ["skill", "context"],
        "top_resources": ["skill:writer", "skill:reviewer", "memory:lesson-1"],
    }
    assert snapshot.resource_credit_summaries[0]["resource_kind"] == "skill"
    assert snapshot.resource_credit_summaries[0]["top_resource_keys"] == [
        "skill:writer",
        "skill:reviewer",
    ]
    assert snapshot.top_credit_resource_kinds == ["skill", "context"]
    assert snapshot.top_credit_resources == ["skill:writer", "skill:reviewer", "memory:lesson-1"]
    assert snapshot.critical_path_step_ids == [1, 3]
    assert snapshot.current_action == "完成信用归因：skill、context 贡献最高"
    assert snapshot.recent_events[-1].kind == "credit.assignment.completed"


def test_state_ledger_replay_reconstructs_task_story_from_durable_events() -> None:
    story = replay_state_ledger_story(
        "task-1",
        [
            {
                "event_id": "evt-1",
                "event_type": "task.created",
                "occurred_at": "2026-04-30T08:00:00Z",
                "task_id": "task-1",
                "summary": "created",
                "payload": {"task_id": "task-1"},
            },
            {
                "event_id": "evt-2",
                "event_type": "llm.model_route.selected",
                "occurred_at": "2026-04-30T08:01:00Z",
                "task_id": "task-1",
                "summary": "model route",
                "cost_usd": 0.02,
                "decision_ticket_id": "decision-1",
                "payload": {
                    "ticket_id": "decision-1",
                    "decision_point": "llm_model_selected",
                    "phase": "step",
                    "selected_action": "codex-mcp:gpt-5.5:top",
                    "status": "applied",
                    "reason": "purpose=execution",
                    "metadata": {"provider": "codex-mcp", "model": "gpt-5.5"},
                },
            },
            {
                "event_id": "evt-3",
                "event_type": "delivery.needs_review",
                "occurred_at": "2026-04-30T08:01:30Z",
                "task_id": "task-1",
                "summary": "needs review",
                "decision_ticket_id": "decision-2",
                "payload": {
                    "ticket_id": "decision-2",
                    "decision_point": "delivery_review",
                    "phase": "delivery",
                    "selected_action": "needs_review",
                    "status": "needs_review",
                    "reason": "人工复核更稳",
                },
            },
            {
                "event_id": "evt-4",
                "event_type": "task.pending_actions.created",
                "occurred_at": "2026-04-30T08:02:00Z",
                "task_id": "task-1",
                "summary": "approval",
                "payload": {
                    "actions": [
                        {"action_id": "act-1", "action_type": "email.send"},
                    ]
                },
            },
            {
                "event_id": "evt-5",
                "event_type": "task.pending_action.executed",
                "occurred_at": "2026-04-30T08:03:00Z",
                "task_id": "task-1",
                "summary": "world action",
                "payload": {
                    "action_id": "act-1",
                    "action_type": "email.send",
                    "external_dispatched": True,
                },
            },
            {
                "event_id": "evt-6",
                "event_type": "credit.assignment.completed",
                "occurred_at": "2026-04-30T08:03:30Z",
                "task_id": "task-1",
                "summary": "credit",
                "payload": {
                    "task_id": "task-1",
                    "task_outcome": "success",
                    "step_count": 2,
                    "critical_path_step_ids": [1, 2],
                    "total_immediate_reward": 1.2,
                    "resource_count": 3,
                    "resource_kind_summaries": [
                        {
                            "resource_kind": "context",
                            "total_delta": 0.7,
                            "mean_delta": 0.35,
                            "positive_count": 2,
                            "negative_count": 0,
                            "resource_count": 2,
                            "top_resource_keys": ["memory:lesson-1"],
                        },
                        {
                            "resource_kind": "model",
                            "total_delta": 0.2,
                            "mean_delta": 0.2,
                            "positive_count": 1,
                            "negative_count": 0,
                            "resource_count": 1,
                            "top_resource_keys": ["model:gpt-5.5"],
                        },
                    ],
                },
            },
            {
                "event_id": "evt-7",
                "event_type": "task.done",
                "occurred_at": "2026-04-30T08:04:00Z",
                "task_id": "task-1",
                "summary": "done",
                "cost_usd": 0.03,
                "payload": {"status": "done"},
            },
        ],
    )

    assert story["status"] == "done"
    assert story["decision_count"] == 2
    assert story["decision_summary"] == {
        "llm_model_selected": 1,
        "delivery_review": 1,
    }
    assert story["decision_status_summary"]["needs_review"] == 1
    assert story["needs_review_decision_count"] == 1
    assert story["world_action_count"] == 1
    assert story["external_action_count"] == 1
    assert story["pending_confirmations"] == []
    assert story["model_routes"] == ["codex-mcp:gpt-5.5:top"]
    assert story["total_cost_usd"] == 0.05
    assert story["credit_assignment_count"] == 1
    assert story["top_credit_resource_kinds"] == ["context", "model"]
    assert story["top_credit_resources"] == ["memory:lesson-1", "model:gpt-5.5"]
    assert story["critical_path_step_ids"] == [1, 2]
    assert story["credit_assignment_summary"]["resource_kind_count"] == 2
    assert story["credit_assignment_summary"]["top_resources"] == [
        "memory:lesson-1",
        "model:gpt-5.5",
    ]
    assert story["resource_credit_summaries"][0]["resource_kind"] == "context"
    assert story["reconstruction_confidence"] > 0.7
    assert "missing_terminal_status_event" not in story["gaps"]


def test_state_ledger_replay_is_honest_about_missing_facts() -> None:
    story = replay_state_ledger_story(
        "task-2",
        [
            {
                "event_id": "evt-1",
                "event_type": "task.step.completed",
                "occurred_at": "2026-04-30T08:00:00Z",
                "task_id": "task-2",
                "summary": "step",
                "payload": {"step_id": 1, "cost_delta_usd": 0.01},
            }
        ],
        history_limit_reached=True,
    )

    assert story["status"] == "unknown"
    assert "history_may_be_truncated" in story["gaps"]
    assert "missing_task_created_event" in story["gaps"]
    assert "missing_terminal_status_event" in story["gaps"]
    assert "missing_decision_ticket_events" in story["gaps"]


class _DictStateLedgerStore:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], StateLedgerEntry] = {}

    def save(self, entry: StateLedgerEntry) -> None:
        self.entries[(entry.tenant_id, entry.task_id)] = entry.model_copy(deep=True)

    def get(self, *, task_id: str, tenant_id: str) -> StateLedgerEntry | None:
        entry = self.entries.get((tenant_id, task_id))
        return entry.model_copy(deep=True) if entry is not None else None

    def list_active(self, *, tenant_id: str, limit: int = 50) -> list[StateLedgerEntry]:
        entries = [
            entry
            for (entry_tenant_id, _), entry in self.entries.items()
            if entry_tenant_id == tenant_id and entry.status in {"queued", "running", "paused"}
        ]
        entries.sort(key=lambda item: item.updated_at, reverse=True)
        return [entry.model_copy(deep=True) for entry in entries[:limit]]


def _task_ref(owner: Owner, title: str, *, task_id: str | None = None) -> TaskRef:
    meta_kwargs = {
        "fingerprint": TaskMeta.compute_fingerprint(title, owner),
        "task_type": "product.ops",
        "owner": owner,
        "success_criteria_short": title,
    }
    if task_id is not None:
        meta_kwargs["task_id"] = task_id
    return TaskRef(
        meta=TaskMeta(**meta_kwargs),
        spec=TaskSpec(goal_detail=title),
    )
