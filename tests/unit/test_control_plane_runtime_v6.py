from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import kun.control_plane.runtime as runtime_module
import pytest
from kun.control_plane import (
    ArtifactManifest,
    ArtifactRecord,
    CollaborationResponse,
    CollaborationTicket,
    ExecutionContract,
    FileControlPlaneStore,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    NuoRuntimeRepairRunner,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    build_rainflow_ad_mission,
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


def test_runtime_treats_mission_director_human_acceptance_as_collaboration_not_failure() -> None:
    work_item = WorkItem(
        work_item_id="work-mission-director-final",
        mission_id="msn-v6",
        task_plan_version="v1",
        type="review",
        owner="mission-director",
        priority=90,
        expected_output="Review final delivery and request human acceptance if needed.",
    )
    runtime = _submit_runtime([work_item])
    gate = GateEvaluation(
        gate_evaluation_id="gate-mission-director-human-only",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="acceptance",
        task_type="product_development",
        rubric_version="mission-director-final-v1",
        metric_pack_version="human-target-acceptance-v1",
        north_star_verdict="partial",
        result_quality=0.7,
        speed=0.7,
        cost=0.8,
        risk=0.3,
        evidence_quality=0.84,
        collaboration_quality=0.82,
        hard_gate_failures=["human_or_target_user_acceptance_missing"],
        next_action="needs_human",
        next_state="waiting_human",
        governance_signal="mission_director_supervision",
        created_by="mission-director",
    )
    runner = StaticRunner(
        lambda item: WorkItemResult(
            status="done",
            summary="Automated evidence is ready; human or target-user acceptance is pending.",
            gate_evaluation=gate,
        )
    )

    run = runtime.run_work_item(work_item_id=work_item.work_item_id, runner=runner)

    assert run is not None
    assert run.exit_status == "succeeded"
    assert run.failure_category is None
    assert runtime.work_items[work_item.work_item_id].status == "done"
    assert runtime.missions[work_item.mission_id].status == "waiting_human"


def test_followup_upsert_does_not_reset_progressed_work_item() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-parent")])
    existing = _single_work_item(work_item_id="work-followup").model_copy(
        update={
            "status": "done",
            "priority": 50,
            "recovery_refs": ["old-evidence"],
            "artifact_manifest_ref": "manifest-existing",
        }
    )
    runtime.work_items[existing.work_item_id] = existing

    runner = StaticRunner(lambda item: WorkItemResult(status="done", summary="done"))
    started = runtime.start_work_item_run(work_item_id="work-parent", runner=runner)
    assert started is not None
    run, _ = started
    runtime.finish_work_item_run(
        run_id=run.run_id,
        result=WorkItemResult(
            status="done",
            summary="parent produced duplicate follow-up",
            followup_work_items=[
                _single_work_item(work_item_id="work-followup").model_copy(
                    update={"priority": 90, "recovery_refs": ["new-evidence"]}
                )
            ],
        ),
    )

    followup = runtime.work_items["work-followup"]
    assert followup.status == "done"
    assert followup.artifact_manifest_ref == "manifest-existing"
    assert followup.priority == 90
    assert followup.recovery_refs == ["old-evidence", "new-evidence"]


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
            rollback_refs=[test.artifact_id],
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


def test_plan_change_from_delivery_reactivates_daemon_queue() -> None:
    runtime = _submit_runtime(work_items=[_single_work_item()])

    def delivery(item: WorkItem) -> WorkItemResult:
        answer = ArtifactRecord(
            artifact_id="artifact-v1-answer",
            kind="answer",
            path_or_uri="mem://answer-v1",
            content_hash="answer-v1-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-v1-delivery",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            kind="delivery",
            artifact_refs=[answer.artifact_id],
            primary_artifact_ref=answer.artifact_id,
            evidence_refs=[answer.artifact_id],
            rollback_refs=[answer.artifact_id],
            created_by="kun",
            content_hash="manifest-v1-hash",
            supports_delivery=True,
        )
        return WorkItemResult(
            status="done",
            summary="delivery complete",
            artifacts=[answer],
            artifact_manifest=manifest,
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=manifest.artifact_refs,
                evidence_refs=manifest.evidence_refs,
            ),
        )

    runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(delivery),
    )

    assert runtime.missions["msn-v6"].status == "delivering"

    next_plan = _plan().model_copy(
        update={"plan_id": "plan-v6-v2", "version": "v2", "objective": "Continue deeper work"}
    )
    next_contract = _contract().model_copy(
        update={"contract_id": "contract-v6-v2", "task_plan_version": "v2"}
    )
    next_context = _context().model_copy(
        update={"working_context_id": "ctx-v6-v2", "task_plan_version": "v2"}
    )
    next_item = _single_work_item(work_item_id="work-v2").model_copy(
        update={"task_plan_version": "v2"}
    )

    runtime.record_plan_change(
        mission_id="msn-v6",
        task_plan=next_plan,
        execution_contract=next_contract,
        working_context=next_context,
        work_items=[next_item],
        actor="kun",
        reason="User raised the quality target after delivery gate.",
    )

    assert runtime.missions["msn-v6"].status == "changing_plan"
    assert runtime.progress_report("msn-v6").next_ready_work_item_ids == ["work-v2"]


def test_runtime_keeps_changing_plan_when_late_followup_gate_requests_repairing() -> None:
    runtime = _submit_runtime(work_items=[_single_work_item(work_item_id="work-nuo-followup")])

    class LateRepairRunner:
        runner_type: Literal["agent"] = "agent"
        runner_identity = "late-repair-runner"

        def run(self, work_item: WorkItem) -> WorkItemResult:
            runtime.transition_mission(
                mission_id=work_item.mission_id,
                target="changing_plan",
                actor="qi-runtime-governance-runner",
                reason="Qi already escalated the mission to a stricter plan-change path.",
                subject_ref=work_item.work_item_id,
            )
            return WorkItemResult(
                status="partial",
                summary="Nuo classified the blocker after Qi had already escalated the mission.",
                gate_evaluation=GateEvaluation(
                    gate_evaluation_id="gate-late-repair",
                    mission_id=work_item.mission_id,
                    task_plan_version=work_item.task_plan_version,
                    subject_ref=work_item.work_item_id,
                    stage="governance",
                    task_type="self_improvement",
                    rubric_version="runtime-followup-v1",
                    metric_pack_version="runtime-followup-v1",
                    north_star_verdict="partial",
                    result_quality=0.72,
                    speed=0.72,
                    cost=0.84,
                    risk=0.42,
                    evidence_quality=0.82,
                    collaboration_quality=0.78,
                    hard_gate_failures=["nuo_repair_requires_clean_retest"],
                    failure_category="environment_failure",
                    responsibility_scope="kun_auto",
                    confidence=0.82,
                    next_action="needs_repair",
                    next_state="repairing",
                    created_by="nuo-runtime-repair-runner",
                ),
            )

    run = runtime.run_work_item(work_item_id="work-nuo-followup", runner=LateRepairRunner())

    assert run is not None
    assert runtime.missions["msn-v6"].status == "changing_plan"
    applied_gate = runtime.gate_evaluations[str(run.gate_evaluation_ref)]
    assert applied_gate.next_action == "needs_plan_change"
    assert applied_gate.next_state == "changing_plan"


def test_ready_queue_ignores_superseded_plan_work_items() -> None:
    runtime = InMemoryControlPlane()
    old_item = _single_work_item(work_item_id="work-old").model_copy(update={"priority": 100})
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract(),
        working_context=_context(),
        work_items=[old_item],
    )
    next_plan = _plan().model_copy(
        update={"plan_id": "plan-v6-v2", "version": "v2", "objective": "Superseded target"}
    )
    next_contract = _contract().model_copy(
        update={"contract_id": "contract-v6-v2", "task_plan_version": "v2"}
    )
    next_context = _context().model_copy(
        update={"working_context_id": "ctx-v6-v2", "task_plan_version": "v2"}
    )
    next_item = _single_work_item(work_item_id="work-v2").model_copy(
        update={"task_plan_version": "v2", "priority": 10}
    )

    runtime.record_plan_change(
        mission_id="msn-v6",
        task_plan=next_plan,
        execution_contract=next_contract,
        working_context=next_context,
        work_items=[next_item],
        actor="kun",
        reason="New plan supersedes stale queued work.",
    )

    assert runtime.next_ready_work_item("msn-v6").work_item_id == "work-v2"
    assert runtime.progress_report("msn-v6").next_ready_work_item_ids == ["work-v2"]
    assert runtime.work_items["work-old"].status == "queued"


def test_runtime_blocks_rainflow_ai_stage_until_phase1_gate_passes(tmp_path: Path) -> None:
    runtime = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = runtime.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    for work_item_id, work_item in list(runtime.work_items.items()):
        runtime.work_items[work_item_id] = work_item.model_copy(update={"status": "done"})
    review_id = f"work-{mission.mission_id}-05-phase1-acceptance-review"
    stage1_id = f"work-{mission.mission_id}-06-stage1-transition-generation"
    runtime.work_items[stage1_id] = WorkItem(
        work_item_id=stage1_id,
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or package.task_plan.version,
        type="execution",
        owner="kun",
        dependencies=[review_id],
        priority=93,
        expected_output="Generate transition clips only after Phase 1 passes.",
        workspace_ref=package.execution_contract.delivery_contract["workspace_ref"],
        sandbox_ref=package.execution_contract.delivery_contract["sandbox_ref"],
        resource_locks=list(package.execution_contract.delivery_contract["resource_locks"]),
    )

    assert runtime.ready_work_items(mission.mission_id) == []

    runtime.gate_evaluations["gate-phase1-pass"] = GateEvaluation.model_validate(
        {
            "gate_evaluation_id": "gate-phase1-pass",
            "mission_id": mission.mission_id,
            "task_plan_version": mission.current_plan_version or package.task_plan.version,
            "subject_ref": review_id,
            "stage": "acceptance",
            "task_type": mission.task_type,
            "rubric_version": "rainflow-phase1-v1",
            "metric_pack_version": "rainflow-phase1-v1",
            "north_star_verdict": "pass",
            "result_quality": 0.9,
            "speed": 0.8,
            "cost": 0.8,
            "risk": 0.2,
            "evidence_quality": 0.85,
            "collaboration_quality": 0.84,
            "confidence": 0.88,
            "next_action": "continue",
            "next_state": "running",
            "created_by": "mission-director",
        }
    )

    assert [item.work_item_id for item in runtime.ready_work_items(mission.mission_id)] == [
        stage1_id
    ]

    runtime.gate_evaluations["gate-phase1-human-reject"] = GateEvaluation.model_validate(
        {
            "gate_evaluation_id": "gate-phase1-human-reject",
            "mission_id": mission.mission_id,
            "task_plan_version": mission.current_plan_version or package.task_plan.version,
            "subject_ref": review_id,
            "stage": "acceptance",
            "task_type": mission.task_type,
            "rubric_version": "human-rainflow-phase1-v1",
            "metric_pack_version": "human-rainflow-phase1-v1",
            "north_star_verdict": "partial",
            "result_quality": 0.62,
            "speed": 0.8,
            "cost": 0.8,
            "risk": 0.4,
            "evidence_quality": 0.85,
            "collaboration_quality": 0.84,
            "confidence": 0.88,
            "hard_gate_failures": ["first_3s_hook_not_visible"],
            "next_action": "needs_repair",
            "next_state": "repairing",
            "created_by": "human-supervisor",
        }
    )

    assert runtime.ready_work_items(mission.mission_id) == []

    runtime.gate_evaluations["gate-phase1-pass-after-reject"] = GateEvaluation.model_validate(
        {
            "gate_evaluation_id": "gate-phase1-pass-after-reject",
            "mission_id": mission.mission_id,
            "task_plan_version": mission.current_plan_version or package.task_plan.version,
            "subject_ref": review_id,
            "stage": "acceptance",
            "task_type": mission.task_type,
            "rubric_version": "rainflow-phase1-v1",
            "metric_pack_version": "rainflow-phase1-v1",
            "north_star_verdict": "pass",
            "result_quality": 0.91,
            "speed": 0.8,
            "cost": 0.8,
            "risk": 0.2,
            "evidence_quality": 0.86,
            "collaboration_quality": 0.85,
            "confidence": 0.89,
            "next_action": "continue",
            "next_state": "running",
            "created_by": "mission-director",
        }
    )

    assert [item.work_item_id for item in runtime.ready_work_items(mission.mission_id)] == [
        stage1_id
    ]


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


def test_runtime_can_resume_queued_repair_work_from_repairing_state() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-fails")])

    runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="failed",
                summary="tool unavailable",
                failure_category="tool_failure",
            )
        ),
    )
    assert runtime.progress_report("msn-v6").status == "repairing"
    runtime.work_items["work-repair"] = WorkItem(
        work_item_id="work-repair",
        mission_id="msn-v6",
        task_plan_version="v1",
        type="repair",
        owner="kun",
        priority=90,
        expected_output="Repair the failed execution path.",
    )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="done",
                summary="repair path completed",
            )
        ),
    )

    assert run is not None
    assert runtime.work_items["work-repair"].status == "done"


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


def test_runtime_does_not_create_qi_learning_for_pure_human_acceptance_signal(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "runtime-human-acceptance-only.json")
    runtime = InMemoryControlPlane(store=store)
    work_item = _single_work_item(work_item_id="work-mission-director-human-acceptance")
    work_item = work_item.model_copy(update={"owner": "mission-director", "type": "review"})
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
            lambda item: WorkItemResult(
                status="done",
                summary="Mission Director found only human acceptance is missing.",
                gate_evaluation=GateEvaluation(
                    gate_evaluation_id="gate-mission-director-human-acceptance",
                    mission_id=item.mission_id,
                    task_plan_version=item.task_plan_version,
                    subject_ref=item.work_item_id,
                    stage="acceptance",
                    task_type="product_development",
                    rubric_version="mission-director-test-v1",
                    metric_pack_version="mission-director-test-v1",
                    north_star_verdict="partial",
                    result_quality=0.7,
                    speed=0.7,
                    cost=0.8,
                    risk=0.45,
                    evidence_quality=0.75,
                    collaboration_quality=0.8,
                    score_breakdown={"human_acceptance_missing": 1.0},
                    thresholds={"result_quality": 0.8},
                    hard_gate_failures=["human_or_target_user_acceptance_missing"],
                    source_freshness="fresh",
                    responsibility_scope="kun_auto",
                    confidence=0.84,
                    next_action="needs_human",
                    next_state="waiting_human",
                    governance_signal="mission_director_supervision",
                    created_by="mission-director",
                ),
            )
        ),
    )

    restored = InMemoryControlPlane(store=store)
    assert run is not None
    assert not [
        item
        for item in restored.work_items.values()
        if item.work_item_id.startswith("work-qi-runtime-learning-")
    ]


def test_runtime_classifies_sandbox_process_pool_permission_as_environment_blocker() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-sandbox-permission")])

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="failed",
                summary=(
                    "ProcessPoolExecutor failed: PermissionError: [Errno 1] "
                    "Operation not permitted in os.sysconf('SC_SEM_NSEMS_MAX')"
                ),
                failure_category="evidence_failure",
            )
        ),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.failure_category == "environment_failure"
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "nuo"
    assert gate.hard_gate_failures == ["sandbox_permission_blocked"]
    assert gate.next_action == "needs_repair"


def test_runtime_nuo_failed_gate_overrides_done_work_item_status() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-sandbox-done")])

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="done",
                summary=(
                    "Attempted test run, but ProcessPoolExecutor failed with "
                    "PermissionError: [Errno 1] Operation not permitted in os.sysconf."
                ),
            )
        ),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "failed"
    assert restored_run.failure_category == "environment_failure"
    assert runtime.work_items["work-sandbox-done"].status == "failed"
    recovery = runtime.work_items["work-nuo-work-sandbox-done-fix_wrapper"]
    assert recovery.status == "queued"
    assert recovery.owner == "control-plane"


def test_runtime_preserves_partial_status_for_nuo_repair_followup() -> None:
    work_item = _single_work_item(work_item_id="work-nuo-repair").model_copy(
        update={
            "type": "repair",
            "owner": "control-plane",
            "idempotency_key": "nuo-recovery:work-main:fix_wrapper:sandbox_permission_blocked",
            "expected_output": "Repair wrapper availability or contract compatibility.",
        }
    )
    runtime = _submit_runtime([work_item])

    run = runtime.run_next_ready(
        mission_id="msn-v6", runner=NuoRuntimeRepairRunner(control_plane=runtime)
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "succeeded"
    assert restored_run.failure_category == "environment_failure"
    assert runtime.work_items["work-nuo-repair"].status == "partial"


def test_runtime_uses_report_artifact_and_work_item_rollback_refs_for_delivery_nuo() -> None:
    work_item = _single_work_item(work_item_id="work-report-with-rollback").model_copy(
        update={
            "type": "review",
            "expected_output": "Produce final validation report with rollback references.",
            "rollback_refs": ["rollback-existing"],
        }
    )
    runtime = _submit_runtime([work_item])

    def delivery_review(item: WorkItem) -> WorkItemResult:
        report = ArtifactRecord(
            artifact_id="artifact-validation-report",
            kind="report",
            path_or_uri="mem://validation-report",
            content_hash="report-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["validation_report", "report"],
        )
        return WorkItemResult(
            status="done",
            summary="Final validation report complete with attached rollback reference.",
            artifacts=[report],
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=[report.artifact_id],
                evidence_refs=[report.artifact_id],
            ),
        )

    run = runtime.run_next_ready(mission_id="msn-v6", runner=StaticRunner(delivery_review))

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "kun"
    assert "report_missing" not in gate.hard_gate_failures
    assert "rollback_refs_missing" not in gate.hard_gate_failures
    assert restored_run.failure_category is None
    assert runtime.missions["msn-v6"].status == "delivering"


def test_runtime_does_not_require_human_playtest_for_early_research_report() -> None:
    work_item = _single_work_item(work_item_id="work-product-gap-audit").model_copy(
        update={
            "type": "research",
            "phase": "phase1_intake",
            "expected_output": "Produce an engineering gap report before implementation.",
        }
    )
    runtime = InMemoryControlPlane()
    runtime.submit_mission(
        mission=_mission().model_copy(update={"task_type": "product_development"}),
        task_plan=_plan(),
        execution_contract=_contract().model_copy(
            update={"delivery_contract": {"human_playtest_required": True}}
        ),
        working_context=_context(),
        work_items=[work_item],
    )

    def report_result(item: WorkItem) -> WorkItemResult:
        report = ArtifactRecord(
            artifact_id="artifact-gap-report",
            kind="report",
            path_or_uri="mem://gap-report",
            content_hash="gap-report-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["report", "engineering_gap_report"],
        )
        return WorkItemResult(
            status="done",
            summary="Engineering gap report complete. No delivery approval is claimed.",
            artifacts=[report],
        )

    run = runtime.run_next_ready(mission_id="msn-v6", runner=StaticRunner(report_result))

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "validation-pipeline"
    assert "subjective_playtest_missing" not in gate.hard_gate_failures
    assert restored_run.failure_category is None
    assert runtime.missions["msn-v6"].status == "running"


def test_runtime_does_not_block_qi_strategy_replay_for_missing_final_human_playtest() -> None:
    work_item = _single_work_item(work_item_id="work-qi-quality-strategy").model_copy(
        update={
            "type": "governance",
            "owner": "qi",
            "expected_output": (
                "Optimize the failed quality gate and require human/player-experience "
                "evidence before final closure."
            ),
        }
    )
    artifact = ArtifactRecord(
        artifact_id="artifact-qi-strategy",
        kind="report",
        path_or_uri="mem://qi-strategy",
        content_hash="qi-strategy-hash",
        created_by="qi",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=["report", "qi_strategy_replay"],
    )
    result = WorkItemResult(
        status="failed",
        summary=(
            "Quality gate still needs plan change and human/player-experience "
            "evidence before final closure."
        ),
        artifacts=[artifact],
        failure_category="evidence_failure",
    )

    required = runtime_module._runtime_human_playtest_required(
        mission=_mission().model_copy(update={"task_type": "product_development"}),
        work_item=work_item,
        result=result,
        contract=_contract().model_copy(
            update={"delivery_contract": {"human_playtest_required": True}}
        ),
    )

    assert required is False


def test_runtime_does_not_relabel_internal_test_failure_as_human_playtest_missing() -> None:
    work_item = _single_work_item(work_item_id="work-game-internal-test").model_copy(
        update={
            "type": "test",
            "phase": "internal-test",
            "expected_output": (
                "Run build, visual, browser/static playtest, and product interaction checks."
            ),
        }
    )
    artifact = ArtifactRecord(
        artifact_id="artifact-internal-test-failure",
        kind="report",
        path_or_uri="mem://internal-test-failure",
        content_hash="internal-test-failure-hash",
        created_by="kun",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=["report", "internal_test_failure"],
    )
    result = WorkItemResult(
        status="failed",
        summary=(
            "Visual gate failed. Human/player-experience evidence is required before "
            "final closure, but this run is still an internal repair test."
        ),
        artifacts=[artifact],
        failure_category="evidence_failure",
    )

    required = runtime_module._runtime_human_playtest_required(
        mission=_mission().model_copy(update={"task_type": "product_development"}),
        work_item=work_item,
        result=result,
        contract=_contract().model_copy(
            update={"delivery_contract": {"human_playtest_required": True}}
        ),
    )

    assert required is False


def test_runtime_nuo_findings_override_runner_pass_gate() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-premature-delivery")])

    def premature_delivery(item: WorkItem) -> WorkItemResult:
        answer = ArtifactRecord(
            artifact_id="artifact-premature-answer",
            kind="answer",
            path_or_uri="mem://answer",
            content_hash="answer-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["directly_playable_game"],
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-premature-delivery",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            kind="delivery",
            artifact_refs=[answer.artifact_id],
            primary_artifact_ref=answer.artifact_id,
            evidence_refs=[answer.artifact_id],
            rollback_refs=[answer.artifact_id],
            created_by="kun",
            content_hash="manifest-hash",
            supports_delivery=True,
        )
        return WorkItemResult(
            status="done",
            summary="Final delivery ready, but mechanics only and visual missing.",
            artifacts=[answer],
            artifact_manifest=manifest,
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=manifest.artifact_refs,
                evidence_refs=manifest.evidence_refs,
            ),
        )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(premature_delivery),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "failed"
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "nuo"
    assert gate.north_star_verdict == "fail"
    assert "premature_delivery_claim" in gate.hard_gate_failures
    assert runtime.missions["msn-v6"].status == "changing_plan"


def test_runtime_forces_nuo_on_delivery_manifest_contract_visual_gap() -> None:
    runtime = InMemoryControlPlane()
    work_item = _single_work_item(work_item_id="work-contract-visual-delivery")
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract().model_copy(
            update={
                "delivery_contract": {
                    "visual_product_iteration_required": True,
                }
            }
        ),
        working_context=_context(),
        work_items=[work_item],
    )

    def delivery(item: WorkItem) -> WorkItemResult:
        answer = ArtifactRecord(
            artifact_id="artifact-contract-visual-answer",
            kind="answer",
            path_or_uri="mem://answer",
            content_hash="answer-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["directly_playable_game"],
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-contract-visual-delivery",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            kind="delivery",
            artifact_refs=[answer.artifact_id],
            primary_artifact_ref=answer.artifact_id,
            evidence_refs=[answer.artifact_id],
            rollback_refs=[answer.artifact_id],
            created_by="kun",
            content_hash="manifest-hash",
            supports_delivery=True,
        )
        return WorkItemResult(
            status="done",
            summary="Final delivery ready.",
            artifacts=[answer],
            artifact_manifest=manifest,
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=manifest.artifact_refs,
                evidence_refs=manifest.evidence_refs,
            ),
        )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(delivery),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "failed"
    gate = runtime.gate_evaluations[restored_run.gate_evaluation_ref]
    assert gate.created_by == "nuo"
    assert "premature_delivery_claim" in gate.hard_gate_failures


def test_runtime_nuo_does_not_treat_capability_name_timeout_as_environment_timeout() -> None:
    runtime = InMemoryControlPlane()
    work_item = _single_work_item(work_item_id="work-blocked-visual-delivery")
    runtime.submit_mission(
        mission=_mission().model_copy(update={"task_type": "product_development"}),
        task_plan=_plan(),
        execution_contract=_contract().model_copy(
            update={"delivery_contract": {"visual_product_iteration_required": True}}
        ),
        working_context=_context(),
        work_items=[work_item],
    )

    def blocked_delivery(item: WorkItem) -> WorkItemResult:
        capability_receipt = ArtifactRecord(
            artifact_id="artifact-capability-timeout-name",
            kind="evidence",
            path_or_uri="mem://capability-timeout-name",
            content_hash="capability-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=[
                "capability_policy_consumed",
                "cap_comparison_hermes_activity_based_long_run_timeout_instead_of_wall_clock_kill",
            ],
        )
        return WorkItemResult(
            status="blocked",
            summary=(
                "Final delivery blocked because the contract requires visual product iteration "
                "evidence, but no real visual iteration artifact exists."
            ),
            artifacts=[capability_receipt],
            failure_category="evidence_failure",
        )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(blocked_delivery),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.gate_evaluation_ref is None
    assert restored_run.failure_category == "evidence_failure"
    assert runtime.work_items["work-blocked-visual-delivery"].status == "blocked"
    assert runtime.missions["msn-v6"].status == "info_gap"


def test_runtime_does_not_route_clean_capability_receipt_to_nuo() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-clean-capability-receipt")])

    def clean_result(item: WorkItem) -> WorkItemResult:
        capability_receipt = ArtifactRecord(
            artifact_id="artifact-capability-timeout-receipt",
            kind="evidence",
            path_or_uri="mem://capability-timeout-receipt",
            content_hash="capability-timeout-receipt-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=[
                "capability_policy_consumed",
                "cap_comparison_hermes_activity_based_long_run_timeout_instead_of_wall_clock_kill",
            ],
        )
        return WorkItemResult(
            status="done",
            summary="Visual product iteration completed with evidence receipts.",
            artifacts=[capability_receipt],
        )

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(clean_result),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.exit_status == "succeeded"
    assert runtime.work_items["work-clean-capability-receipt"].status == "done"
    assert not [
        gate
        for gate in runtime.gate_evaluations.values()
        if gate.subject_ref == "work-clean-capability-receipt" and gate.created_by == "nuo"
    ]


def test_runtime_routes_mission_director_delivery_block_to_plan_change() -> None:
    runtime = _submit_runtime([_single_work_item(work_item_id="work-director-blocked-delivery")])

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="blocked",
                summary=(
                    "Final delivery blocked by Mission Director supervision. "
                    "Latest blocker=gate-mission-director-blocks-delivery; "
                    "action=needs_plan_change. KUN must change the plan before another "
                    "final delivery attempt."
                ),
                failure_category="delivery_failure",
            )
        ),
    )

    assert run is not None
    restored_run = runtime.runs[run.run_id]
    assert restored_run.gate_evaluation_ref is None
    assert restored_run.failure_category == "delivery_failure"
    assert runtime.work_items["work-director-blocked-delivery"].status == "blocked"
    assert runtime.missions["msn-v6"].status == "repairing"


def test_runtime_does_not_duplicate_mission_director_supervision_as_qi_learning() -> None:
    director_item = _single_work_item(work_item_id="work-mission-director-current")
    director_item = director_item.model_copy(update={"owner": "mission-director", "type": "review"})
    runtime = _submit_runtime([director_item])

    def director_result(item: WorkItem) -> WorkItemResult:
        gate = GateEvaluation(
            gate_evaluation_id="gate-mission-director-current",
            mission_id=item.mission_id,
            task_plan_version=item.task_plan_version,
            subject_ref=item.work_item_id,
            stage="acceptance",
            task_type="product_development",
            rubric_version="kun-v6-mission-director-v1",
            metric_pack_version="kun-v6-mission-director-v1",
            north_star_verdict="partial",
            result_quality=0.66,
            speed=0.72,
            cost=0.82,
            risk=0.58,
            evidence_quality=0.84,
            collaboration_quality=0.82,
            thresholds={"result_quality": 0.8},
            hard_gate_failures=["gate_pass_not_product_done"],
            root_cause="Mission Director found that gate pass is not final product quality.",
            responsibility_scope="kun_auto",
            confidence=0.86,
            next_action="needs_plan_change",
            next_state="changing_plan",
            governance_signal="mission_director_supervision",
            created_by="mission-director",
        )
        return WorkItemResult(status="done", summary="Needs plan change.", gate_evaluation=gate)

    run = runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(director_result),
    )

    assert run is not None
    assert "work-qi-runtime-learning-work-mission-director-current" not in runtime.work_items
    assert runtime.missions["msn-v6"].status == "changing_plan"


def test_runtime_human_playtest_recovery_ref_satisfies_nuo_acceptance_check() -> None:
    work_item = _single_work_item(work_item_id="work-final-delivery")
    work_item = work_item.model_copy(
        update={
            "phase": "final-delivery",
            "recovery_refs": ["artifact-external-human-playtest-20260524"],
        }
    )
    runtime = InMemoryControlPlane()
    runtime.submit_mission(
        mission=_mission().model_copy(update={"task_type": "product_development"}),
        task_plan=_plan(),
        execution_contract=_contract().model_copy(
            update={"delivery_contract": {"human_playtest_required": True}}
        ),
        working_context=_context(),
        work_items=[work_item],
    )

    def delivery_claim(item: WorkItem) -> WorkItemResult:
        answer = ArtifactRecord(
            artifact_id="artifact-delivery-answer",
            kind="answer",
            path_or_uri="mem://answer",
            content_hash="answer-hash",
            created_by="kun",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            supports=["directly_playable_game"],
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-delivery",
            mission_id=item.mission_id,
            work_item_id=item.work_item_id,
            kind="delivery",
            artifact_refs=[answer.artifact_id],
            primary_artifact_ref=answer.artifact_id,
            evidence_refs=[answer.artifact_id],
            review_refs=["artifact-external-human-playtest-20260524"],
            rollback_refs=[answer.artifact_id],
            created_by="kun",
            content_hash="manifest-hash",
            supports_delivery=True,
        )
        return WorkItemResult(
            status="done",
            summary="Final delivery ready with external human playtest evidence.",
            artifacts=[answer],
            artifact_manifest=manifest,
            gate_evaluation=_gate(
                work_item=item,
                next_action="ready_to_deliver",
                next_state="delivering",
                artifact_refs=manifest.artifact_refs,
                evidence_refs=manifest.evidence_refs,
            ),
        )

    run = runtime.run_next_ready(mission_id="msn-v6", runner=StaticRunner(delivery_claim))

    assert run is not None
    gate = runtime.gate_evaluations[runtime.runs[run.run_id].gate_evaluation_ref]
    assert "subjective_playtest_missing" not in gate.hard_gate_failures
    assert runtime.runs[run.run_id].exit_status == "succeeded"


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


def test_runtime_collaboration_response_closes_only_explicitly_bound_work_item() -> None:
    first = WorkItem(
        work_item_id="work-human-playtest-first",
        mission_id="msn-v6",
        task_plan_version="v1",
        type="collaboration",
        owner="operator",
        priority=95,
        expected_output="Open a human or target-user playtest ticket.",
    )
    second = first.model_copy(update={"work_item_id": "work-human-playtest-second"})
    runtime = _submit_runtime([first, second])
    ticket = CollaborationTicket(
        ticket_id="ticket-human-playtest-first",
        mission_id="msn-v6",
        type="operator_action",
        role_needed="operator",
        why_needed="Human simulator needs to unblock one playtest checkpoint.",
        context_ref=first.work_item_id,
        risk_if_skipped="KUN may keep a human-playtest item queued forever.",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        output_contract="Answer whether this checkpoint may continue.",
        auto_resolvable_by=[first.work_item_id],
    )

    runtime.record_collaboration_ticket(ticket)
    runtime.record_collaboration_response(
        CollaborationResponse(
            ticket_id=ticket.ticket_id,
            responder="human-simulator",
            answer="Continue this checkpoint; final acceptance remains pending.",
        )
    )

    assert runtime.work_items[first.work_item_id].status == "done"
    assert runtime.work_items[second.work_item_id].status == "queued"


def test_nuo_clean_retest_closes_only_explicitly_bound_human_ticket() -> None:
    work_item = WorkItem(
        work_item_id="work-permission-clean-retest",
        mission_id="msn-v6",
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        expected_output="Clean retest proves workspace write permission is restored.",
        recovery_refs=["ticket-write-permission"],
        workspace_ref="workspace:///tmp/product",
        resource_locks=["workspace:/tmp/product"],
    )
    runtime = _submit_runtime([work_item])
    permission_ticket = CollaborationTicket(
        ticket_id="ticket-write-permission",
        mission_id="msn-v6",
        type="operator_action",
        role_needed="operator",
        why_needed="Approve or restore workspace write access.",
        context_ref=work_item.work_item_id,
        risk_if_skipped="KUN cannot write the product workspace.",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        output_contract="Confirm workspace is writable.",
        auto_resolvable_by=[work_item.work_item_id],
    )
    direction_ticket = CollaborationTicket(
        ticket_id="ticket-product-direction",
        mission_id="msn-v6",
        type="user_decision",
        role_needed="customer",
        why_needed="Choose whether the product direction should change before continuing.",
        decision_options=["continue", "change_direction"],
        recommended_option="continue",
        context_ref="ctx-product-direction",
        risk_if_skipped="KUN may bypass the user's product intent.",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        output_contract="Product direction decision.",
    )
    runtime.record_collaboration_ticket(permission_ticket)
    runtime.record_collaboration_ticket(direction_ticket)
    gate = _gate(
        work_item=work_item,
        next_action="continue",
        next_state="running",
        artifact_refs=["artifact-clean-retest"],
    ).model_copy(
        update={
            "created_by": "nuo-runtime-repair-runner",
            "governance_signal": "nuo_clean_retest_passed",
        }
    )

    runtime.run_next_ready(
        mission_id="msn-v6",
        runner=StaticRunner(
            lambda _item: WorkItemResult(
                status="done",
                summary="workspace write clean retest passed",
                gate_evaluation=gate,
            )
        ),
    )

    assert runtime.collaboration_tickets["ticket-write-permission"].status == "closed"
    assert (
        work_item.work_item_id
        in runtime.collaboration_tickets["ticket-write-permission"].resolution_refs
    )
    assert any(
        ref.startswith("gate-")
        for ref in runtime.collaboration_tickets["ticket-write-permission"].resolution_refs
    )
    assert runtime.collaboration_tickets["ticket-product-direction"].status == "open"
