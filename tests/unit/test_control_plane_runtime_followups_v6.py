from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kun.control_plane import (
    ArtifactManifest,
    ArtifactRecord,
    CollaborationTicket,
    ControlPlaneDaemon,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    NuoRuntimeRepairRunner,
    QiRuntimeGovernanceRunner,
    TaskPlan,
    WorkItem,
)
from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.runtime_observation import build_runtime_observation_report

NOW = datetime(2026, 5, 20, 9, 30, tzinfo=UTC)


def test_qi_runtime_governance_runner_records_replay_candidate_not_default_runtime() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-qi-runtime-learning-work-main",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="governance",
        owner="qi",
        expected_output="Review this runtime learning signal as a capability candidate.",
        recovery_refs=["gate-main", "work-main"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.north_star_verdict == "pass"
    assert control_plane.capability_profiles
    profile = next(iter(control_plane.capability_profiles.values()))
    assert profile.promotion_stage == "replay"
    assert profile.runtime_enabled is False


def test_qi_runtime_governance_runner_handles_nuo_review_collection_research() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-nuo-work-main-collect_reviews",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="research",
        owner="qi",
        expected_output="Collect the missing peer reviews before any delivery or capability conclusion.",
        recovery_refs=["gate-main", "work-main"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.north_star_verdict == "pass"
    assert result.artifacts[0].supports[0] == "qi_runtime_governance_report"


def test_qi_runtime_governance_runner_keeps_neutral_signal_without_profile() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-qi-runtime-note-work-main",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="governance",
        owner="qi",
        expected_output="Record this runtime note for audit only.",
        recovery_refs=["work-main"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "qi_runtime_governance_keep"
    assert control_plane.capability_profiles == {}


def test_qi_runtime_governance_runner_opens_plan_change_for_product_gap_signal() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-qi-product-gap",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="governance",
        owner="qi",
        expected_output=(
            "Open a stricter continuation branch, find a better path, and turn product gaps "
            "into new acceptance criteria before final closure."
        ),
        recovery_refs=["work-main"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "qi_runtime_governance_plan_change"
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert result.gate_evaluation.next_state == "changing_plan"
    assert [item.owner for item in result.followup_work_items] == ["qi", "kun"]
    assert (
        result.followup_work_items[0].work_item_id == "work-qi-strategy-replay-work-qi-product-gap"
    )
    assert result.followup_work_items[0].dependencies == [work_item.work_item_id]
    assert "isolated Qi strategy replay" in result.followup_work_items[0].expected_output
    assert result.followup_work_items[1].work_item_id == "work-kun-plan-change-work-qi-product-gap"
    assert control_plane.capability_profiles == {}


def test_qi_strategy_replay_creates_replay_candidate_not_runtime_default() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-qi-strategy-replay-work-product-gap",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="research",
        owner="qi",
        expected_output=(
            "Run an isolated strategy replay and process audit for the same task slice; "
            "compare the old path with a stricter alternative."
        ),
        recovery_refs=["work-product-gap", "gate-product-gap"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.task_type == "self_improvement"
    assert result.gate_evaluation.governance_signal == "qi_runtime_governance_strategy_replay"
    assert "qi_strategy_replay_report" in result.artifacts[0].supports
    assert "self_improvement_governance_only" in result.artifacts[0].supports
    assert "qi_shadow_rerun" in result.artifacts[0].supports
    assert result.followup_work_items == []
    profile = next(iter(control_plane.capability_profiles.values()))
    assert profile.promotion_stage == "replay"
    assert profile.runtime_enabled is False
    assert profile.governance_key.startswith("strategy-replay-learning:")


def test_qi_runtime_governance_runner_records_merge_without_new_runtime_candidate() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-qi-capability-duplicates",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="governance",
        owner="qi",
        expected_output=(
            "Merge and dedupe duplicate production capability profiles; keep only the strongest "
            "runtime default and preserve duplicate source refs as evidence."
        ),
        recovery_refs=["cap-duplicate-a", "cap-duplicate-b"],
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "qi_runtime_governance_merge"
    assert control_plane.capability_profiles == {}


def test_nuo_runtime_repair_runner_classifies_system_condition() -> None:
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id="work-nuo-preflight-work-main",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        expected_output="Classify timeout and network EOF preflight failures.",
        skill_refs=["shell-exec"],
        recovery_refs=["artifact-preflight-failed"],
    )

    result = runner.run(work_item)

    assert result.status == "partial"
    assert result.failure_category is None
    assert result.artifacts[0].supports[0] == "nuo_runtime_repair_report"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.north_star_verdict == "partial"
    assert result.gate_evaluation.next_action == "needs_repair"
    assert "nuo_repair_requires_clean_retest" in result.gate_evaluation.hard_gate_failures
    assert result.gate_evaluation.score_breakdown["runtime_followup_executed"] == 1.0


def test_nuo_quality_observation_does_not_trigger_permission_clean_retest(tmp_path) -> None:
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id="work-nuo-observation-quality-gate",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        workspace_ref=str(tmp_path),
        resource_locks=[f"workspace:{tmp_path}"],
        expected_output=(
            "Classify this runtime observation before agent scoring. Qi should arrange "
            "a retest after it chooses a better strategy."
        ),
    )

    result = runner.run(work_item)

    assert result.status == "partial"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_repair"
    assert "nuo_repair_requires_clean_retest" in result.gate_evaluation.hard_gate_failures
    assert "nuo_clean_retest_failed" not in result.gate_evaluation.hard_gate_failures


def test_nuo_clean_retest_passes_and_closes_stale_permission_ticket(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-clean-retest",
        owner="product-owner",
        objective="Continue the blocked product mission after a stale permission blocker.",
        task_type="product_development",
        status="waiting_human",
        current_plan_version="v1",
    )
    work_item = WorkItem(
        work_item_id="work-nuo-clean-retest-write-boundary",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        priority=90,
        workspace_ref=str(tmp_path),
        resource_locks=[f"workspace:{tmp_path}"],
        recovery_refs=["ticket-clean-retest-write-permission"],
        expected_output=(
            "Run clean retest for project workspace write permission. If writable, "
            "auto-unblock the stale human ticket and continue queued work."
        ),
    )
    ticket = CollaborationTicket(
        ticket_id="ticket-clean-retest-write-permission",
        mission_id=mission.mission_id,
        type="operator_action",
        role_needed="operator",
        why_needed="Control Plane believed the project workspace was not writable.",
        context_ref="ctx-clean-retest",
        risk_if_skipped="KUN may remain waiting for a false permission blocker.",
        deadline=NOW + timedelta(hours=1),
        output_contract="Confirm project write permission or let Nuo clean retest resolve it.",
        status="open",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[work_item.work_item_id] = work_item
    control_plane.collaboration_tickets[ticket.ticket_id] = ticket
    runner = NuoRuntimeRepairRunner(control_plane=control_plane)

    run = control_plane.run_work_item(work_item_id=work_item.work_item_id, runner=runner)

    assert run is not None
    assert run.exit_status == "succeeded"
    assert control_plane.work_items[work_item.work_item_id].status == "done"
    assert control_plane.missions[mission.mission_id].status == "running"
    assert control_plane.collaboration_tickets[ticket.ticket_id].status == "closed"
    gate = control_plane.gate_evaluations[str(run.gate_evaluation_ref)]
    assert gate.governance_signal == "nuo_clean_retest_passed"
    assert gate.next_action == "continue"
    artifact = next(iter(control_plane.artifacts.values()))
    assert "nuo_clean_retest_passed" in artifact.supports


def test_nuo_clean_retest_keeps_real_permission_blocker_waiting_human(tmp_path) -> None:
    missing_workspace = tmp_path / "missing"
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id="work-nuo-clean-retest-missing-workspace",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        workspace_ref=str(missing_workspace),
        expected_output="Run clean retest for workspace write permission.",
    )

    result = runner.run(work_item)

    assert result.status == "waiting_human"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_human"
    assert result.gate_evaluation.failure_category == "permission_failure"
    assert "nuo_clean_retest_failed" in result.gate_evaluation.hard_gate_failures


def test_runtime_observation_ignores_superseded_quality_gates() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-observation",
        owner="product-owner",
        objective="Deliver a long-running product task",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="v2",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.gate_evaluations["gate-v1-fail"] = GateEvaluation(
        gate_evaluation_id="gate-v1-fail",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-old",
        stage="delivery",
        task_type="product_development",
        rubric_version="rubric-v1",
        metric_pack_version="metrics-v1",
        north_star_verdict="fail",
        result_quality=0.2,
        speed=0.8,
        cost=0.8,
        risk=0.5,
        evidence_quality=0.3,
        collaboration_quality=0.7,
        hard_gate_failures=["stale_browser_evidence"],
        next_action="needs_plan_change",
        next_state="changing_plan",
        created_by="test",
    )
    control_plane.gate_evaluations["gate-v2-pass"] = GateEvaluation(
        gate_evaluation_id="gate-v2-pass",
        mission_id=mission.mission_id,
        task_plan_version="v2",
        subject_ref="work-current",
        stage="delivery",
        task_type="product_development",
        rubric_version="rubric-v1",
        metric_pack_version="metrics-v1",
        north_star_verdict="pass",
        result_quality=0.98,
        speed=0.8,
        cost=0.8,
        risk=0.1,
        evidence_quality=0.98,
        collaboration_quality=0.9,
        next_action="continue",
        next_state="running",
        created_by="test",
    )

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-test", built_at=NOW),
    )

    assert "quality_gate_not_passed" not in [item.code for item in report.items]


def test_runtime_observation_treats_partial_qi_nuo_recovery_as_incomplete() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-partial-recovery",
        owner="product-owner",
        objective="Do not close failed work from diagnosis-only followups.",
        task_type="product_development",
        status="repairing",
        current_plan_version="v1",
    )
    failed = WorkItem(
        work_item_id="work-failed",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="failed",
        expected_output="failed product work",
    )
    partial_nuo = WorkItem(
        work_item_id="work-nuo-diagnosis",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        recovery_refs=[failed.work_item_id],
        expected_output="diagnosed only; clean retest still required",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed.work_item_id] = failed
    control_plane.work_items[partial_nuo.work_item_id] = partial_nuo

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    items = {item.code: item for item in report.items}
    assert "failed_work_recovery_incomplete" in items
    assert items["failed_work_recovery_incomplete"].evidence_refs == [failed.work_item_id]
    assert "failed_work_without_recovery" not in items


def test_runtime_observation_flags_required_capability_without_behavior_receipt() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-capability-receipt",
        owner="product-owner",
        objective="Do not treat metadata-only capability activation as real execution.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    work_item = WorkItem(
        work_item_id="work-required-capability",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
        required_capability_refs=["cap-production-required"],
        expected_output="Execute with a production capability.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[work_item.work_item_id] = work_item

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    items = {item.code: item for item in report.items}
    assert "capability_consumption_unproven" in items
    assert items["capability_consumption_unproven"].evidence_refs == [work_item.work_item_id]

    control_plane.artifacts["artifact-capability-behavior"] = ArtifactRecord(
        artifact_id="artifact-capability-behavior",
        kind="evidence",
        path_or_uri="mem://capability-behavior",
        content_hash="hash-capability-behavior",
        created_by="kun",
        mission_id=mission.mission_id,
        work_item_id=work_item.work_item_id,
        supports=["capability_behavior_receipt"],
    )

    clean_report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "capability_consumption_unproven" not in [item.code for item in clean_report.items]


def test_runtime_observation_flags_mechanical_acceptance_rework_loop() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rework-loop",
        owner="product-owner",
        objective="Continue a product until the user truly accepts it.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="v1-acceptance-rework-2",
    )
    control_plane.missions[mission.mission_id] = mission
    for index in range(2):
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-pressure-{index}",
            mission_id=mission.mission_id,
            task_plan_version=f"v1-acceptance-rework-{index}",
            subject_ref=f"ticket-acceptance-{index}",
            stage="delivery",
            task_type="product_development",
            rubric_version="rubric-v1",
            metric_pack_version="metrics-v1",
            north_star_verdict="partial",
            result_quality=0.74,
            speed=0.8,
            cost=0.8,
            risk=0.45,
            evidence_quality=0.7,
            collaboration_quality=0.8,
            thresholds={"result_quality": 0.8},
            hard_gate_failures=["human_acceptance_missing"],
            next_action="needs_plan_change",
            next_state="changing_plan",
            governance_signal="open_acceptance_requires_continued_product_pressure",
            created_by="control-plane-daemon",
        )
        control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    items = {item.code: item for item in report.items}
    assert "mechanical_acceptance_rework_loop" in items
    assert items["mechanical_acceptance_rework_loop"].routes == [
        "qi",
        "nuo",
        "external_supervisor",
    ]


def test_observation_followup_without_runner_opens_operator_ticket() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-observation-no-runner",
        owner="product-owner",
        objective="Quality gate failure must not create silent unrunnable followups.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-quality-failed",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-main",
        stage="workitem",
        task_type="product_development",
        rubric_version="rubric-v1",
        metric_pack_version="metrics-v1",
        north_star_verdict="partial",
        result_quality=0.4,
        speed=0.8,
        cost=0.8,
        risk=0.5,
        evidence_quality=0.5,
        collaboration_quality=0.6,
        hard_gate_failures=["product_gap"],
        failure_category="delivery_failure",
        responsibility_scope="unknown",
        next_action="needs_repair",
        next_state="repairing",
        created_by="external-supervisor",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-no-runner-test")

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )

    assert report.created_work_item_ids == []
    assert len(report.created_collaboration_ticket_ids) >= 1
    assert control_plane.missions[mission.mission_id].status == "blocked"
    tickets = [
        control_plane.collaboration_tickets[ticket_id]
        for ticket_id in report.created_collaboration_ticket_ids
    ]
    assert all(ticket.type == "operator_action" for ticket in tickets)
    assert any("no runner is registered" in ticket.why_needed for ticket in tickets)


def test_daemon_opens_info_gap_ticket_before_execution() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-info-gap",
        owner="product-owner",
        objective="Build a long-running product task",
        task_type="product_development",
        status="planning",
    )
    plan = TaskPlan(
        plan_id="plan-info-gap",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        info_gaps=["Need target platform and acceptance criteria."],
        acceptance_criteria=["do not execute until information is complete"],
        constraints=["ask before irreversible work"],
        approval_status="draft",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-info-gap-test")

    report = daemon.tick_once(
        now=NOW,
        max_work_items=1,
        write_progress=False,
    )

    assert report.created_collaboration_ticket_ids == ["collab-info-gap-msn-info-gap-v1"]
    ticket = control_plane.collaboration_tickets["collab-info-gap-msn-info-gap-v1"]
    assert ticket.status == "open"
    assert ticket.type == "expert_input"
    assert control_plane.missions[mission.mission_id].status == "waiting_human"


def test_daemon_opens_acceptance_ticket_and_retires_superseded_work() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-product-delivery",
        owner="product-owner",
        objective="Deliver a user-facing product.",
        task_type="product_development",
        status="delivering",
        current_plan_version="v2",
        artifact_manifest_refs=["manifest-v2-delivery"],
    )
    plan = TaskPlan(
        plan_id="plan-v2",
        mission_id=mission.mission_id,
        version="v2",
        objective=mission.objective,
        acceptance_criteria=["human review must be requested after delivery"],
        approval_status="approved_with_limits",
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-v2-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-game"],
        primary_artifact_ref="artifact-game",
        evidence_refs=["artifact-gate"],
        rollback_refs=["artifact-rollback"],
        created_by="kun",
        content_hash="hash",
        supports_delivery=True,
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.work_items["work-old-blocked"] = WorkItem(
        work_item_id="work-old-blocked",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="test",
        owner="kun",
        status="blocked",
        expected_output="obsolete blocked work",
    )
    control_plane.work_items["work-current-done"] = WorkItem(
        work_item_id="work-current-done",
        mission_id=mission.mission_id,
        task_plan_version="v2",
        type="execution",
        owner="kun",
        status="done",
        expected_output="current delivery work",
    )
    control_plane.collaboration_tickets["collab-info-gap-msn-product-delivery-v1"] = (
        CollaborationTicket(
            ticket_id="collab-info-gap-msn-product-delivery-v1",
            mission_id=mission.mission_id,
            type="expert_input",
            role_needed="product-owner",
            why_needed="obsolete v1 clarification",
            context_ref="ctx-v1",
            risk_if_skipped="obsolete task context could keep the cockpit noisy",
            deadline=NOW,
            output_contract="obsolete",
        )
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="daemon-acceptance-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )

    assert len(report.created_collaboration_ticket_ids) == 1
    ticket_id = report.created_collaboration_ticket_ids[0]
    assert ticket_id.startswith("collab-acceptance-msn-product-delivery-manifest-v2-delivery-")
    ticket = control_plane.collaboration_tickets[ticket_id]
    assert ticket.type == "review"
    assert ticket.context_ref == manifest.manifest_id
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert report.retired_work_item_ids == ["work-old-blocked"]
    assert control_plane.work_items["work-old-blocked"].status == "cancelled"
    assert report.retired_collaboration_ticket_ids == ["collab-info-gap-msn-product-delivery-v1"]
    assert (
        control_plane.collaboration_tickets["collab-info-gap-msn-product-delivery-v1"].status
        == "cancelled"
    )
    assert any(
        "superseded_work_cleanup" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    assert any(
        "superseded_collaboration_cleanup" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
