from __future__ import annotations

from datetime import UTC, datetime

from kun.control_plane import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ControlPlaneStore,
    ExecutionContract,
    GateEvaluation,
    InMemoryControlPlaneStore,
    LedgerEvent,
    Mission,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_FIXED_DEADLINE = datetime(2026, 1, 1, 1, tzinfo=UTC)


def _store() -> ControlPlaneStore:
    return InMemoryControlPlaneStore()


def _mission(mission_id: str) -> Mission:
    return Mission(
        mission_id=mission_id,
        owner="customer",
        objective=f"Deliver {mission_id}",
        task_type="product_development",
    )


def _task_plan(mission_id: str) -> TaskPlan:
    return TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version="v1",
        objective=f"Deliver {mission_id}",
        acceptance_criteria=["Useful and verified."],
        constraints=["No unapproved external action."],
        approval_status="approved",
    )


def _execution_contract(mission_id: str) -> ExecutionContract:
    return ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        allowed_actions=["research", "execute", "test"],
        permissions=["local_write"],
    )


def _working_context(mission_id: str) -> WorkingContext:
    return WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        audience="operator",
        scope="mission",
        summary=f"Context for {mission_id}",
        acceptance_criteria=["Useful and verified."],
        constraints=["No unapproved external action."],
    )


def _work_item(mission_id: str, work_item_id: str | None = None) -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id or f"work-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        expected_output="Traceable result.",
    )


def _run_record(work_item_id: str, run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        work_item_id=work_item_id,
        runner_type="agent",
        runner_identity="kun-test-runner",
        started_at=_FIXED_TIME,
    )


def _artifact_record(mission_id: str) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=f"artifact-{mission_id}",
        kind="answer",
        path_or_uri=f"mem://{mission_id}/answer",
        content_hash=f"hash-{mission_id}",
        created_by="kun",
        mission_id=mission_id,
        work_item_id=f"work-{mission_id}",
    )


def _artifact_manifest(mission_id: str) -> ArtifactManifest:
    return ArtifactManifest(
        manifest_id=f"manifest-{mission_id}",
        mission_id=mission_id,
        work_item_id=f"work-{mission_id}",
        kind="run",
        artifact_refs=[f"artifact-{mission_id}"],
        created_by="kun",
        content_hash=f"manifest-hash-{mission_id}",
    )


def _ledger_event(mission_id: str, event_id: str | None = None) -> LedgerEvent:
    return LedgerEvent(
        event_id=event_id or f"ledger-{mission_id}",
        mission_id=mission_id,
        sequence=1,
        event_type="state_change",
        actor="kun",
        time=_FIXED_TIME,
        correlation_id=mission_id,
        subject_ref=mission_id,
        after={"status": "planning"},
        idempotency_key=f"{mission_id}:state-change:1",
    )


def _gate_evaluation(mission_id: str) -> GateEvaluation:
    return GateEvaluation(
        gate_evaluation_id=f"gate-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        subject_ref=f"work-{mission_id}",
        stage="workitem",
        task_type="product_development",
        rubric_version="rubric-v6",
        metric_pack_version="north-star-v6",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.7,
        cost=0.6,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        confidence=0.85,
        next_action="continue",
        next_state="running",
        created_by="kun",
    )


def _collaboration_ticket(mission_id: str) -> CollaborationTicket:
    return CollaborationTicket(
        ticket_id=f"ticket-{mission_id}",
        mission_id=mission_id,
        type="user_decision",
        role_needed="customer",
        why_needed="Need approval before continuing.",
        decision_options=["approve", "hold"],
        recommended_option="approve",
        context_ref=f"ctx-{mission_id}",
        risk_if_skipped="May execute the wrong scope.",
        deadline=_FIXED_DEADLINE,
        output_contract="Choose one option and explain constraints.",
    )


def _acceptance_review(mission_id: str) -> AcceptanceReview:
    return AcceptanceReview(
        acceptance_id=f"accept-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        delivery_manifest_ref=f"manifest-{mission_id}",
        gate_evaluation_ref=f"gate-{mission_id}",
        reviewer="customer",
        decision="accepted",
        satisfaction=0.9,
        reason="Meets the requested outcome.",
    )


def _capability_profile(capability_id: str) -> CapabilityProfile:
    return CapabilityProfile(
        capability_id=capability_id,
        capability_name=f"Capability {capability_id}",
        promotion_stage="replay",
        evidence_refs=["artifact-evidence"],
    )


def test_memory_store_round_trips_all_v6_records_and_mission_scoped_lists() -> None:
    store = _store()
    mission_a = _mission("msn-a")
    mission_b = _mission("msn-b")

    for mission in [mission_a, mission_b]:
        store.put_mission(mission)
        store.put_task_plan(_task_plan(mission.mission_id))
        store.put_execution_contract(_execution_contract(mission.mission_id))
        store.put_working_context(_working_context(mission.mission_id))
        store.put_work_item(_work_item(mission.mission_id))
        store.put_run_record(_run_record(f"work-{mission.mission_id}", f"run-{mission.mission_id}"))
        store.put_artifact_record(_artifact_record(mission.mission_id))
        store.put_artifact_manifest(_artifact_manifest(mission.mission_id))
        store.put_ledger_event(_ledger_event(mission.mission_id))
        store.put_gate_evaluation(_gate_evaluation(mission.mission_id))
        store.put_collaboration_ticket(_collaboration_ticket(mission.mission_id))
        store.put_acceptance_review(_acceptance_review(mission.mission_id))

    capability = store.put_capability_profile(_capability_profile("cap-research"))

    assert store.get_mission("msn-a") == mission_a
    assert store.get_task_plan("plan-msn-a") == _task_plan("msn-a")
    assert store.get_execution_contract("contract-msn-a") == _execution_contract("msn-a")
    assert store.get_working_context("ctx-msn-a") == _working_context("msn-a")
    assert store.get_work_item("work-msn-a") == _work_item("msn-a")
    assert store.get_run_record("run-msn-a") == _run_record("work-msn-a", "run-msn-a")
    assert store.get_artifact_record("artifact-msn-a") == _artifact_record("msn-a")
    assert store.get_artifact_manifest("manifest-msn-a") == _artifact_manifest("msn-a")
    assert store.get_ledger_event("ledger-msn-a") == _ledger_event("msn-a")
    assert store.get_gate_evaluation("gate-msn-a") == _gate_evaluation("msn-a")
    assert store.get_collaboration_ticket("ticket-msn-a") == _collaboration_ticket("msn-a")
    assert store.get_acceptance_review("accept-msn-a") == _acceptance_review("msn-a")
    assert store.get_capability_profile("cap-research") == capability

    assert [mission.mission_id for mission in store.list_missions()] == ["msn-a", "msn-b"]
    assert [plan.plan_id for plan in store.list_task_plans(mission_id="msn-a")] == ["plan-msn-a"]
    assert [
        contract.contract_id
        for contract in store.list_execution_contracts(mission_id="msn-a")
    ] == ["contract-msn-a"]
    assert [
        context.working_context_id
        for context in store.list_working_contexts(mission_id="msn-a")
    ] == ["ctx-msn-a"]
    assert [item.work_item_id for item in store.list_work_items(mission_id="msn-a")] == [
        "work-msn-a"
    ]
    assert [run.run_id for run in store.list_run_records(mission_id="msn-a")] == ["run-msn-a"]
    assert [
        artifact.artifact_id for artifact in store.list_artifact_records(mission_id="msn-a")
    ] == ["artifact-msn-a"]
    assert [
        manifest.manifest_id for manifest in store.list_artifact_manifests(mission_id="msn-a")
    ] == ["manifest-msn-a"]
    assert [event.event_id for event in store.list_ledger_events(mission_id="msn-a")] == [
        "ledger-msn-a"
    ]
    assert [
        gate.gate_evaluation_id for gate in store.list_gate_evaluations(mission_id="msn-a")
    ] == ["gate-msn-a"]
    assert [
        ticket.ticket_id for ticket in store.list_collaboration_tickets(mission_id="msn-a")
    ] == ["ticket-msn-a"]
    assert [
        review.acceptance_id for review in store.list_acceptance_reviews(mission_id="msn-a")
    ] == ["accept-msn-a"]
    assert [profile.capability_id for profile in store.list_capability_profiles()] == [
        "cap-research"
    ]


def test_memory_store_upsert_replaces_primary_id_without_duplicate() -> None:
    store = _store()
    mission = _mission("msn-upsert")
    store.put_mission(mission)

    updated = mission.model_copy(update={"objective": "Updated objective", "status": "planning"})
    assert store.put_mission(updated) == updated
    assert store.get_mission("msn-upsert") == updated
    assert [item.mission_id for item in store.list_missions()] == ["msn-upsert"]

    work_item = _work_item("msn-upsert")
    store.put_work_item(work_item)
    running = work_item.model_copy(update={"status": "running"})
    assert store.put_work_item(running) == running
    assert store.get_work_item(work_item.work_item_id) == running
    assert [item.work_item_id for item in store.list_work_items(mission_id="msn-upsert")] == [
        "work-msn-upsert"
    ]


def test_memory_store_idempotency_key_prevents_retry_duplicates() -> None:
    store = _store()

    first_work_item = WorkItem(
        work_item_id="work-first",
        mission_id="msn-idem",
        task_plan_version="v1",
        type="external_worker",
        owner="kun",
        idempotency_key="same-external-action",
    )
    retry_work_item = first_work_item.model_copy(update={"work_item_id": "work-retry"})

    assert store.put_work_item(first_work_item).work_item_id == "work-first"
    assert store.put_work_item(retry_work_item).work_item_id == "work-first"
    assert [item.work_item_id for item in store.list_work_items(mission_id="msn-idem")] == [
        "work-first"
    ]

    first_event = _ledger_event("msn-idem", event_id="ledger-first")
    retry_event = first_event.model_copy(update={"event_id": "ledger-retry"})

    assert store.put_ledger_event(first_event).event_id == "ledger-first"
    assert store.put_ledger_event(retry_event).event_id == "ledger-first"
    assert [event.event_id for event in store.list_ledger_events(mission_id="msn-idem")] == [
        "ledger-first"
    ]


def test_memory_store_returns_copies_not_live_references() -> None:
    store = _store()
    task_plan = _task_plan("msn-copy")
    store.put_task_plan(task_plan)

    loaded = store.get_task_plan(task_plan.plan_id)
    assert loaded is not None
    loaded.known_facts.append("mutated outside store")

    reloaded = store.get_task_plan(task_plan.plan_id)
    assert reloaded is not None
    assert reloaded.known_facts == []
