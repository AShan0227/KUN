from __future__ import annotations

from kun.control_plane import (
    ArtifactRecord,
    CapabilityProfile,
    ControlPlaneDaemon,
    InMemoryControlPlane,
    Mission,
    NuoSelfImprovementAuditRunner,
    QiSelfImprovementStrategyRunner,
    WorkItem,
    build_qi_self_improvement_strategy_search,
    build_self_improvement_audit_report,
)


def _mission(
    *,
    mission_id: str = "msn-self-audit",
    task_type: str = "product_development",
    status: str = "running",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        owner="user",
        objective="Exercise KUN self-improvement governance.",
        task_type=task_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        current_plan_version="v1",
    )


def test_nuo_self_improvement_audit_finds_unconsumed_capability_and_routes_qi() -> None:
    control_plane = InMemoryControlPlane()
    mission = _mission()
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items["work-main"] = WorkItem(
        work_item_id="work-main",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        required_capability_refs=["cap-strategy"],
        expected_output="Use the strategy capability during execution.",
        status="done",
    )
    audit_work = WorkItem(
        work_item_id="work-nuo-self-audit",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="nuo",
        idempotency_key="nuo-self-improvement-audit:msn-self-audit:test",
        expected_output="Run a self improvement audit.",
    )

    result = NuoSelfImprovementAuditRunner(control_plane=control_plane).run(audit_work)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert result.gate_evaluation.score_breakdown["gap_count"] == 1.0
    assert result.gate_evaluation.hard_gate_failures == []
    assert "nuo_self_improvement_audit" in result.artifacts[0].supports
    assert [item.owner for item in result.followup_work_items] == ["qi"]
    assert result.followup_work_items[0].idempotency_key.startswith("qi-self-improvement-strategy:")


def test_qi_self_improvement_strategy_generates_candidates_and_replay_profile() -> None:
    control_plane = InMemoryControlPlane()
    mission = _mission()
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items["work-main"] = WorkItem(
        work_item_id="work-main",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        required_capability_refs=["cap-strategy"],
        status="done",
    )
    audit = build_self_improvement_audit_report(
        control_plane,
        mission_id=mission.mission_id,
        task_plan_version="v1",
    )
    strategy = build_qi_self_improvement_strategy_search(audit)

    assert len(strategy.candidates) >= 3
    assert strategy.selected_candidate_refs
    assert any(candidate.selected for candidate in strategy.candidates)

    strategy_work = WorkItem(
        work_item_id="work-qi-self-strategy",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        idempotency_key=f"qi-self-improvement-strategy:{audit.audit_id}",
        expected_output="Run Qi multi-strategy search for this self improvement strategy.",
        recovery_refs=[audit.audit_id],
    )
    result = QiSelfImprovementStrategyRunner(control_plane=control_plane).run(strategy_work)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "continue"
    assert "qi_multi_strategy_candidates" in result.artifacts[0].supports
    assert [item.owner for item in result.followup_work_items] == ["kun"]
    profile = next(iter(control_plane.capability_profiles.values()))
    assert profile.promotion_stage == "replay"
    assert profile.runtime_enabled is False


def test_audit_marks_runtime_enabled_profile_without_promotion_proof_as_safety_gap() -> None:
    control_plane = InMemoryControlPlane()
    mission = _mission(task_type="self_improvement")
    control_plane.missions[mission.mission_id] = mission
    control_plane.capability_profiles["cap-unsafe"] = CapabilityProfile.model_construct(
        capability_id="cap-unsafe",
        capability_name="Unsafe default",
        governance_key="unsafe-default",
        source_refs=["task"],
        source_versions=["v1"],
        supersedes_refs=[],
        evidence_refs=[],
        known_limits=[],
        promotion_stage="replay",
        holdout_refs=[],
        regression_refs=[],
        last_verified_at=None,
        rollback_plan=[],
        runtime_enabled=True,
        rolled_back_at=None,
        rollback_reason="",
        rollback_refs=[],
    )

    audit = build_self_improvement_audit_report(
        control_plane,
        mission_id=mission.mission_id,
        task_plan_version="v1",
    )

    assert audit.status == "blocked"
    assert [gap.category for gap in audit.gaps] == ["safety_gap"]
    assert audit.gaps[0].severity == "critical"


def test_daemon_queues_nuo_self_improvement_audit_when_runner_can_execute() -> None:
    control_plane = InMemoryControlPlane()
    mission = _mission()
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items["work-main"] = WorkItem(
        work_item_id="work-main",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        required_capability_refs=["cap-strategy"],
        status="done",
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "nuo": NuoSelfImprovementAuditRunner(control_plane=control_plane),
        },
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], max_work_items=0)

    audit_ids = [
        item_id
        for item_id in report.created_work_item_ids
        if item_id.startswith("work-nuo-self-audit-")
    ]
    assert audit_ids
    audit_item = control_plane.work_items[audit_ids[0]]
    assert audit_item.owner == "nuo"
    assert audit_item.status == "queued"


def test_audit_does_not_flag_capability_when_behavior_receipt_exists() -> None:
    control_plane = InMemoryControlPlane()
    mission = _mission()
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items["work-main"] = WorkItem(
        work_item_id="work-main",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        required_capability_refs=["cap-strategy"],
        status="done",
    )
    control_plane.artifacts["artifact-receipt"] = ArtifactRecord(
        artifact_id="artifact-receipt",
        kind="report",
        path_or_uri="control-plane://receipt/work-main",
        content_hash="receipt",
        created_by="kun",
        mission_id=mission.mission_id,
        work_item_id="work-main",
        supports=["capability_behavior_receipt"],
    )

    audit = build_self_improvement_audit_report(
        control_plane,
        mission_id=mission.mission_id,
        task_plan_version="v1",
    )

    assert [gap.category for gap in audit.gaps] == []
