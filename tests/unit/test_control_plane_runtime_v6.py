from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from kun.control_plane import (
    ArtifactManifest,
    ArtifactRecord,
    CollaborationTicket,
    ExecutionContract,
    FileControlPlaneStore,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
)


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "kun-test-runner"

    def __init__(self, handler: Callable[[WorkItem], WorkItemResult]) -> None:
        self._handler = handler

    def run(self, work_item: WorkItem) -> WorkItemResult:
        return self._handler(work_item)


def _mission() -> Mission:
    return Mission(
        mission_id="msn-v6",
        owner="customer",
        objective="Deliver a traceable product result",
        task_type="product_development",
        status="contracted",
    )


def _plan(*, approved: bool = True, info_gaps: list[str] | None = None) -> TaskPlan:
    return TaskPlan(
        plan_id="plan-v6",
        mission_id="msn-v6",
        version="v1",
        objective="Deliver a traceable product result",
        known_facts=["User wants result quality first."],
        info_gaps=info_gaps or [],
        acceptance_criteria=["Result is useful and verified."],
        constraints=["No unsafe external action without approval."],
        evidence_plan=["Attach evidence, tests, and review refs."],
        decomposition=["research", "delivery"],
        worker_plan=["research worker then delivery worker"],
        merge_plan=["merge evidence into final manifest"],
        test_plan=["run delivery gate"],
        rollback_plan=["return to repair if gate fails"],
        approval_status="approved" if approved else "draft",
    )


def _contract() -> ExecutionContract:
    return ExecutionContract(
        contract_id="contract-v6",
        mission_id="msn-v6",
        task_plan_version="v1",
        allowed_actions=["research", "execute", "test", "report"],
        forbidden_actions=["publish_without_approval"],
        permissions=["local_write"],
        budget={"usd": 10.0},
    )


def _context() -> WorkingContext:
    return WorkingContext(
        working_context_id="ctx-v6",
        mission_id="msn-v6",
        task_plan_version="v1",
        audience="operator",
        scope="mission",
        summary="Deliver the mission with evidence and gates.",
        critical_facts=["Quality is the hard gate."],
        acceptance_criteria=["Result is useful and verified."],
        constraints=["No unsafe external action without approval."],
    )


def _work_items() -> list[WorkItem]:
    return [
        WorkItem(
            work_item_id="work-research",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="research",
            owner="kun",
            priority=80,
            expected_output="Evidence pack",
        ),
        WorkItem(
            work_item_id="work-delivery",
            mission_id="msn-v6",
            task_plan_version="v1",
            type="execution",
            owner="kun",
            dependencies=["work-research"],
            priority=60,
            expected_output="Delivery manifest",
        ),
    ]


def _single_work_item(*, work_item_id: str = "work-runtime") -> WorkItem:
    return WorkItem(
        work_item_id=work_item_id,
        mission_id="msn-v6",
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=80,
        expected_output="Runtime execution result",
    )


def _gate(
    *,
    work_item: WorkItem,
    next_action: str,
    next_state: str,
    artifact_refs: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    test_refs: list[str] | None = None,
) -> GateEvaluation:
    return GateEvaluation.model_validate(
        {
            "mission_id": work_item.mission_id,
            "task_plan_version": work_item.task_plan_version,
            "subject_ref": work_item.work_item_id,
            "stage": "delivery" if next_action == "ready_to_deliver" else "workitem",
            "task_type": "product_development",
            "rubric_version": "rubric-v6",
            "metric_pack_version": "north-star-v6",
            "north_star_verdict": "pass",
            "result_quality": 0.9,
            "speed": 0.7,
            "cost": 0.7,
            "risk": 0.2,
            "evidence_quality": 0.85,
            "collaboration_quality": 0.8,
            "artifact_refs": artifact_refs or [],
            "evidence_refs": evidence_refs or [],
            "test_refs": test_refs or [],
            "confidence": 0.86,
            "next_action": next_action,
            "next_state": next_state,
            "created_by": "kun",
        }
    )


def _submit_runtime(work_items: list[WorkItem] | None = None) -> InMemoryControlPlane:
    runtime = InMemoryControlPlane()
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract(),
        working_context=_context(),
        work_items=work_items or _work_items(),
    )
    return runtime


def test_runtime_hydrates_from_file_store_after_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "control-plane.json"
    runtime = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract(),
        working_context=_context(),
        work_items=_work_items(),
    )

    restored = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    report = restored.progress_report("msn-v6")

    assert report.status == "queued"
    assert report.total_work_items == 2
    assert report.next_ready_work_item_ids == ["work-research"]
    assert restored.working_contexts["ctx-v6"] == _context()


def test_runtime_submits_contracted_mission_and_runs_to_delivery() -> None:
    runtime = _submit_runtime()

    first = runtime.next_ready_work_item("msn-v6")
    assert first is not None
    assert first.work_item_id == "work-research"

    runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda item: WorkItemResult(
                status="done",
                summary="research complete",
                gate_evaluation=_gate(work_item=item, next_action="continue", next_state="running"),
            )
        ),
    )
    assert runtime.progress_report("msn-v6").next_ready_work_item_ids == ["work-delivery"]

    def delivery(item: WorkItem) -> WorkItemResult:
        answer = ArtifactRecord(
            artifact_id="artifact-answer",
            kind="answer",
            path_or_uri="mem://answer",
            content_hash="answer-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
        )
        evidence = ArtifactRecord(
            artifact_id="artifact-evidence",
            kind="evidence",
            path_or_uri="mem://evidence",
            content_hash="evidence-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["delivery"],
            source_quality="credible",
        )
        test = ArtifactRecord(
            artifact_id="artifact-test",
            kind="test_result",
            path_or_uri="mem://test",
            content_hash="test-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-delivery",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            kind="delivery",
            artifact_refs=[answer.artifact_id, evidence.artifact_id, test.artifact_id],
            primary_artifact_ref=answer.artifact_id,
            evidence_refs=[evidence.artifact_id],
            test_refs=[test.artifact_id],
            created_by="kun",
            content_hash="manifest-hash",
            supports_delivery=True,
        )
        return WorkItemResult(
            status="done",
            summary="delivery complete",
            artifacts=[answer, evidence, test],
            artifact_manifest=manifest,
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=manifest.artifact_refs,
                evidence_refs=manifest.evidence_refs,
                test_refs=manifest.test_refs,
            ),
        )

    runtime.run_next_ready(mission_id="msn-v6", runner=StaticRunner(delivery))

    report = runtime.progress_report("msn-v6")
    assert report.status == "delivering"
    assert report.work_item_counts == {"done": 2}
    assert report.artifact_manifest_count == 1
    assert report.latest_gate_action == "ready_to_deliver"
    assert report.latest_gate_verdict == "pass"
    assert report.ledger_event_count >= 6


def test_runtime_rejects_plan_before_approval_or_with_info_gaps() -> None:
    runtime = InMemoryControlPlane()

    with pytest.raises(ValueError, match="approved"):
        runtime.submit_mission(
            mission=_mission(),
            task_plan=_plan(approved=False),
            execution_contract=_contract(),
            working_context=_context(),
            work_items=_work_items(),
        )

    with pytest.raises(ValueError, match="info_gaps"):
        runtime.submit_mission(
            mission=_mission(),
            task_plan=_plan(info_gaps=["Need user budget confirmation."]),
            execution_contract=_contract(),
            working_context=_context(),
            work_items=_work_items(),
        )


def test_runtime_recovers_failed_work_item_by_failure_matrix() -> None:
    runtime = _submit_runtime()

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="failed",
                summary="tool timed out",
                failure_category="tool_failure",
            )
        ),
    )

    assert run is not None
    report = runtime.progress_report("msn-v6")
    assert report.status == "repairing"
    assert report.latest_failure_category == "environment_failure"
    assert report.work_item_counts["failed"] == 1


def test_runtime_routes_failed_work_to_nuo_and_qi(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "runtime-nuo-qi.json")
    runtime = InMemoryControlPlane(store=store)
    work_item = _single_work_item(work_item_id="work-runtime-eof")
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract(),
        working_context=_context(),
        work_items=[work_item],
    )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="failed",
                summary="unexpected EOF while reading response body",
                failure_category="environment_failure",
            )
        ),
    )

    restored = InMemoryControlPlane(store=store)
    assert run is not None
    assert restored.runs[run.run_id].failure_category == "environment_failure"
    nuo_gates = [
        gate
        for gate in restored.gate_evaluations.values()
        if gate.created_by == "nuo" and gate.subject_ref == "work-runtime-eof"
    ]
    assert nuo_gates
    assert "network_eof" in nuo_gates[0].hard_gate_failures
    assert restored.work_items["work-nuo-work-runtime-eof-rerun"].owner == "control-plane"
    qi_item = restored.work_items["work-qi-runtime-learning-work-runtime-eof"]
    assert qi_item.owner == "qi"
    assert qi_item.dependencies == []
    assert "work-runtime-eof" in qi_item.recovery_refs


def test_runtime_applies_default_validation_gate_when_runner_omits_gate() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-validation")])

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="done",
                summary="runtime result completed with traceable summary",
            )
        ),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.gate_evaluation_ref == "gate-validation-work-validation"
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "validation-pipeline"
    assert gate.north_star_verdict == "pass"
    assert gate.next_action == "continue"
    assert runtime.progress_report("msn-v6").latest_gate_ref == gate.gate_evaluation_id


def test_runtime_validation_gate_rejects_complete_status_without_trace() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-empty-result")])

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(lambda _item: WorkItemResult(status="done")),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "failed"
    assert restored_run.failure_category == "delivery_failure"
    gate = runtime.gate_evaluations["gate-validation-work-empty-result"]
    assert gate.north_star_verdict == "fail"
    assert gate.next_action == "needs_repair"
    assert "missing_summary_and_artifact_trace" in gate.hard_gate_failures
    assert runtime.progress_report("msn-v6").status == "repairing"


def test_runtime_routes_human_wait_to_collaboration_queue() -> None:
    runtime = _submit_runtime(
        [
            WorkItem(
                work_item_id="work-approval",
                mission_id="msn-v6",
                task_plan_version="v1",
                type="collaboration",
                owner="kun",
                expected_output="User approval",
            )
        ]
    )
    ticket = CollaborationTicket(
        ticket_id="ticket-approval",
        mission_id="msn-v6",
        type="user_decision",
        role_needed="customer",
        why_needed="Need approval before external action.",
        decision_options=["approve", "hold"],
        recommended_option="hold",
        context_ref="ctx-v6",
        risk_if_skipped="External action may violate user intent.",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        output_contract="Decision option and rationale.",
    )

    runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="waiting_human",
                summary="waiting for approval",
                collaboration_tickets=[ticket],
            )
        ),
    )

    report = runtime.progress_report("msn-v6")
    assert report.status == "waiting_human"
    assert report.open_collaboration_ticket_ids == ["ticket-approval"]
    assert report.next_ready_work_item_ids == []
