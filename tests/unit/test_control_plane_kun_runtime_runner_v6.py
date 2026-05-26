from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane import (
    ArtifactRecord,
    CapabilityExecutionDirective,
    CapabilityExecutionPolicy,
    ControlPlaneDaemon,
    ExecutionContract,
    InMemoryControlPlane,
    KunRuntimeTaskRunner,
    KunTaskExecutionOutput,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)
from kun.control_plane.concurrency import normalize_resource_lock_ref
from kun.control_plane.rainflow_ad_mission import RAINFLOW_AD_PRODUCTION_MODE

NOW = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)


def _runtime() -> tuple[InMemoryControlPlane, Mission]:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-real-task",
        owner="kun",
        objective="Deliver a traceable real task result",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-real-task",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["result has a concrete answer and delivery manifest"],
        constraints=["quality cannot be traded for speed"],
        evidence_plan=["record runtime task output as artifact"],
        test_plan=["delivery gate has evidence refs"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-real-task",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["execute local KUN task"],
        forbidden_actions=["skip delivery evidence"],
    )
    context = WorkingContext(
        working_context_id="ctx-real-task",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="operator",
        scope="real-task",
        summary="Runtime task runner activation test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-real-task",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="Produce a concrete audited result.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )
    return control_plane, mission


def test_kun_runtime_runner_executes_and_finalizes_real_task_work_item() -> None:
    control_plane, mission = _runtime()
    old_work = WorkItem(
        work_item_id="work-old-rejected-real-task",
        mission_id=mission.mission_id,
        task_plan_version="v0",
        type="execution",
        owner="kun",
        priority=90,
        expected_output="Rejected old result should not enter the fresh delivery manifest.",
        status="done",
    )
    old_artifact = ArtifactRecord(
        artifact_id="artifact-old-rejected-real-task",
        kind="answer",
        path_or_uri="control-plane://kun-runtime/msn-real-task/work-old-rejected-real-task",
        content_hash="sha256:old-rejected-real-task",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id=old_work.work_item_id,
        supports=["kun_runtime_task_output"],
    )
    control_plane.work_items[old_work.work_item_id] = old_work
    control_plane.artifacts[old_artifact.artifact_id] = old_artifact
    current_test_artifact = ArtifactRecord(
        artifact_id="artifact-current-real-task-test",
        kind="test_result",
        path_or_uri="control-plane://kun-runtime/msn-real-task/work-real-task/test",
        content_hash="sha256:current-real-task-test",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id="work-real-task",
        supports=["local_runtime_evidence", "runtime_test_evidence", "test_result"],
    )
    control_plane.artifacts[current_test_artifact.artifact_id] = current_test_artifact

    def fake_executor(prompt: str) -> KunTaskExecutionOutput:
        assert "Deliver a traceable real task result" in prompt
        assert "Produce a concrete audited result" in prompt
        assert "Control Plane execution attempt timestamp:" in prompt
        assert "Control Plane runner will persist artifacts" in prompt
        assert "Create a new unique output directory" in prompt
        assert "rm -rf" in prompt
        return KunTaskExecutionOutput(
            status="done",
            answer="Concrete audited result delivered.",
            raw={"source": "fake-real-task-executor"},
        )

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=fake_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-real-task-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == ["work-real-task"]
    assert report.finalized_mission_ids == [mission.mission_id]
    assert report.delivery_manifest_refs == ["manifest-kun-runtime-delivery-msn-real-task"]
    assert control_plane.work_items["work-real-task"].status == "done"
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert report.created_collaboration_ticket_ids
    assert any(
        "real_task_execution" in artifact.supports for artifact in control_plane.artifacts.values()
    )
    manifest = control_plane.artifact_manifests["manifest-kun-runtime-delivery-msn-real-task"]
    assert manifest.review_refs
    assert manifest.rollback_refs
    assert old_artifact.artifact_id not in manifest.evidence_refs
    assert current_test_artifact.artifact_id in manifest.evidence_refs
    assert current_test_artifact.artifact_id in manifest.test_refs


def test_kun_runtime_runner_finalizes_with_cancelled_observation_followup() -> None:
    control_plane, mission = _runtime()
    cancelled_followup = WorkItem(
        work_item_id="work-nuo-observation-msn-real-task-quality_gate_not_passed-old",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="cancelled",
        priority=90,
        idempotency_key=(
            "runtime-observation:nuo:work-nuo-observation-msn-real-task-quality_gate_not_passed-old"
        ),
        expected_output="Old observation follow-up retired after clean rerun.",
    )
    control_plane.work_items[cancelled_followup.work_item_id] = cancelled_followup

    def fake_executor(_prompt: str) -> KunTaskExecutionOutput:
        return KunTaskExecutionOutput(status="done", answer="Concrete audited result delivered.")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=fake_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-cancelled-followup-finalize-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.finalized_mission_ids == [mission.mission_id]
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert report.created_collaboration_ticket_ids


def test_kun_runtime_runner_blocks_required_capability_without_bound_policy() -> None:
    control_plane, _mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={"required_capability_refs": ["cap-production-required"]}
    )
    control_plane.work_items[work.work_item_id] = work

    def fake_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("runner must stop before executing without a bound policy")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=fake_executor)

    result = runner.run(work)

    assert result.status == "failed"
    assert result.failure_category == "plan_failure"
    assert "capability execution policy" in result.summary.lower()


def test_kun_runtime_runner_blocks_required_capability_without_directive_receipt() -> None:
    control_plane, _mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={"required_capability_refs": ["cap-production-required"]}
    )
    control_plane.work_items[work.work_item_id] = work
    policy = CapabilityExecutionPolicy(
        policy_id="policy-test",
        built_at=NOW,
        capability_profile_refs=["cap-production-required"],
        directives=[
            CapabilityExecutionDirective(
                directive_id="directive-runner-other-capability",
                category="runner",
                capability_refs=["cap-other"],
                summary="Carry another capability into runtime output.",
                runtime_hooks=["artifact_record"],
            )
        ],
    )

    def fake_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("runner must stop before executing uncovered capabilities")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=fake_executor)
    runner.bind_capability_execution_policy(policy)

    result = runner.run(work)

    assert result.status == "failed"
    assert result.failure_category == "plan_failure"
    assert "no executable directive receipts" in result.summary


def test_kun_runtime_runner_records_capability_behavior_receipt() -> None:
    control_plane, _mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={"required_capability_refs": ["cap-production-required"]}
    )
    control_plane.work_items[work.work_item_id] = work
    policy = CapabilityExecutionPolicy(
        policy_id="policy-test",
        built_at=NOW,
        capability_profile_refs=["cap-production-required"],
        directives=[
            CapabilityExecutionDirective(
                directive_id="directive-runner-required-capability",
                category="runner",
                capability_refs=["cap-production-required"],
                summary="Carry the required capability into runtime execution.",
                runtime_hooks=["prompt", "artifact_record"],
            )
        ],
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Concrete audited result delivered with capability behavior.",
            raw={"source": "fake-real-task-executor"},
        ),
    )
    runner.bind_capability_execution_policy(policy)

    result = runner.run(work)

    assert result.status == "done"
    assert result.artifacts
    artifact = result.artifacts[0]
    assert "capability_policy_consumed" in artifact.supports
    assert "capability_behavior_receipt" in artifact.supports


def test_kun_runtime_runner_gate_ids_stay_unique_for_long_work_item_ids() -> None:
    control_plane, _mission = _runtime()
    base = (
        "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
        "rework-0524bb04-2b31b7e9-02-material-screening-and-selection"
    )
    work_a = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": f"{base}-first-attempt",
            "expected_output": "Produce a concrete audited result.",
        }
    )
    work_b = work_a.model_copy(update={"work_item_id": f"{base}-second-attempt"})

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Concrete audited result delivered.",
        ),
    )

    result_a = runner.run(work_a)
    result_b = runner.run(work_b)

    assert result_a.gate_evaluation is not None
    assert result_b.gate_evaluation is not None
    assert (
        result_a.gate_evaluation.gate_evaluation_id != result_b.gate_evaluation.gate_evaluation_id
    )
    assert result_a.gate_evaluation.subject_ref == work_a.work_item_id
    assert result_b.gate_evaluation.subject_ref == work_b.work_item_id


def test_daemon_supplies_work_item_capability_policy_for_required_refs() -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={"required_capability_refs": ["cap-rainflow-required"]}
    )
    control_plane.work_items[work.work_item_id] = work

    def fake_executor(prompt: str) -> KunTaskExecutionOutput:
        assert "Production capabilities: cap-rainflow-required" in prompt
        return KunTaskExecutionOutput(
            status="done",
            answer="Concrete audited result delivered with cap-rainflow-required behavior.",
            raw={"source": "fake-real-task-executor"},
        )

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=fake_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-required-capability-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == ["work-real-task"]
    assert control_plane.work_items["work-real-task"].status == "done"
    runtime_artifacts = [
        artifact
        for artifact in control_plane.artifacts.values()
        if artifact.work_item_id == "work-real-task"
        and "kun_runtime_task_output" in artifact.supports
    ]
    assert runtime_artifacts
    assert "capability_behavior_receipt" in runtime_artifacts[0].supports
    assert "cap-rainflow-required" in runtime_artifacts[0].supports


def test_kun_runtime_runner_marks_review_output_as_report_evidence() -> None:
    control_plane, _mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-review-report",
            "type": "review",
            "expected_output": "Produce a validation report for delivery evidence.",
        }
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Validation report complete with delivery evidence. No approval is claimed.",
            raw={"source": "fake-review-executor"},
        ),
    )

    result = runner.run(work)

    assert result.status == "done"
    assert result.artifacts[0].kind == "review"
    assert "runtime_report" in result.artifacts[0].supports
    assert "report" in result.artifacts[0].supports


def test_kun_runtime_runner_blocks_when_local_evidence_rejects_demo(tmp_path: Path) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-demo",
            "expected_output": "Produce a review-ready RainFlow Phase 1 demo package.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_dir = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    package_dir = output_dir / "phase1-improved-demo-packages"
    package_dir.mkdir(parents=True)
    (package_dir / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "mode": "blueprint_only",
          "packages": [
            {
              "delivery_status": "blocked_not_deliverable_demo",
              "human_simulation_decision": "reject_weak_ad_grade_output",
              "human_simulation_accepted": false,
              "phase1_comparison_status": "blocked",
              "phase1_comparison_accepted": false
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    reports_dir = output_dir / "reports"
    reports_dir.mkdir()
    (reports_dir / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\n\nAI-video gate: real AI generation remains blocked.",
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Generated Phase 1 demo package files under outputs.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-blocker-evidence-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "failed"
    assert control_plane.missions[mission.mission_id].status == "repairing"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "fail"
    assert "local_human_simulation_rejected" in gate.hard_gate_failures
    assert "local_deliverable_demo_blocked" in gate.hard_gate_failures
    assert any(
        artifact.path_or_uri.endswith("manifest.json")
        and "local_runtime_evidence" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_ignores_superseded_failed_attempt_when_recovered(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-retest",
            "expected_output": "Run Phase 1 comparison and recover failed attempts when possible.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_dir = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    failed_reports_dir = output_dir / "reference-master-batch-fresh" / "reports"
    failed_reports_dir.mkdir(parents=True)
    (failed_reports_dir / "phase1-improved-demo-final-blocker-report.md").write_text(
        (
            "Decision: rejected\n"
            "blocked_not_deliverable_demo\n"
            "phase1_comparison_status: blocked\n"
            "AI-video gate remains blocked.\n"
        ),
        encoding="utf-8",
    )
    recovered_dir = output_dir / "reference-master-batch-fresh-recovered"
    recovered_dir.mkdir()
    (recovered_dir / "final-player-experience-gate.json").write_text(
        """
        {
          "status": "passed",
          "delivery_status": "rendered_review_ready_demo",
          "human_simulation_decision": "accept_phase1_demo",
          "phase1_comparison_status": "accepted",
          "blocker_codes": []
        }
        """,
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Recovered Phase 1 comparison passed with current evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-recovery-evidence-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert not gate.hard_gate_failures
    assert any(
        artifact.path_or_uri.endswith("final-player-experience-gate.json")
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        artifact.path_or_uri.endswith("phase1-improved-demo-final-blocker-report.md")
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_ignores_unselected_phase1_candidate_blockers(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-selected-package",
            "expected_output": "Package selected Phase 1 demo while retaining rejected candidates.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    package_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    package_root = package_root / "phase1-improved-demo-packages"
    rejected_dir = package_root / "phase1_improved_01_parent_trust_product_proof"
    accepted_dir = package_root / "phase1_real_01"
    rejected_dir.mkdir(parents=True)
    accepted_dir.mkdir()
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "packages": [
            {
              "demo_id": "phase1_real_01",
              "delivery_status": "rendered_review_ready_demo",
              "human_simulation_decision": "accept_phase1_demo",
              "human_simulation_accepted": true,
              "phase1_comparison_status": "accepted",
              "phase1_comparison_accepted": true,
              "blocker_codes": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (accepted_dir / "human_simulation_review.json").write_text(
        '{"decision": "accept_phase1_demo", "human_simulation_accepted": true}',
        encoding="utf-8",
    )
    (rejected_dir / "human_simulation_review.json").write_text(
        (
            '{"decision": "reject_weak_ad_grade_output", '
            '"delivery_status": "blocked_not_deliverable_demo"}'
        ),
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Selected Phase 1 package is ready; rejected candidates were retained as traces.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-selected-package-evidence-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert not gate.hard_gate_failures
    assert any(
        "phase1_real_01/human_simulation_review.json" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "phase1_improved_01_parent_trust_product_proof/human_simulation_review.json"
        in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_prefers_real_output_root_over_draft_blockers(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-real-output",
            "expected_output": "Replace draft Phase 1 package with a real rendered demo package.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    draft_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    draft_reports = draft_root / "reports"
    draft_reports.mkdir(parents=True)
    (draft_reports / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\nblocked_not_deliverable_demo\nphase1_comparison_status: blocked",
        encoding="utf-8",
    )

    real_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524-real"
    real_package = real_root / "phase1-improved-demo-packages" / "phase1_real_01"
    real_package.mkdir(parents=True)
    (real_root / "phase1-improved-demo-packages" / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "packages": [
            {
              "demo_id": "phase1_real_01",
              "delivery_status": "rendered_review_ready_demo",
              "human_simulation_decision": "accept_phase1_demo",
              "human_simulation_accepted": true,
              "phase1_comparison_status": "accepted",
              "phase1_comparison_accepted": true,
              "blocker_codes": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (real_package / "human_simulation_review.json").write_text(
        '{"decision": "accept_phase1_demo", "human_simulation_accepted": true}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Real rendered Phase 1 package superseded the draft package.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-real-output-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert not gate.hard_gate_failures
    assert any(
        "direct-20260524-real/phase1-improved-demo-packages/manifest.json" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "direct-20260524/reports/phase1-human-simulation-review.md" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_ignores_candidate_reports_when_rendered_package_accepted(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-rendered-package-with-stale-reports",
            "expected_output": "Render a review-ready Phase 1 package while retaining candidate reports.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524-fresh"
    stale_reports = output_root / "reports"
    stale_reports.mkdir(parents=True)
    (stale_reports / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\nblocked_not_deliverable_demo\nphase1_comparison_status: blocked",
        encoding="utf-8",
    )
    (stale_reports / "ai-video-stage-gate.json").write_text(
        '{"status": "blocked", "summary": "AI-video gate remains blocked."}',
        encoding="utf-8",
    )

    package_root = output_root / "reference-master-batch" / "phase1-improved-demo-packages"
    accepted_dir = package_root / "phase1_real_01"
    accepted_dir.mkdir(parents=True)
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "packages": [
            {
              "demo_id": "phase1_real_01",
              "delivery_status": "rendered_review_ready_demo",
              "human_simulation_decision": "accept_phase1_demo",
              "human_simulation_accepted": true,
              "phase1_comparison_status": "accepted",
              "phase1_comparison_accepted": true,
              "blocker_codes": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (accepted_dir / "human_simulation_review.json").write_text(
        '{"decision": "accept_phase1_demo", "human_simulation_accepted": true}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Rendered Phase 1 package passed; candidate reports are historical traces.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-rendered-package-stale-reports-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert not gate.hard_gate_failures
    assert any(
        "reference-master-batch/phase1-improved-demo-packages/phase1_real_01/"
        "human_simulation_review.json" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "reports/phase1-human-simulation-review.md" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "reports/ai-video-stage-gate.json" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_does_not_apply_delivery_blockers_to_material_screening(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-02-material-screening-and-selection",
            "expected_output": "Screen and select source material candidates before demo assembly.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    reports_dir = output_root / "reports"
    package_root = output_root / "phase1-improved-demo-packages"
    candidate_dir = package_root / "phase1_improved_01_parent_trust_product_proof"
    reports_dir.mkdir(parents=True)
    candidate_dir.mkdir(parents=True)
    (reports_dir / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\nblocked_not_deliverable_demo\nphase1_comparison_status: blocked",
        encoding="utf-8",
    )
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "mode": "candidate_screening_only",
          "packages": [
            {
              "demo_id": "phase1_improved_01_parent_trust_product_proof",
              "delivery_status": "blocked_not_deliverable_demo",
              "human_simulation_accepted": false,
              "phase1_comparison_accepted": false
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (candidate_dir / "human_simulation_review.json").write_text(
        '{"decision": "reject_weak_ad_grade_output", "human_simulation_accepted": false}',
        encoding="utf-8",
    )
    material_screening = output_root / "material-screening"
    material_screening.mkdir()
    (material_screening / "reference_candidate_screening_report_material_gate.json").write_text(
        '{"status": "passed", "selected_candidates": 8}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer=(
                "Material screening completed with selected candidates and screening "
                "evidence for the next assembly step."
            ),
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-material-screening-evidence-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert not any(failure.startswith("local_") for failure in gate.hard_gate_failures)


def test_kun_runtime_runner_blocks_when_browser_player_gate_is_blocked(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-03-assembly-and-creator-integration",
            "expected_output": (
                "Assemble the accepted material into a creator-led information-flow ad demo "
                "with playable review evidence."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    output_root.mkdir(parents=True)
    (output_root / "browser-player-gate.json").write_text(
        """
        {
          "schema_version": "rainflow_browser_player_gate_v1",
          "status": "blocked",
          "blocker": "codex_in_app_browser_unavailable",
          "error": "Browser is not available: iab"
        }
        """,
        encoding="utf-8",
    )
    (output_root / "browser-playtest-blocker.json").write_text(
        """
        {
          "schema_version": "rainflow_browser_playtest_blocker_v1",
          "status": "blocked_by_environment",
          "blocker": "no iab browser session is provisioned in this environment"
        }
        """,
        encoding="utf-8",
    )
    package_root = output_root / "phase1-improved-demo-packages"
    accepted_dir = package_root / "phase1_real_01"
    accepted_dir.mkdir(parents=True)
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "packages": [
            {
              "demo_id": "phase1_real_01",
              "delivery_status": "rendered_review_ready_demo",
              "human_simulation_accepted": true,
              "phase1_comparison_accepted": true,
              "blocker_codes": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Assembled a playable creator-led demo with local evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-browser-player-gate-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "failed"
    run = next(
        item for item in control_plane.runs.values() if item.work_item_id == work.work_item_id
    )
    assert run.failure_category == "environment_failure"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "fail"
    assert "local_browser_player_gate_blocked" in gate.hard_gate_failures


def test_kun_runtime_runner_finds_rainflow_step_alias_output_evidence(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-0524bb04-2b31b7e9-02-material-screening-and-selection"
            ),
            "expected_output": (
                "Improve material acquisition, review/screening, and selection so clips "
                "support a coherent information-flow ad narrative."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    output_root = (
        tmp_path
        / "outputs"
        / f"work-{mission.mission_id}"
        / "rainflow-adflow-v1-acceptance-rework-02-material-screening-selection-direct-20260524"
    )
    reports_dir = output_root / "reports"
    package_root = output_root / "phase1-improved-demo-packages"
    reports_dir.mkdir(parents=True)
    package_root.mkdir(parents=True)
    (reports_dir / "material-screening-selection-run-summary.json").write_text(
        '{"status": "passed", "selected_candidates": 8}',
        encoding="utf-8",
    )
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_phase1_output_manifest_v2",
          "mode": "candidate_screening_only",
          "packages": [
            {
              "demo_id": "phase1_improved_01_parent_trust_product_proof",
              "delivery_status": "screened_materials_ready"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer=(
                "Material screening completed with selected candidates, reports, "
                "and package manifest evidence for the next assembly step."
            ),
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-step-alias-evidence-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_runtime_evidence_missing" not in gate.hard_gate_failures
    assert any(
        artifact.path_or_uri.endswith("material-screening-selection-run-summary.json")
        and "local_runtime_evidence" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_prefers_exact_output_root_over_stale_step_alias(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-2b8d26fb-f4931144-01-phase1-mixed-edit-repair"
            ),
            "expected_output": "Produce RainFlow Phase 1 mixed-edit repair evidence.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "outputs" / "kundirect-phase1-mixed-edit-repair-20260524-old"
    stale_reports = stale_root / "reports"
    stale_reports.mkdir(parents=True)
    (stale_reports / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\nblocked_not_deliverable_demo",
        encoding="utf-8",
    )

    exact_root = tmp_path / "outputs" / f"{work.work_item_id}-direct-20260524"
    exact_reports = exact_root / "reports"
    exact_reports.mkdir(parents=True)
    (exact_reports / "execution-summary.md").write_text(
        "Current work item produced clean Phase 1 mixed-edit repair evidence.",
        encoding="utf-8",
    )
    (exact_reports / "player_gate_summary.json").write_text(
        '{"status": "passed", "first_3s_hook": "strong", "cta": "visible"}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Phase 1 mixed-edit repair completed with current exact output evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-exact-evidence-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_human_simulation_rejected" not in gate.hard_gate_failures
    assert any(
        artifact.path_or_uri.endswith("execution-summary.md")
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "kundirect-phase1-mixed-edit-repair-20260524-old" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_filters_stale_rework_plan_alias_roots(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-2b8d26fb-f4931144-02-material-screening-and-selection"
            ),
            "expected_output": (
                "Improve material acquisition, review/screening, and selection so clips "
                "support a coherent information-flow ad narrative."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    current_root = (
        tmp_path
        / "outputs"
        / "work-msn-rainflow-adflow-extreme-v1-02-material-screening-20260524_230649"
    )
    current_reports = current_root / "reports"
    current_reports.mkdir(parents=True)
    (current_reports / "material-screening-selection-run-summary.json").write_text(
        '{"status": "passed", "selected_candidates": 8}',
        encoding="utf-8",
    )

    stale_root = tmp_path / "outputs" / "adflow_acceptance_rework_0524bb04_material_screening"
    stale_reports = stale_root / "reports"
    stale_reports.mkdir(parents=True)
    (stale_reports / "material-screening-selection-run-summary.json").write_text(
        '{"status": "old_plan", "selected_candidates": 0}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer=(
                "Material screening completed with selected candidates, reports, "
                "and package manifest evidence for the next assembly step."
            ),
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-current-plan-evidence-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert any(
        "work-msn-rainflow-adflow-extreme-v1-02-material-screening-20260524_230649"
        in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "adflow_acceptance_rework_0524bb04_material_screening" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_filters_older_tokenless_alias_roots(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-3aec4d07-33bd0fad-01-phase1-mixed-edit-repair"
            ),
            "expected_output": "Produce RainFlow Phase 1 mixed-edit repair evidence.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "outputs" / "kundirect-phase1-mixed-edit-repair-20260524-205109"
    stale_root.mkdir(parents=True)
    (stale_root / "browser-playtest-blocker.json").write_text(
        """
        {
          "schema_version": "rainflow_browser_playtest_blocker_v1",
          "status": "blocked_by_environment",
          "blocker": "no iab browser session is provisioned in this environment"
        }
        """,
        encoding="utf-8",
    )
    old_time = NOW.timestamp() - 3600
    os.utime(stale_root, (old_time, old_time))

    fresh_root = tmp_path / "outputs" / "phase1-mixed-edit-repair-20260524-2358"
    fresh_reports = fresh_root / "01_phase1_short_clip_mix_01"
    fresh_reports.mkdir(parents=True)
    (fresh_reports / "review_report.json").write_text(
        '{"status": "ready", "blockers": [], "score": 0.946}',
        encoding="utf-8",
    )
    (fresh_reports / "review_evidence.json").write_text(
        '{"acceptance": {"status": "ready", "blockers": []}}',
        encoding="utf-8",
    )
    (fresh_root / "batch_manifest.json").write_text(
        '{"ready_count": 1, "rendered_count": 1, "items": [{"status": "ready"}]}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Phase 1 mixed-edit repair completed with current evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-tokenless-alias-evidence-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_browser_player_gate_blocked" not in gate.hard_gate_failures
    assert any(
        "phase1-mixed-edit-repair-20260524-2358" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "kundirect-phase1-mixed-edit-repair-20260524-205109" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_accepts_date_named_rework_alias_root(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-3aec4d07-33bd0fad-03-assembly-and-creator-integration"
            ),
            "expected_output": (
                "Refine mixed-edit assembly with usable creator spoken-sales segments, "
                "smoother transitions, and better timing between proof, guidance, and CTA."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    root = tmp_path / "outputs" / "adflow-assembly-creator-rework-20260525-002223"
    evidence_dir = root / "player-evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "phase1-player-evidence.json").write_text(
        '{"status": "review_ready", "timeline": "hook_body_proof_cta"}',
        encoding="utf-8",
    )
    (root / "reference-master-batch-rendered").mkdir(parents=True)
    (root / "reference-master-batch-rendered" / "batch_manifest.json").write_text(
        '{"rendered_count": 1, "ready_count": 1}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Assembly and creator integration completed with player evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-date-named-rework-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert any(
        "adflow-assembly-creator-rework-20260525-002223" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_accepts_adflow_extreme_attempt_alias_root(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-3aec4d07-33bd0fad-03-assembly-and-creator-integration"
            ),
            "expected_output": (
                "Refine mixed-edit assembly with usable creator spoken-sales segments, "
                "smoother transitions, and better timing between proof, guidance, and CTA."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    root = tmp_path / "outputs" / "adflow-extreme-attempt-20260525-003750-66809"
    evidence_dir = root / "player-evidence"
    reports_dir = root / "reports"
    package_root = root / "reference-master-batch-fresh" / "phase1-improved-demo-packages"
    evidence_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    package_root.mkdir(parents=True)
    (root / "final_execution_report.md").write_text(
        "Assembly and creator integration produced a rendered Phase 1 package.",
        encoding="utf-8",
    )
    (evidence_dir / "ffprobe.json").write_text(
        '{"width": 1080, "height": 1920, "duration": 20.0}',
        encoding="utf-8",
    )
    (reports_dir / "ai_video_stage_gate_audit.json").write_text(
        '{"status": "next", "phase1_acceptance_ready": true}',
        encoding="utf-8",
    )
    (package_root / "manifest.json").write_text(
        """
        {
          "packages": [
            {
              "demo_id": "phase1_real_01",
              "delivery_status": "rendered_review_ready_demo",
              "human_simulation_accepted": true,
              "phase1_comparison_accepted": true,
              "blocker_codes": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (package_root / "phase1_real_01").mkdir()

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Assembly and creator integration completed with adflow extreme attempt evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-adflow-extreme-attempt-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert any(
        "adflow-extreme-attempt-20260525-003750-66809" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_prefers_adflow_extreme_outputs_over_stale_outputs_root(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-3aec4d07-33bd0fad-03-assembly-and-creator-integration"
            ),
            "expected_output": (
                "Refine mixed-edit assembly with usable creator spoken-sales segments, "
                "smoother transitions, and better timing between proof, guidance, and CTA."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "outputs" / "adflow-extreme-attempt-20260525-003750-66809"
    stale_reports = stale_root / "reports"
    stale_reports.mkdir(parents=True)
    (stale_reports / "phase1-human-simulation-review.md").write_text(
        "Decision: rejected\nblocked_not_deliverable_demo",
        encoding="utf-8",
    )
    old_time = NOW.timestamp() - 3600
    os.utime(stale_root, (old_time, old_time))

    fresh_root = tmp_path / "adflow_extreme_outputs" / "attempt-20260525-005101-assembly-creator"
    fresh_reports = fresh_root / "reference-master-batch-fresh-after-refine"
    fresh_reports.mkdir(parents=True)
    (fresh_root / "assembly_creator_integration_execution_report.md").write_text(
        "Result: ready_count=1, rendered_count=1, human_simulation=accept_phase1_demo.",
        encoding="utf-8",
    )
    (fresh_root / "browser_player_gate_blocker.md").write_text(
        """
        Result: blocked by environment: Browser is not available: iab.
        Recovery/evidence used instead: ffprobe render metadata was refreshed;
        generated local player HTML remains for replay when the in-app browser is available.
        """,
        encoding="utf-8",
    )
    (fresh_reports / "batch_manifest.json").write_text(
        '{"rendered_count": 1, "ready_count": 1}',
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Assembly and creator integration completed with fresh adflow_extreme_outputs evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-adflow-extreme-outputs-root-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_browser_player_gate_blocked" not in gate.hard_gate_failures
    assert any(
        "adflow_extreme_outputs/attempt-20260525-005101-assembly-creator" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "adflow-extreme-attempt-20260525-003750-66809" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_accepts_k_output_stage1_transition_evidence(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
                "rework-3aec4d07-33bd0fad-06-stage1-transition-generation"
            ),
            "expected_output": (
                "Only after Phase 1 passes, generate transition clips that improve mixed-edit "
                "flow without changing ad meaning."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "adflow_extreme_outputs" / "attempt-20260525-005101-assembly-creator"
    stale_package = (
        stale_root / "reference-master-batch-fresh" / "ai-video-stage1-transition-package"
    )
    stale_package.mkdir(parents=True)
    (stale_package / "progress_report.md").write_text(
        "Old assembly output; do not use for current Stage 1 transition work.",
        encoding="utf-8",
    )
    old_time = NOW.timestamp() - 3600
    os.utime(stale_root, (old_time, old_time))

    fresh_root = tmp_path / "k_output" / "attempt-20260525-013624-stage1-transition-generation"
    package_root = fresh_root / "ai-video-stage1-transition-package"
    package_root.mkdir(parents=True)
    (package_root / "manifest.json").write_text(
        """
        {
          "schema_version": "rainflow_ai_video_stage1_transition_package_v1",
          "status": "mock_verified",
          "ready_provider_count": 0,
          "transition_evidence": {
            "status": "mock_verified",
            "mock_insertion_count": 1,
            "provider_insertion_count": 0
          },
          "next_action": "Provider-backed rerun is required before claiming real AI completion."
        }
        """,
        encoding="utf-8",
    )
    (package_root / "progress_report.md").write_text(
        "Status: `mock_verified`. Ready providers: 0. Next real blocker is provider credentials.",
        encoding="utf-8",
    )
    (fresh_root / "browser_player_gate_blocker.md").write_text(
        """
        Browser runtime returned `Browser is not available: iab`.
        Fallback evidence captured instead: ffprobe verifies the bridge MP4 and
        stage1-local-bridge-player.html is persisted for local-player replay.
        """,
        encoding="utf-8",
    )
    (fresh_root / "final_execution_summary.md").write_text(
        "Result: mock_verified. 31 passed, 1 skipped. Provider-backed AI generation was not claimed.",
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer=(
                "Stage 1 transition generation completed with k_output evidence. "
                "Result is mock_verified and provider-backed AI generation remains blocked."
            ),
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-k-output-stage1-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_browser_player_gate_blocked" not in gate.hard_gate_failures
    assert any(
        "k_output/attempt-20260525-013624-stage1-transition-generation" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "adflow_extreme_outputs/attempt-20260525-005101-assembly-creator" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_prefers_explicit_output_lock_over_stale_k_output(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    output_root = tmp_path / "outputs" / "kun-seedance-stage1-real-20260525"
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-msn-rainflow-seedance-stage1-real-v1-01-source-parity-and-port",
            "expected_output": (
                "Port Seedance 1.5 bridge parity and record provider blocker evidence."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
            "resource_locks": [f"output:{output_root}"],
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "k_output" / "attempt-20260525-013624-stage1-transition-generation"
    stale_root.mkdir(parents=True)
    (stale_root / "browser_player_gate_blocker.md").write_text(
        "Browser runtime returned blocked_by_environment and no fallback evidence.",
        encoding="utf-8",
    )

    current_root = output_root / "attempt-20260525T014440Z-direct"
    current_root.mkdir(parents=True)
    (current_root / "seedance_stage1_real_summary.json").write_text(
        """
        {
          "status": "blocked_provider_network_failure",
          "provider": "seedance",
          "ready_for_flow": false,
          "placeholder_fallback_allowed": false,
          "blocker": "DNS/name resolution failed before provider task creation"
        }
        """,
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer=(
                "Ported Seedance 1.5 bridge parity and recorded provider blocker evidence "
                "under the explicit output lock."
            ),
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-explicit-output-lock-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "pass"
    assert "local_browser_player_gate_blocked" not in gate.hard_gate_failures
    assert any(
        "outputs/kun-seedance-stage1-real-20260525" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "k_output/attempt-20260525-013624-stage1-transition-generation" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_blocks_real_provider_probe_when_output_missing(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    output_root = tmp_path / "outputs" / "kun-seedance-stage1-real-20260525"
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-msn-rainflow-seedance-stage1-real-v1-03-real-seedance-single-probe",
            "phase": "real_seedance_single_probe",
            "expected_output": (
                "Produce a real provider-backed Seedance probe MP4 with ffprobe evidence."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
            "resource_locks": [f"output:{output_root}"],
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    attempt_root = output_root / "attempt-20260525T023144Z-direct-real"
    package_dir = attempt_root / "ai-video-stage1-transition-package"
    package_dir.mkdir(parents=True)
    (attempt_root / "seedance_stage1_real_t2v.probe.json").write_text(
        """
        {
          "blocker_classification": "provider_network_failure",
          "bridge_result": {
            "status": "failed",
            "provider": "seedance",
            "ready_for_flow": false,
            "error": "[Errno 8] nodename nor servname provided, or not known"
          }
        }
        """,
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        """
        {
          "status": "planning_ready_provider_blocked",
          "transition_evidence": {
            "reason": "no_insertable_bridge_evidence",
            "ready_task_count": 0,
            "provider_output_count": 0,
            "provider_insertion_count": 0
          }
        }
        """,
        encoding="utf-8",
    )
    (attempt_root / "review_report.md").write_text(
        (
            "Provider status: `failed`\n"
            "Blocker classification: `provider_network_failure`\n"
            "Current result: `output_missing`.\n"
            "Provider output count: `0`\n"
        ),
        encoding="utf-8",
    )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Ran the real Seedance probe and recorded provider blocker evidence.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-real-provider-blocker-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "failed"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "fail"
    assert gate.failure_category == "environment_failure"
    assert "local_provider_network_blocked" in gate.hard_gate_failures
    assert "local_provider_output_not_materialized" in gate.hard_gate_failures
    assert "provider-backed video did not materialize" in (gate.root_cause or "")


def test_kun_runtime_runner_requires_local_evidence_for_rainflow_product_stage(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-01-mixed-edit-repair",
            "expected_output": "Produce RainFlow Phase 1 mixed-edit repair evidence.",
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Phase 1 mixed-edit repair completed.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-local-evidence-required-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "failed"
    gate = control_plane.gate_evaluations[f"gate-kun-runtime-{work.work_item_id}"]
    assert gate.north_star_verdict == "fail"
    assert "local_runtime_evidence_missing" in gate.hard_gate_failures
    assert "did not produce local RainFlow evidence files" in (gate.root_cause or "")


def test_kun_runtime_runner_rejects_stale_evidence_for_strategy_retest(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-kun-retest-after-work-kun-plan-change-work-qi-observation-"
                "msn-rainflow-seedance-stage1-real-v1"
            ),
            "type": "test",
            "expected_output": (
                "Run clean retest evidence for the stricter RainFlow continuation plan, "
                "including quality gate, product residual audit, and regression checks."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "k_output" / "attempt-20260525-104333-stage1-seedance-real-rework"
    stale_root.mkdir(parents=True)
    stale_file = stale_root / "ai_video_stage_gate_audit_after_patch.json"
    stale_file.write_text('{"status": "ready", "note": "old evidence from prior run"}')
    old_time = datetime.now(UTC).timestamp() - 3600
    os.utime(stale_file, (old_time, old_time))

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Clean RainFlow retest command completed; pytest passed.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-stale-strategy-retest-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "failed"
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "fail"
    assert "local_runtime_evidence_missing" in gate.hard_gate_failures
    assert not any(
        "attempt-20260525-104333-stage1-seedance-real-rework" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_accepts_fresh_evidence_for_strategy_retest(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-kun-retest-after-work-kun-plan-change-work-qi-observation-"
                "msn-rainflow-seedance-stage1-real-v1"
            ),
            "type": "test",
            "expected_output": (
                "Run clean retest evidence for the stricter RainFlow continuation plan, "
                "including quality gate, product residual audit, and regression checks."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    fresh_root = tmp_path / "k_output" / "attempt-20260525-112600-stage1-clean-retest"
    fresh_root.mkdir(parents=True)
    fresh_file = fresh_root / "ai_video_stage_gate_audit_after_patch.json"
    fresh_file.write_text('{"status": "ready", "pytest": "passed"}')
    fresh_time = datetime.now(UTC).timestamp() + 30
    os.utime(fresh_file, (fresh_time, fresh_time))

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Clean RainFlow retest command completed; pytest passed.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-fresh-strategy-retest-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert not gate.hard_gate_failures
    assert any(
        "attempt-20260525-112600-stage1-clean-retest" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_does_not_hide_fresh_outputs_behind_stale_k_output(
    tmp_path: Path,
) -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": (
                "work-kun-retest-after-work-kun-plan-change-work-qi-observation-"
                "msn-rainflow-seedance-stage1-real-v1"
            ),
            "type": "test",
            "expected_output": (
                "Run clean retest evidence for the stricter RainFlow Stage 1 Seedance plan, "
                "including quality gate, product residual audit, and regression checks."
            ),
            "workspace_ref": f"workspace://{tmp_path}",
        }
    )
    control_plane.work_items.pop("work-real-task")
    control_plane.work_items[work.work_item_id] = work

    stale_root = tmp_path / "k_output" / "attempt-20260525-104333-stage1-seedance-real-rework"
    stale_root.mkdir(parents=True)
    stale_file = stale_root / "ai_video_stage_gate_audit_after_patch.json"
    stale_file.write_text('{"status": "stale", "pytest": "old"}', encoding="utf-8")
    stale_time = datetime.now(UTC).timestamp() - 3600
    os.utime(stale_file, (stale_time, stale_time))

    fresh_root = tmp_path / "outputs" / "kun_retest_seedance_stage1_real_20260525_123834_97399"
    evidence_dir = fresh_root / "evidence"
    evidence_dir.mkdir(parents=True)
    fresh_file = evidence_dir / "ai_video_stage_gate_audit.json"
    fresh_file.write_text('{"status": "next", "pytest": "passed"}', encoding="utf-8")
    fresh_time = datetime.now(UTC).timestamp() + 30
    os.utime(fresh_file, (fresh_time, fresh_time))

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: KunTaskExecutionOutput(
            status="done",
            answer="Clean RainFlow retest command completed; pytest passed.",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-fresh-outputs-over-stale-k-output-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    gate = next(
        item
        for item in control_plane.gate_evaluations.values()
        if item.subject_ref == work.work_item_id
    )
    assert gate.north_star_verdict == "pass"
    assert "local_runtime_evidence_missing" not in gate.hard_gate_failures
    assert any(
        "outputs/kun_retest_seedance_stage1_real_20260525_123834_97399" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )
    assert not any(
        "k_output/attempt-20260525-104333-stage1-seedance-real-rework" in artifact.path_or_uri
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_classifies_local_orchestrator_connection_failure_as_environment() -> (
    None
):
    control_plane, _mission = _runtime()

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise OSError("Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 55432)")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=failing_executor)

    result = runner.run(control_plane.work_items["work-real-task"])

    assert result.status == "failed"
    assert result.failure_category == "environment_failure"
    assert "Connect call failed" in result.summary


def test_kun_runtime_runner_uses_direct_fallback_for_environment_blocker() -> None:
    control_plane, _mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "workspace_ref": "workspace:///tmp/rainflow-isolated",
            "sandbox_ref": "sandbox://msn-real-task/work-real-task",
            "resource_locks": ["workspace:/tmp/rainflow-isolated"],
        }
    )
    control_plane.work_items[work.work_item_id] = work

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise OSError("Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 55432)")

    def direct_fallback(
        prompt: str,
        fallback_work_item: WorkItem,
        _control_plane: InMemoryControlPlane,
        primary_failure_summary: str,
    ) -> KunTaskExecutionOutput:
        assert "primary KUN orchestrator was environment-blocked" in prompt
        assert "rainflow-isolated" in prompt
        assert "Create a new unique output directory" in prompt
        assert "rm -rf" in prompt
        assert fallback_work_item.work_item_id == work.work_item_id
        assert "Connect call failed" in primary_failure_summary
        return KunTaskExecutionOutput(
            status="done",
            answer=(
                "Produce a concrete audited result: direct fallback repaired the environment "
                "blocker path and recorded RainFlow execution evidence."
            ),
            raw={"runtime_fallback": "unit-direct-fallback"},
        )

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=failing_executor,
        fallback_executor=direct_fallback,
    )

    result = runner.run(work)

    assert result.status == "done"
    assert result.artifacts
    assert "direct_runtime_fallback" in result.artifacts[0].supports
    assert "orchestrator_environment_fallback" in result.artifacts[0].supports


def test_kun_runtime_runner_handles_strategy_optimization_without_external_executor() -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-plan-change-quality-gate",
            "type": "research",
            "expected_output": (
                "Create or revise a stricter continuation plan from the failed quality gate. "
                "Require clean retest evidence before delivery can close."
            ),
            "recovery_refs": ["gate-low-quality"],
        }
    )
    control_plane.work_items = {work.work_item_id: work}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("strategy optimization should not need the external executor")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=failing_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-strategy-optimization-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    assert any(
        "strategy_optimization_plan" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    gate = control_plane.gate_evaluations[
        "gate-kun-strategy-optimization-work-kun-plan-change-quality-gate"
    ]
    assert gate.next_action == "needs_plan_change"
    assert gate.next_state == "changing_plan"
    assert {
        "work-kun-implement-after-work-kun-plan-change-quality-gate",
        "work-kun-retest-after-work-kun-plan-change-quality-gate",
        "work-kun-merge-after-work-kun-plan-change-quality-gate",
    }.issubset(control_plane.work_items)


def test_strategy_optimization_routes_game_quality_gaps_to_game_runner() -> None:
    control_plane, mission = _runtime()
    mission = control_plane.missions[mission.mission_id]
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    control_plane.contracts[contract.contract_id] = contract.model_copy(
        update={
            "delivery_contract": {
                "project_path": "/tmp/wordforge-game",
                "production_mode": "scribble_adventure_functional_parity_v1",
                "benchmark_residual_required": True,
                "final_player_experience_required": True,
            }
        }
    )
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-plan-change-game-quality-gate",
            "type": "research",
            "expected_output": (
                "Create or revise a stricter continuation plan from the failed quality gate. "
                "Require clean retest evidence before delivery can close."
            ),
            "workspace_ref": "workspace:///tmp/wordforge-game",
            "sandbox_ref": "sandbox://msn-real-task/work-kun-plan-change-game-quality-gate",
            "resource_locks": ["workspace:/tmp/wordforge-game"],
            "recovery_refs": ["gate-low-quality"],
        }
    )
    control_plane.work_items = {work.work_item_id: work}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("game strategy routing should not use the generic executor")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=failing_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-game-strategy-optimization-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    followups = [
        item
        for item in control_plane.work_items.values()
        if item.work_item_id.startswith("work-game-rework-work-kun-plan-change-game-quality-gate")
    ]
    assert [item.phase for item in followups] == [
        "visual-product-iteration",
        "image-object-interaction-iteration",
        "sandbox-dynamics-iteration",
        "commercial-game-polish-iteration",
        "internal-test",
        "supervisor-gate",
        "benchmark-residual-audit",
        "final-delivery",
    ]
    assert followups[0].owner == "kun-game-production-runner"
    assert followups[5].owner == "external-supervisor-gpt5.5"
    assert all(item.workspace_ref == "workspace:///tmp/wordforge-game" for item in followups)
    assert all(item.sandbox_ref for item in followups)
    expected_workspace_lock = normalize_resource_lock_ref("workspace:/tmp/wordforge-game")
    assert all(expected_workspace_lock in item.resource_locks for item in followups)
    assert not any(item.owner == "kun" and item.type == "execution" for item in followups)


def test_strategy_optimization_does_not_route_rainflow_to_game_runner() -> None:
    control_plane, mission = _runtime()
    mission = control_plane.missions[mission.mission_id]
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    control_plane.contracts[contract.contract_id] = contract.model_copy(
        update={
            "delivery_contract": {
                "project_path": "/tmp/rainflow-ad-video",
                "production_mode": RAINFLOW_AD_PRODUCTION_MODE,
                "benchmark_residual_required": True,
                "final_player_experience_required": True,
            }
        }
    )
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-plan-change-rainflow-quality-gate",
            "type": "research",
            "expected_output": (
                "Create or revise a stricter continuation plan from the failed RainFlow "
                "quality gate. Require clean retest evidence before delivery can close."
            ),
            "workspace_ref": "workspace:///tmp/rainflow-ad-video",
            "sandbox_ref": "sandbox://msn-real-task/work-kun-plan-change-rainflow-quality-gate",
            "resource_locks": ["workspace:/tmp/rainflow-ad-video"],
            "recovery_refs": ["gate-rainflow-low-quality"],
        }
    )
    control_plane.work_items = {work.work_item_id: work}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("strategy optimization should not call the generic executor")
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-rainflow-strategy-optimization-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert not any(
        item.work_item_id.startswith("work-game-rework-")
        for item in control_plane.work_items.values()
    )
    assert {
        "work-kun-implement-after-work-kun-plan-change-rainflow-quality-gate",
        "work-kun-retest-after-work-kun-plan-change-rainflow-quality-gate",
        "work-kun-merge-after-work-kun-plan-change-rainflow-quality-gate",
    }.issubset(control_plane.work_items)


def test_game_strategy_followups_derive_workspace_boundary_from_contract() -> None:
    control_plane, mission = _runtime()
    mission = control_plane.missions[mission.mission_id]
    project_path = "/tmp/wordforge-game"
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    control_plane.contracts[contract.contract_id] = contract.model_copy(
        update={
            "delivery_contract": {
                "project_path": project_path,
                "production_mode": "scribble_adventure_functional_parity_v1",
                "benchmark_residual_required": True,
                "final_player_experience_required": True,
            }
        }
    )
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-plan-change-game-quality-gate-no-boundary",
            "type": "research",
            "expected_output": "Create a stricter game continuation plan.",
            "workspace_ref": None,
            "sandbox_ref": None,
            "resource_locks": [],
            "recovery_refs": ["gate-low-quality"],
        }
    )
    control_plane.work_items = {work.work_item_id: work}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    runner = KunRuntimeTaskRunner(
        control_plane=control_plane,
        executor=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("game strategy routing should not use the generic executor")
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-game-strategy-boundary-test",
    )

    daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    followups = [
        item
        for item in control_plane.work_items.values()
        if item.work_item_id.startswith(
            "work-game-rework-work-kun-plan-change-game-quality-gate-no-boundary"
        )
    ]
    assert followups
    expected_workspace_lock = normalize_resource_lock_ref(f"workspace:{project_path}")
    assert all(item.workspace_ref == f"workspace://{project_path}" for item in followups)
    assert all(expected_workspace_lock in item.resource_locks for item in followups)
    assert all(item.sandbox_ref for item in followups)


def test_kun_runtime_runner_does_not_recurse_strategy_followups() -> None:
    control_plane, mission = _runtime()
    followup = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-implement-after-work-kun-plan-change-quality-gate",
            "type": "execution",
            "expected_output": (
                "Implement the stricter continuation plan and record concrete changed artifacts."
            ),
            "dependencies": [],
        }
    )
    control_plane.work_items = {followup.work_item_id: followup}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    def executor(_prompt: str) -> KunTaskExecutionOutput:
        return KunTaskExecutionOutput(status="done", answer="implemented")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-strategy-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=3)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert all(
        not work_id.startswith("work-kun-implement-after-work-kun-implement-after")
        for work_id in control_plane.work_items
    )


def test_kun_runtime_runner_handles_control_plane_supervisor_recovery_without_external_executor() -> (
    None
):
    control_plane, mission = _runtime()
    recovery = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-supervisor-recovery",
            "type": "repair",
            "owner": "control-plane-supervisor",
            "expected_output": "Recover work-real-task after environment_failure; next action: needs_repair",
            "dependencies": [],
        }
    )
    control_plane.work_items = {recovery.work_item_id: recovery}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "repairing"})

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("supervisor recovery should not need the external executor")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=failing_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"control-plane-supervisor": runner},
        daemon_id="daemon-supervisor-recovery-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [recovery.work_item_id]
    assert control_plane.work_items[recovery.work_item_id].status == "done"
    assert any(
        "control_plane_supervisor_recovery" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    gate = control_plane.gate_evaluations["gate-control-plane-supervisor-work-supervisor-recovery"]
    assert gate.next_action == "continue"
    assert gate.next_state == "running"


def test_kun_runtime_merge_blocks_overlapping_artifact_outputs() -> None:
    control_plane, mission = _runtime()
    first = WorkItem(
        work_item_id="work-upstream-a",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
    )
    second = first.model_copy(update={"work_item_id": "work-upstream-b"})
    merge = WorkItem(
        work_item_id="work-merge-conflict",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="merge",
        owner="kun",
        dependencies=[first.work_item_id, second.work_item_id],
        expected_output="Merge upstream implementation artifacts.",
    )
    control_plane.work_items = {
        first.work_item_id: first,
        second.work_item_id: second,
        merge.work_item_id: merge,
    }
    for item in (first, second):
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{item.work_item_id}",
            kind="diff",
            path_or_uri="repo://src/app.ts",
            content_hash=f"hash-{item.work_item_id}",
            created_by=item.work_item_id,
            mission_id=mission.mission_id,
            work_item_id=item.work_item_id,
            supports=["writes:src/app.ts"],
            source_quality="primary",
        )
        control_plane.artifacts[artifact.artifact_id] = artifact
    runner = KunRuntimeTaskRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-merge-conflict-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [merge.work_item_id]
    gate = control_plane.gate_evaluations["gate-kun-merge-work-merge-conflict"]
    assert gate.north_star_verdict == "fail"
    assert "overlapping_artifact_output" in gate.hard_gate_failures
    assert control_plane.work_items[merge.work_item_id].status == "failed"


def test_kun_runtime_merge_allows_latest_retry_artifact_from_same_dependency() -> None:
    control_plane, mission = _runtime()
    upstream = WorkItem(
        work_item_id="work-upstream-retry",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
    )
    retest = WorkItem(
        work_item_id="work-upstream-retest",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="test",
        owner="kun",
        status="done",
    )
    merge = WorkItem(
        work_item_id="work-merge-retry-aware",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="merge",
        owner="kun",
        dependencies=[upstream.work_item_id, retest.work_item_id],
        expected_output="Merge latest retry-safe dependency artifacts.",
    )
    control_plane.work_items = {
        upstream.work_item_id: upstream,
        retest.work_item_id: retest,
        merge.work_item_id: merge,
    }
    control_plane.artifacts["artifact-upstream-old"] = ArtifactRecord(
        artifact_id="artifact-upstream-old",
        kind="report",
        path_or_uri="control-plane://kun-runtime/msn-real-task/work-upstream-retry",
        content_hash="hash-old",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id=upstream.work_item_id,
        supports=["old_attempt"],
        source_quality="primary",
    )
    control_plane.artifacts["artifact-upstream-new"] = ArtifactRecord(
        artifact_id="artifact-upstream-new",
        kind="report",
        path_or_uri="control-plane://kun-runtime/msn-real-task/work-upstream-retry",
        content_hash="hash-new",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id=upstream.work_item_id,
        supports=["latest_attempt"],
        source_quality="primary",
    )
    control_plane.artifacts["artifact-retest"] = ArtifactRecord(
        artifact_id="artifact-retest",
        kind="report",
        path_or_uri="control-plane://kun-runtime/msn-real-task/work-upstream-retest",
        content_hash="hash-retest",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id=retest.work_item_id,
        supports=["clean_retest"],
        source_quality="primary",
    )
    runner = KunRuntimeTaskRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-merge-retry-aware-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [merge.work_item_id]
    gate = control_plane.gate_evaluations["gate-kun-merge-work-merge-retry-aware"]
    assert gate.north_star_verdict == "pass"
    assert gate.hard_gate_failures == []
    assert control_plane.work_items[merge.work_item_id].status == "done"
