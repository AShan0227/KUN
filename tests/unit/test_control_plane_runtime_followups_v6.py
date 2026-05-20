from __future__ import annotations

from datetime import UTC, datetime

from kun.control_plane import (
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
