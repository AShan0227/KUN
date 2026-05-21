from __future__ import annotations

from datetime import UTC, datetime

from kun.control_plane import (
    ArtifactManifest,
    CollaborationTicket,
    ControlPlaneDaemon,
    InMemoryControlPlane,
    Mission,
    NuoRuntimeRepairRunner,
    QiRuntimeGovernanceRunner,
    TaskPlan,
    WorkItem,
)

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
    assert [item.owner for item in result.followup_work_items] == ["kun"]
    assert result.followup_work_items[0].work_item_id == "work-kun-plan-change-work-qi-product-gap"
    assert control_plane.capability_profiles == {}


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

    assert result.status == "done"
    assert result.failure_category is None
    assert result.artifacts[0].supports[0] == "nuo_runtime_repair_report"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.score_breakdown["runtime_followup_executed"] == 1.0


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

    assert report.created_collaboration_ticket_ids == [
        "collab-acceptance-msn-product-delivery-manifest-v2-delivery"
    ]
    ticket = control_plane.collaboration_tickets[
        "collab-acceptance-msn-product-delivery-manifest-v2-delivery"
    ]
    assert ticket.type == "review"
    assert ticket.context_ref == manifest.manifest_id
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert report.retired_work_item_ids == ["work-old-blocked"]
    assert control_plane.work_items["work-old-blocked"].status == "cancelled"
    assert report.retired_collaboration_ticket_ids == [
        "collab-info-gap-msn-product-delivery-v1"
    ]
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
