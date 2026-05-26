from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kun.control_plane import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CollaborationTicket,
    ControlPlaneDaemon,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    NuoRuntimeRepairRunner,
    QiRuntimeGovernanceRunner,
    RunRecord,
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
    assert result.followup_work_items[1].dependencies == [
        work_item.work_item_id,
        result.followup_work_items[0].work_item_id,
    ]
    assert control_plane.capability_profiles == {}


def test_qi_plan_change_followup_ids_do_not_collide_after_long_observation_prefix() -> None:
    control_plane = InMemoryControlPlane()
    runner = QiRuntimeGovernanceRunner(control_plane=control_plane)

    def run_for(suffix: str) -> list[str]:
        work_item = WorkItem(
            work_item_id=(
                "work-qi-observation-msn-rainflow-seedance-stage1-real-v1-"
                f"failed_work_recovery_incomplete-recovery_v1-{suffix}"
            ),
            mission_id="msn-runtime-followup",
            task_plan_version="v1",
            type="governance",
            owner="qi",
            expected_output=(
                "Continue iteration, find a better path, and create stricter acceptance "
                "criteria before this failed work item can close."
            ),
        )
        result = runner.run(work_item)
        return [item.work_item_id for item in result.followup_work_items]

    first_ids = run_for("f3ee0bdd29aa")
    second_ids = run_for("2acf8ac1e7e9")

    assert first_ids[0].startswith("work-qi-strategy-replay-")
    assert first_ids[1].startswith("work-kun-plan-change-")
    assert set(first_ids).isdisjoint(second_ids)


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


def test_nuo_runtime_repair_runner_handles_control_plane_nuo_recovery() -> None:
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id="work-nuo-work-main-fix-wrapper",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        idempotency_key="nuo-recovery:work-main:fix_wrapper:sandbox_permission_blocked",
        expected_output="Repair wrapper availability or contract compatibility.",
    )

    result = runner.run(work_item)

    assert result.status == "partial"
    assert result.gate_evaluation is not None
    assert "nuo_repair_requires_clean_retest" in result.gate_evaluation.hard_gate_failures


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


def test_nuo_runtime_repair_runner_preserves_changing_plan_state_when_qi_already_escalated() -> (
    None
):
    control_plane = InMemoryControlPlane()
    control_plane.missions["msn-runtime-followup"] = Mission(
        mission_id="msn-runtime-followup",
        owner="product-owner",
        objective="Keep the stricter plan-change path once Qi escalates product gaps.",
        task_type="product_development",
        status="changing_plan",
        current_plan_version="v1",
    )
    runner = NuoRuntimeRepairRunner(control_plane=control_plane)
    work_item = WorkItem(
        work_item_id="work-nuo-post-qi-plan-change",
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
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert result.gate_evaluation.next_state == "changing_plan"


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


def test_nuo_clean_retest_requeues_blocked_subject(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-clean-retest-blocked",
        owner="product-owner",
        objective="Resume blocked implementation work after a clean workspace retest.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    blocked = WorkItem(
        work_item_id="work-blocked-implementation",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="blocked",
        expected_output="Patch the project workspace after a stale tool failure.",
    )
    retest = WorkItem(
        work_item_id="work-nuo-clean-retest-blocked-implementation",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        priority=90,
        workspace_ref=str(tmp_path),
        resource_locks=[f"workspace:{tmp_path}"],
        recovery_refs=[blocked.work_item_id],
        expected_output="Run clean retest for project workspace write permission.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[blocked.work_item_id] = blocked
    control_plane.work_items[retest.work_item_id] = retest
    runner = NuoRuntimeRepairRunner(control_plane=control_plane)

    run = control_plane.run_work_item(work_item_id=retest.work_item_id, runner=runner)

    assert run is not None
    assert run.exit_status == "succeeded"
    assert control_plane.work_items[retest.work_item_id].status == "done"
    assert control_plane.work_items[blocked.work_item_id].status == "queued"
    assert any(
        ref.startswith("artifact-nuo-runtime-repair-runner-work-nuo-clean-retest")
        for ref in control_plane.work_items[blocked.work_item_id].recovery_refs
    )


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
    assert len(result.collaboration_tickets) == 1
    ticket = result.collaboration_tickets[0]
    assert ticket.type == "operator_action"
    assert ticket.role_needed == "operator_with_workspace_write_access"
    assert ticket.context_ref == work_item.work_item_id
    assert ticket.auto_resolvable_by == [work_item.work_item_id]
    assert result.artifacts[0].artifact_id in ticket.resolution_refs
    assert ticket.fallback_policy["probe_failure_reason"] == "workspace_missing_or_not_directory"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_human"
    assert result.gate_evaluation.failure_category == "permission_failure"
    assert "nuo_clean_retest_failed" in result.gate_evaluation.hard_gate_failures


def test_nuo_clean_retest_checks_existing_build_state_files(tmp_path) -> None:
    build_state = tmp_path / "tsconfig.tsbuildinfo"
    build_state.write_text("stale build info", encoding="utf-8")
    build_state.chmod(0o444)
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id="work-nuo-clean-retest-build-state",
        mission_id="msn-runtime-followup",
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        workspace_ref=str(tmp_path),
        expected_output="Run clean retest for workspace write permission.",
    )

    try:
        result = runner.run(work_item)
    finally:
        build_state.chmod(0o644)

    assert result.status == "waiting_human"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.failure_category == "permission_failure"
    assert "nuo_clean_retest_failed" in result.gate_evaluation.hard_gate_failures
    assert result.artifacts[0].supports[0] == "nuo_clean_retest_failed"


def test_nuo_mechanical_loop_retest_does_not_open_permission_ticket(tmp_path) -> None:
    missing_workspace = tmp_path / "missing"
    runner = NuoRuntimeRepairRunner(control_plane=InMemoryControlPlane())
    work_item = WorkItem(
        work_item_id=(
            "work-nuo-clean-retest-work-nuo-observation-msn-wordforge-"
            "mechanical_acceptance_rework_loop"
        ),
        mission_id="msn-wordforge-loop",
        task_plan_version="wordforge-v36-acceptance-rework-11111111",
        type="retest",
        owner="nuo",
        workspace_ref=str(missing_workspace),
        resource_locks=[f"workspace:{missing_workspace}"],
        recovery_refs=["work-nuo-observation-msn-wordforge-loop-mechanical_acceptance_rework_loop"],
        expected_output=(
            "Run a Nuo clean retest for the mechanical_acceptance_rework_loop diagnosis "
            "and require Qi strategy replay before another delivery attempt."
        ),
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.collaboration_tickets == []
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "nuo_loop_clean_retest_passed"
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert result.gate_evaluation.next_state == "changing_plan"
    assert result.gate_evaluation.failure_category is None
    assert "mechanical_acceptance_rework_loop_retest" in result.artifacts[0].supports


def test_nuo_clean_retest_does_not_rerun_failed_player_experience_gate(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-wordforge-product-gap",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="repairing",
        current_plan_version="wordforge-v58",
    )
    failed_gate_item = WorkItem(
        work_item_id="work-wordforge-v58-07-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="failed",
        expected_output="Review final player experience and product feel before delivery.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-wordforge-product-gap",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="retest",
        owner="nuo",
        workspace_ref=str(tmp_path),
        expected_output="Run clean retest for workspace write permission.",
        recovery_refs=[failed_gate_item.work_item_id],
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-v58-player-experience-gap",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        subject_ref=failed_gate_item.work_item_id,
        stage="delivery",
        task_type="product_development",
        rubric_version="final-player-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.84,
        speed=0.7,
        cost=0.8,
        risk=0.44,
        evidence_quality=0.86,
        collaboration_quality=0.82,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=[
            "fresh_real_player_review_pass",
            "final_game_commercial_ui_gap",
            "player_feel_touch_drag_affordance_gap",
        ],
        failure_category="delivery_failure",
        root_cause="The product needs another iteration before the player gate can pass.",
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="external_supervisor_real_player_gap_requires_iteration",
        created_by="external-supervisor-gpt5.5",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed_gate_item.work_item_id] = failed_gate_item
    control_plane.work_items[clean_retest.work_item_id] = clean_retest
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    result = NuoRuntimeRepairRunner(control_plane=control_plane).run(clean_retest)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "nuo_clean_retest_passed"
    assert result.followup_work_items == []


def test_nuo_clean_retest_uses_latest_run_to_avoid_rerunning_product_gate(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-wordforge-product-run-gap",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="repairing",
        current_plan_version="wordforge-v58",
    )
    failed_gate_item = WorkItem(
        work_item_id="work-wordforge-v58-07-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="failed",
        expected_output="Review final player experience and product feel before delivery.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-wordforge-product-run-gap",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="retest",
        owner="nuo",
        workspace_ref=str(tmp_path),
        expected_output="Run clean retest for workspace write permission.",
        recovery_refs=[failed_gate_item.work_item_id],
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed_gate_item.work_item_id] = failed_gate_item
    control_plane.work_items[clean_retest.work_item_id] = clean_retest
    control_plane.runs["run-product-gate-fail"] = RunRecord(
        run_id="run-product-gate-fail",
        work_item_id=failed_gate_item.work_item_id,
        runner_type="agent",
        runner_identity="external-supervisor-gpt5.5",
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        exit_status="failed",
        failure_category="delivery_failure",
    )

    result = NuoRuntimeRepairRunner(control_plane=control_plane).run(clean_retest)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.governance_signal == "nuo_clean_retest_passed"
    assert result.followup_work_items == []


def test_nuo_clean_retest_failed_ticket_is_persisted_by_runtime(tmp_path) -> None:
    missing_workspace = tmp_path / "missing"
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-followup",
        owner="product-owner",
        objective="Continue after a real workspace permission blocker.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    work_item = WorkItem(
        work_item_id="work-nuo-clean-retest-missing-workspace",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        workspace_ref=str(missing_workspace),
        expected_output="Run clean retest for workspace write permission.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[work_item.work_item_id] = work_item

    run = control_plane.run_work_item(
        work_item_id=work_item.work_item_id,
        runner=NuoRuntimeRepairRunner(control_plane=control_plane),
    )

    assert run is not None
    assert control_plane.missions[work_item.mission_id].status == "waiting_human"
    tickets = control_plane.list_collaboration_tickets(work_item.mission_id)
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.type == "operator_action"
    assert ticket.context_ref == work_item.work_item_id
    assert ticket.auto_resolvable_by == [work_item.work_item_id]


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


def test_runtime_observation_does_not_reclassify_human_acceptance_as_quality_gap() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-observation-human-only-gate",
        owner="product-owner",
        objective="Wait for target-player acceptance after automated product gates pass.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="wordforge-v58",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.gate_evaluations["gate-human-only"] = GateEvaluation(
        gate_evaluation_id="gate-human-only",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "wordforge-v58",
        subject_ref="work-mission-director-final",
        stage="acceptance",
        task_type="product_development",
        rubric_version="mission-director-final-v1",
        metric_pack_version="final-player-acceptance-v1",
        north_star_verdict="partial",
        result_quality=0.94,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.92,
        collaboration_quality=0.9,
        hard_gate_failures=["human_or_target_user_acceptance_missing"],
        next_action="needs_human",
        next_state="waiting_human",
        created_by="mission-director",
    )

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-test", built_at=NOW),
    )

    codes = [item.code for item in report.items]
    assert "quality_gate_not_passed" not in codes


def test_runtime_observation_ignores_failed_mission_director_human_acceptance_wait() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-observation-md-human-wait",
        owner="product-owner",
        objective="Do not recover Mission Director work that is only waiting for humans.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="wordforge-v58",
    )
    work_item = WorkItem(
        work_item_id="work-mission-director-final",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "wordforge-v58",
        type="review",
        owner="mission-director",
        status="failed",
        expected_output="Request human or target-user acceptance.",
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-md-human-wait",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "wordforge-v58",
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
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[work_item.work_item_id] = work_item
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-test", built_at=NOW),
    )

    codes = [item.code for item in report.items]
    assert "failed_work_without_recovery" not in codes
    assert "failed_work_recovery_incomplete" not in codes


def test_runtime_observation_ignores_recovered_governance_gate_after_later_pass() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-observation-recovered",
        owner="product-owner",
        objective="Do not keep stale governance blockers after a clean rerun.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    work_item = WorkItem(
        work_item_id="work-product-step",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
        expected_output="Recovered product work.",
    )
    stale_gate = GateEvaluation(
        gate_evaluation_id="gate-governance-stale-fail",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref=work_item.work_item_id,
        stage="governance",
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
        hard_gate_failures=["environment_probe_failed"],
        next_action="needs_repair",
        next_state="repairing",
        created_by="nuo-runtime-repair-runner",
    )
    clean_gate = GateEvaluation(
        gate_evaluation_id="gate-workitem-clean-pass",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type="product_development",
        rubric_version="rubric-v1",
        metric_pack_version="metrics-v1",
        north_star_verdict="pass",
        result_quality=0.96,
        speed=0.8,
        cost=0.8,
        risk=0.1,
        evidence_quality=0.95,
        collaboration_quality=0.9,
        next_action="continue",
        next_state="running",
        created_by="kun-runtime-runner",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[work_item.work_item_id] = work_item
    control_plane.gate_evaluations[stale_gate.gate_evaluation_id] = stale_gate
    control_plane.gate_evaluations[clean_gate.gate_evaluation_id] = clean_gate
    control_plane.runs["run-stale"] = RunRecord(
        run_id="run-stale",
        work_item_id=work_item.work_item_id,
        runner_type="agent",
        runner_identity="nuo",
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=2),
        exit_status="failed",
        failure_category="environment_failure",
        gate_evaluation_ref=stale_gate.gate_evaluation_id,
    )
    control_plane.runs["run-clean"] = RunRecord(
        run_id="run-clean",
        work_item_id=work_item.work_item_id,
        runner_type="agent",
        runner_identity="kun",
        started_at=NOW,
        ended_at=NOW,
        exit_status="succeeded",
        gate_evaluation_ref=clean_gate.gate_evaluation_id,
    )

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-test", built_at=NOW),
    )

    assert "quality_gate_not_passed" not in [item.code for item in report.items]


def test_runtime_observation_ignores_old_acceptance_gate_after_later_clean_recovery() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-runtime-observation-acceptance-recovered",
        owner="product-owner",
        objective="Rework after rejected acceptance, then ask for fresh acceptance.",
        task_type="product_development",
        status="running",
        current_plan_version="v1-acceptance-rework-12345678",
    )
    control_plane.missions[mission.mission_id] = mission
    failed_gate = GateEvaluation(
        gate_evaluation_id="gate-human-acceptance-old-partial",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "v1",
        subject_ref="manifest-old-delivery",
        stage="acceptance",
        task_type="product_development",
        rubric_version="human-product-acceptance-v1",
        metric_pack_version="final-product-v1",
        north_star_verdict="partial",
        result_quality=0.4,
        speed=0.7,
        cost=0.7,
        risk=0.4,
        evidence_quality=0.5,
        collaboration_quality=0.8,
        hard_gate_failures=["human_or_target_user_acceptance_missing"],
        next_action="needs_plan_change",
        next_state="changing_plan",
        created_by="mission-director",
    )
    recovery_artifact = ArtifactRecord(
        artifact_id="artifact-recovery-merge",
        kind="report",
        path_or_uri="control-plane://runtime/recovery-merge",
        content_hash="recovery-merge",
        created_by="kun-runtime-task-runner",
        mission_id=mission.mission_id,
        work_item_id="work-recovery-merge",
        supports=[failed_gate.gate_evaluation_id],
        freshness="fresh",
        source_quality="primary",
    )
    clean_gate = GateEvaluation(
        gate_evaluation_id="gate-clean-recovery-merge-pass",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "v1",
        subject_ref="work-recovery-merge",
        stage="merge",
        task_type="product_development",
        rubric_version="kun-recovery-v1",
        metric_pack_version="kun-recovery-v1",
        north_star_verdict="pass",
        result_quality=0.92,
        speed=0.7,
        cost=0.7,
        risk=0.2,
        evidence_quality=0.9,
        collaboration_quality=0.8,
        evidence_refs=[recovery_artifact.artifact_id],
        artifact_refs=[recovery_artifact.artifact_id],
        next_action="continue",
        next_state="running",
        created_by="kun-runtime-task-runner",
    )
    control_plane.artifacts[recovery_artifact.artifact_id] = recovery_artifact
    control_plane.gate_evaluations[failed_gate.gate_evaluation_id] = failed_gate
    control_plane.gate_evaluations[clean_gate.gate_evaluation_id] = clean_gate
    control_plane.runs["run-old-acceptance"] = RunRecord(
        run_id="run-old-acceptance",
        work_item_id="work-mission-director-old",
        runner_type="agent",
        runner_identity="mission-director",
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=2),
        exit_status="failed",
        failure_category="delivery_failure",
        gate_evaluation_ref=failed_gate.gate_evaluation_id,
    )
    control_plane.runs["run-clean-recovery"] = RunRecord(
        run_id="run-clean-recovery",
        work_item_id="work-recovery-merge",
        runner_type="agent",
        runner_identity="kun",
        started_at=NOW,
        ended_at=NOW,
        exit_status="succeeded",
        gate_evaluation_ref=clean_gate.gate_evaluation_id,
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


def test_runtime_observation_counts_control_plane_nuo_recovery_as_incomplete() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-control-plane-nuo-recovery",
        owner="product-owner",
        objective="Do not report missing recovery when Nuo delegated repair to control-plane.",
        task_type="product_development",
        status="repairing",
        current_plan_version="v1",
    )
    failed = WorkItem(
        work_item_id="work-failed-sandbox-test",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="test",
        owner="kun",
        status="failed",
        expected_output="failed sandboxed product test",
    )
    delegated_nuo_recovery = WorkItem(
        work_item_id="work-nuo-work-failed-sandbox-test-fix_wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        status="queued",
        idempotency_key="nuo-recovery:work-failed-sandbox-test:fix_wrapper:sandbox_permission_blocked",
        recovery_refs=[failed.work_item_id],
        expected_output="Repair wrapper availability, then rerun the same subject.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed.work_item_id] = failed
    control_plane.work_items[delegated_nuo_recovery.work_item_id] = delegated_nuo_recovery

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


def test_runtime_observation_treats_done_recovery_as_incomplete_until_work_retries() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-done-recovery-still-failed",
        owner="product-owner",
        objective="Do not hide failed work just because a clean retest ran.",
        task_type="product_development",
        status="repairing",
        current_plan_version="v1",
    )
    failed = WorkItem(
        work_item_id="work-failed-after-retest",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="test",
        owner="kun",
        status="failed",
        expected_output="failed product work",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        recovery_refs=[failed.work_item_id],
        expected_output="environment clean retest passed, but original work still failed",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed.work_item_id] = failed
    control_plane.work_items[clean_retest.work_item_id] = clean_retest

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


def test_runtime_observation_caps_capability_consumption_refs() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-capability-receipt-cap",
        owner="product-owner",
        objective="Bound capability receipt observations in long-running missions.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    control_plane.missions[mission.mission_id] = mission
    for index in range(40):
        work_item = WorkItem(
            work_item_id=f"work-capability-receipt-missing-{index:02d}",
            mission_id=mission.mission_id,
            task_plan_version="v1",
            type="execution",
            owner="kun",
            status="done",
            required_capability_refs=["cap-production-required"],
            expected_output="Execute with a production capability.",
        )
        control_plane.work_items[work_item.work_item_id] = work_item

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    item = next(item for item in report.items if item.code == "capability_consumption_unproven")
    assert len(item.evidence_refs) == 32
    assert "work-capability-receipt-missing-00" not in item.evidence_refs
    assert item.evidence_refs[-1] == "work-capability-receipt-missing-39"


def test_runtime_observation_ignores_superseded_capability_receipt_gaps() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-capability-receipt-current-plan",
        owner="product-owner",
        objective="Only current plan work should keep runtime observations noisy.",
        task_type="product_development",
        status="running",
        current_plan_version="v2",
    )
    old_work_item = WorkItem(
        work_item_id="work-old-required-capability",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
        required_capability_refs=["cap-production-required"],
        expected_output="Old superseded work lacks a behavior receipt.",
    )
    current_work_item = WorkItem(
        work_item_id="work-current-required-capability",
        mission_id=mission.mission_id,
        task_plan_version="v2",
        type="execution",
        owner="kun",
        status="done",
        required_capability_refs=["cap-production-required"],
        expected_output="Current work has a behavior receipt.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[old_work_item.work_item_id] = old_work_item
    control_plane.work_items[current_work_item.work_item_id] = current_work_item
    control_plane.artifacts["artifact-current-capability-behavior"] = ArtifactRecord(
        artifact_id="artifact-current-capability-behavior",
        kind="evidence",
        path_or_uri="mem://current-capability-behavior",
        content_hash="hash-current-capability-behavior",
        created_by="kun",
        mission_id=mission.mission_id,
        work_item_id=current_work_item.work_item_id,
        supports=["capability_behavior_receipt"],
    )

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "capability_consumption_unproven" not in [item.code for item in report.items]


def test_runtime_observation_does_not_recursively_flag_qi_nuo_followups() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-capability-receipt-followups",
        owner="product-owner",
        objective="Runtime observation follow-ups should not create receipt recursion.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    qi_followup = WorkItem(
        work_item_id="work-qi-observation-msn-capability-receipt-followups-signal",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="done",
        required_capability_refs=["cap-production-required"],
        expected_output="Audit the runtime observation.",
    )
    nuo_followup = WorkItem(
        work_item_id="work-nuo-observation-msn-capability-receipt-followups-signal",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        required_capability_refs=["cap-production-required"],
        expected_output="Classify the runtime observation.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[qi_followup.work_item_id] = qi_followup
    control_plane.work_items[nuo_followup.work_item_id] = nuo_followup

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "capability_consumption_unproven" not in [item.code for item in report.items]


def test_runtime_observation_accepts_strategy_optimization_as_behavior_receipt() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-capability-receipt-strategy",
        owner="product-owner",
        objective="A routed strategy optimization plan is a planner behavior receipt.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    strategy_work = WorkItem(
        work_item_id="work-kun-plan-change-msn-capability-receipt-strategy",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="research",
        owner="kun",
        status="done",
        required_capability_refs=["cap-production-required"],
        expected_output="Create a stricter continuation plan.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[strategy_work.work_item_id] = strategy_work
    control_plane.artifacts["artifact-strategy-optimization"] = ArtifactRecord(
        artifact_id="artifact-strategy-optimization",
        kind="evidence",
        path_or_uri="mem://strategy-optimization",
        content_hash="hash-strategy-optimization",
        created_by="kun",
        mission_id=mission.mission_id,
        work_item_id=strategy_work.work_item_id,
        supports=["strategy_optimization_plan", "dynamic_best_strategy"],
    )

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "capability_consumption_unproven" not in [item.code for item in report.items]


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
            task_plan_version="v1-acceptance-rework-2",
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


def test_runtime_observation_stops_mechanical_loop_after_acceptance() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rework-loop-accepted",
        owner="product-owner",
        objective="Accepted delivery should not keep showing active loop pressure.",
        task_type="product_development",
        status="learning_writeback",
        current_plan_version="v1-acceptance-rework-2",
        acceptance_ref="accept-delivery",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.acceptance_reviews["accept-delivery"] = AcceptanceReview(
        acceptance_id="accept-delivery",
        mission_id=mission.mission_id,
        task_plan_version="v1-acceptance-rework-2",
        delivery_manifest_ref="manifest-delivery",
        gate_evaluation_ref="gate-delivery",
        reviewer="product-owner",
        decision="accepted",
        satisfaction=0.9,
        reason="Accepted after current delivery review.",
    )
    for index in range(2):
        control_plane.gate_evaluations[f"gate-pressure-{index}"] = GateEvaluation(
            gate_evaluation_id=f"gate-pressure-{index}",
            mission_id=mission.mission_id,
            task_plan_version="v1-acceptance-rework-2",
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

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "mechanical_acceptance_rework_loop" not in [item.code for item in report.items]


def test_runtime_observation_ignores_cancelled_rework_versions_in_loop_count() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rework-loop-cancelled",
        owner="product-owner",
        objective="Cancelled stale rework should not keep loop pressure alive.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="v1-acceptance-rework-aaa11111",
    )
    active = WorkItem(
        work_item_id="work-active-rework",
        mission_id=mission.mission_id,
        task_plan_version="v1-acceptance-rework-aaa11111",
        type="test",
        owner="kun",
        status="done",
        expected_output="current rework finished",
    )
    cancelled = WorkItem(
        work_item_id="work-cancelled-rework",
        mission_id=mission.mission_id,
        task_plan_version="v1-acceptance-rework-bbb22222",
        type="test",
        owner="kun",
        status="cancelled",
        expected_output="bug-created stale rework",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[active.work_item_id] = active
    control_plane.work_items[cancelled.work_item_id] = cancelled

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "mechanical_acceptance_rework_loop" not in [item.code for item in report.items]


def test_runtime_observation_ignores_completed_rework_versions_in_loop_count() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rework-loop-completed-history",
        owner="product-owner",
        objective="Completed historical rework branches should not keep loop pressure alive.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="v1-acceptance-rework-ccc33333",
    )
    current = WorkItem(
        work_item_id="work-current-rework",
        mission_id=mission.mission_id,
        task_plan_version="v1-acceptance-rework-ccc33333",
        type="test",
        owner="kun",
        status="queued",
        expected_output="current rework still needs player-feel proof",
    )
    completed_history = WorkItem(
        work_item_id="work-completed-rework-history",
        mission_id=mission.mission_id,
        task_plan_version="v1-acceptance-rework-bbb22222",
        type="test",
        owner="kun",
        status="done",
        expected_output="historical rework evidence already archived",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[current.work_item_id] = current
    control_plane.work_items[completed_history.work_item_id] = completed_history

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "mechanical_acceptance_rework_loop" not in [item.code for item in report.items]


def test_runtime_observation_caps_mechanical_loop_evidence_refs() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rework-loop-cap",
        owner="product-owner",
        objective="Long acceptance rework loops should not bloat daemon observations.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="v1-acceptance-rework-9",
    )
    control_plane.missions[mission.mission_id] = mission
    for index in range(40):
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-pressure-{index:02d}",
            mission_id=mission.mission_id,
            task_plan_version="v1-acceptance-rework-9",
            subject_ref=f"ticket-acceptance-{index:02d}",
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

    item = next(item for item in report.items if item.code == "mechanical_acceptance_rework_loop")
    assert len(item.evidence_refs) == 32
    assert "gate-pressure-00" not in item.evidence_refs
    assert item.evidence_refs[-1] == "gate-pressure-39"


def test_runtime_observation_flags_cross_plan_delivery_rework_loop() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-cross-plan-rework-loop",
        owner="product-owner",
        objective="Repeated delivery passes must not hide a rework loop.",
        task_type="product_development",
        status="delivering",
        current_plan_version="wordforge-v36-acceptance-rework-33333333",
    )
    control_plane.missions[mission.mission_id] = mission
    for index, plan_version in enumerate(
        [
            "wordforge-v36-acceptance-rework-11111111",
            "wordforge-v36-acceptance-rework-22222222",
            "wordforge-v36-acceptance-rework-33333333",
        ]
    ):
        delivery_gate = GateEvaluation(
            gate_evaluation_id=f"gate-delivery-pass-{index}",
            mission_id=mission.mission_id,
            task_plan_version=plan_version,
            subject_ref=f"manifest-{index}",
            stage="delivery",
            task_type="product_development",
            rubric_version="delivery-v1",
            metric_pack_version="metrics-v1",
            north_star_verdict="pass",
            result_quality=0.9,
            speed=0.8,
            cost=0.8,
            risk=0.2,
            evidence_quality=0.8,
            collaboration_quality=0.8,
            thresholds={"result_quality": 0.8},
            artifact_refs=[f"artifact-delivery-{index}"],
            evidence_refs=[f"artifact-delivery-{index}"],
            next_action="ready_to_deliver",
            next_state="delivering",
            created_by="game-production-runner",
        )
        director_gate = delivery_gate.model_copy(
            update={
                "gate_evaluation_id": f"gate-mission-director-block-{index}",
                "subject_ref": f"work-mission-director-{index}",
                "stage": "acceptance",
                "north_star_verdict": "partial",
                "result_quality": 0.66,
                "hard_gate_failures": [
                    "human_or_target_user_acceptance_missing",
                    "gate_pass_not_product_done",
                ],
                "next_action": "needs_plan_change",
                "next_state": "changing_plan",
                "created_by": "mission-director",
            }
        )
        control_plane.gate_evaluations[delivery_gate.gate_evaluation_id] = delivery_gate
        control_plane.gate_evaluations[director_gate.gate_evaluation_id] = director_gate

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    item = next(item for item in report.items if item.code == "mechanical_acceptance_rework_loop")
    assert "gate-delivery-pass-0" in item.evidence_refs
    assert "gate-mission-director-block-2" in item.evidence_refs


def test_runtime_observation_ignores_superseded_acceptance_rework_loop() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-old-rework-loop",
        owner="product-owner",
        objective="Old acceptance pressure should not keep current execution noisy.",
        task_type="product_development",
        status="running",
        current_plan_version="v3-current-clean",
    )
    control_plane.missions[mission.mission_id] = mission
    for index in range(4):
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-old-pressure-{index}",
            mission_id=mission.mission_id,
            task_plan_version=f"v1-acceptance-rework-{index}",
            subject_ref=f"ticket-old-acceptance-{index}",
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

    assert "mechanical_acceptance_rework_loop" not in [item.code for item in report.items]


def test_runtime_observation_quality_gate_ignores_self_improvement_partials() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-self-improvement-quality",
        owner="product-owner",
        objective="Self-improvement diagnostics should not be counted as user product quality.",
        task_type="product_development",
        status="running",
        current_plan_version="v1",
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-nuo-partial-diagnostic",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-nuo-diagnostic",
        stage="governance",
        task_type="self_improvement",
        rubric_version="rubric-v1",
        metric_pack_version="metrics-v1",
        north_star_verdict="partial",
        result_quality=0.72,
        speed=0.8,
        cost=0.8,
        risk=0.4,
        evidence_quality=0.7,
        collaboration_quality=0.8,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["nuo_repair_requires_clean_retest"],
        next_action="needs_repair",
        next_state="repairing",
        created_by="nuo-runtime-repair-runner",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    assert "quality_gate_not_passed" not in [item.code for item in report.items]


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


def test_runtime_observation_requires_acceptance_ticket_for_latest_delivery() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-product-ticket-current",
        owner="product-owner",
        objective="Keep product delivery acceptance tied to the current artifact.",
        task_type="product_development",
        status="delivering",
        current_plan_version="v2",
        artifact_manifest_refs=["manifest-old-delivery", "manifest-fresh-delivery"],
    )
    old_manifest = ArtifactManifest(
        manifest_id="manifest-old-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-old"],
        primary_artifact_ref="artifact-old",
        evidence_refs=["artifact-old-evidence"],
        rollback_refs=["rollback-old"],
        created_by="kun",
        content_hash="old",
        supports_delivery=True,
    )
    fresh_manifest = ArtifactManifest(
        manifest_id="manifest-fresh-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-fresh"],
        primary_artifact_ref="artifact-fresh",
        evidence_refs=["artifact-fresh-evidence"],
        rollback_refs=["rollback-fresh"],
        created_by="kun",
        content_hash="fresh",
        supports_delivery=True,
    )
    stale_ticket = CollaborationTicket(
        ticket_id="collab-acceptance-msn-product-ticket-current-manifest-old-delivery",
        mission_id=mission.mission_id,
        type="review",
        role_needed="product-owner",
        why_needed="Old delivery needs review.",
        context_ref=old_manifest.manifest_id,
        risk_if_skipped="Old acceptance cannot validate a fresh delivery.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Accept or request rework.",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.artifact_manifests[old_manifest.manifest_id] = old_manifest
    control_plane.artifact_manifests[fresh_manifest.manifest_id] = fresh_manifest
    control_plane.collaboration_tickets[stale_ticket.ticket_id] = stale_ticket

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    items = {item.code: item for item in report.items}
    assert "human_acceptance_ticket_missing" in items
    assert items["human_acceptance_ticket_missing"].evidence_refs == [
        old_manifest.manifest_id,
        fresh_manifest.manifest_id,
    ]


def test_runtime_observation_ignores_stale_recovery_from_old_plan() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-current-plan-failure",
        owner="product-owner",
        objective="Current plan failures need current recovery evidence.",
        task_type="product_development",
        status="queued",
        current_plan_version="v2",
    )
    failed = WorkItem(
        work_item_id="work-colliding-visual-iteration",
        mission_id=mission.mission_id,
        task_plan_version="v2",
        type="execution",
        owner="kun",
        status="failed",
        expected_output="current visual iteration",
    )
    stale_qi = WorkItem(
        work_item_id="work-qi-old-recovery",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="done",
        recovery_refs=[failed.work_item_id],
        expected_output="old plan recovery with a colliding work id",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed.work_item_id] = failed
    control_plane.work_items[stale_qi.work_item_id] = stale_qi

    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=NOW),
    )

    items = {item.code: item for item in report.items}
    assert "failed_work_without_recovery" in items
    assert items["failed_work_without_recovery"].evidence_refs == [failed.work_item_id]


def test_daemon_runtime_observation_followups_are_plan_scoped() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-plan-scoped-followup",
        owner="product-owner",
        objective="Do not suppress current recovery with old-plan followups.",
        task_type="product_development",
        status="queued",
        current_plan_version="v1",
    )
    failed_v1 = WorkItem(
        work_item_id="work-colliding-failure",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="failed",
        expected_output="failed v1 work",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[failed_v1.work_item_id] = failed_v1
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="daemon-plan-scoped-followup",
        runners_by_owner={
            "qi": QiRuntimeGovernanceRunner(control_plane=control_plane),
            "nuo": NuoRuntimeRepairRunner(control_plane=control_plane),
        },
    )

    first_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )
    first_followups = set(first_report.observation_followup_ids)
    assert any("failed_work_without_recovery" in item_id for item_id in first_followups)

    control_plane.missions[mission.mission_id] = mission.model_copy(
        update={"current_plan_version": "v2"}
    )
    control_plane.work_items[failed_v1.work_item_id] = failed_v1.model_copy(
        update={"task_plan_version": "v2", "status": "failed"}
    )

    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=True,
    )
    second_followups = set(second_report.observation_followup_ids)

    assert any("failed_work_without_recovery" in item_id for item_id in second_followups)
    assert first_followups.isdisjoint(second_followups)
