from __future__ import annotations

from kun.core.state_ledger import StateLedger
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
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
