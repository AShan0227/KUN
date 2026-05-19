from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane.file_store import FileControlPlaneStore
from kun.control_plane.store import ControlPlaneStore
from kun.control_plane.v6 import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    GateEvaluation,
    LedgerEvent,
    Mission,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_FIXED_DEADLINE = datetime(2026, 1, 1, 1, tzinfo=UTC)


def _store(path: Path) -> ControlPlaneStore:
    return FileControlPlaneStore(path)


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
        path_or_uri=f"file://{mission_id}/answer",
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


def test_file_store_recovers_all_v6_records_after_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.json"
    store = _store(path)

    for mission_id in ["msn-a", "msn-b"]:
        store.put_mission(_mission(mission_id))
        store.put_task_plan(_task_plan(mission_id))
        store.put_execution_contract(_execution_contract(mission_id))
        store.put_working_context(_working_context(mission_id))
        store.put_work_item(_work_item(mission_id))
        store.put_run_record(_run_record(f"work-{mission_id}", f"run-{mission_id}"))
        store.put_artifact_record(_artifact_record(mission_id))
        store.put_artifact_manifest(_artifact_manifest(mission_id))
        store.put_ledger_event(_ledger_event(mission_id))
        store.put_gate_evaluation(_gate_evaluation(mission_id))
        store.put_collaboration_ticket(_collaboration_ticket(mission_id))
        store.put_acceptance_review(_acceptance_review(mission_id))
    store.put_capability_profile(_capability_profile("cap-research"))

    rebuilt = _store(path)

    assert rebuilt.get_mission("msn-a") == _mission("msn-a")
    assert rebuilt.get_task_plan("plan-msn-a") == _task_plan("msn-a")
    assert rebuilt.get_execution_contract("contract-msn-a") == _execution_contract("msn-a")
    assert rebuilt.get_working_context("ctx-msn-a") == _working_context("msn-a")
    assert rebuilt.get_work_item("work-msn-a") == _work_item("msn-a")
    assert rebuilt.get_run_record("run-msn-a") == _run_record("work-msn-a", "run-msn-a")
    assert rebuilt.get_artifact_record("artifact-msn-a") == _artifact_record("msn-a")
    assert rebuilt.get_artifact_manifest("manifest-msn-a") == _artifact_manifest("msn-a")
    assert rebuilt.get_ledger_event("ledger-msn-a") == _ledger_event("msn-a")
    assert rebuilt.get_gate_evaluation("gate-msn-a") == _gate_evaluation("msn-a")
    assert rebuilt.get_collaboration_ticket("ticket-msn-a") == _collaboration_ticket("msn-a")
    assert rebuilt.get_acceptance_review("accept-msn-a") == _acceptance_review("msn-a")
    assert rebuilt.get_capability_profile("cap-research") == _capability_profile("cap-research")

    assert [mission.mission_id for mission in rebuilt.list_missions()] == ["msn-a", "msn-b"]
    assert [
        context.working_context_id for context in rebuilt.list_working_contexts(mission_id="msn-a")
    ] == ["ctx-msn-a"]
    assert [item.work_item_id for item in rebuilt.list_work_items(mission_id="msn-a")] == [
        "work-msn-a"
    ]
    assert [run.run_id for run in rebuilt.list_run_records(mission_id="msn-a")] == ["run-msn-a"]
    assert [
        manifest.manifest_id for manifest in rebuilt.list_artifact_manifests(mission_id="msn-a")
    ] == ["manifest-msn-a"]
    assert [
        gate.gate_evaluation_id for gate in rebuilt.list_gate_evaluations(mission_id="msn-a")
    ] == ["gate-msn-a"]


def test_file_store_persists_primary_id_upsert_without_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.json"
    store = _store(path)
    mission = _mission("msn-upsert")
    store.put_mission(mission)

    updated = mission.model_copy(update={"objective": "Updated objective", "status": "planning"})
    assert store.put_mission(updated) == updated

    rebuilt = _store(path)
    assert rebuilt.get_mission("msn-upsert") == updated
    assert [mission.mission_id for mission in rebuilt.list_missions()] == ["msn-upsert"]


def test_file_store_recovers_idempotency_index_after_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.json"
    store = _store(path)
    first_work_item = WorkItem(
        work_item_id="work-first",
        mission_id="msn-idem",
        task_plan_version="v1",
        type="external_worker",
        owner="kun",
        idempotency_key="same-external-action",
    )
    first_event = _ledger_event("msn-idem", event_id="ledger-first")

    store.put_work_item(first_work_item)
    store.put_ledger_event(first_event)

    rebuilt = _store(path)
    retry_work_item = first_work_item.model_copy(update={"work_item_id": "work-retry"})
    retry_event = first_event.model_copy(update={"event_id": "ledger-retry"})

    assert rebuilt.put_work_item(retry_work_item).work_item_id == "work-first"
    assert rebuilt.put_ledger_event(retry_event).event_id == "ledger-first"
    assert [item.work_item_id for item in rebuilt.list_work_items(mission_id="msn-idem")] == [
        "work-first"
    ]
    assert [event.event_id for event in rebuilt.list_ledger_events(mission_id="msn-idem")] == [
        "ledger-first"
    ]


def test_file_store_writes_json_snapshot_atomically_to_target_path(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "control-plane.json"
    store = _store(path)

    store.put_mission(_mission("msn-json"))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["missions"][0]["mission_id"] == "msn-json"
    assert list(path.parent.glob("*.tmp")) == []
