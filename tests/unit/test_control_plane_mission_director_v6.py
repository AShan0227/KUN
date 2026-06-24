from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from kun.control_plane import (
    MISSION_DIRECTOR_OWNER,
    ArtifactManifest,
    ArtifactRecord,
    ControlPlaneDaemon,
    ExecutionContract,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    MissionDirectorModelConfig,
    MissionDirectorRunner,
    TaskPlan,
    WorkingContext,
    WorkItem,
    build_rainflow_ad_mission,
)

NOW = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _disable_v7_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these V6 runner tests hermetic (audit F154).

    MissionDirectorRunner.run() fires the V7 review bridge, which (enabled by
    default) spawns a thread that opens a real Postgres session (localhost:55432).
    Under unit tests that connection attempt failed silently in a background thread,
    making the suite non-hermetic and slow. The bridge has its own mocked coverage in
    test_mission_director_v7_bridge.py; here we just opt out so no DB is touched.
    The V6 result asserted below is unaffected (the bridge is fire-and-forget).
    """
    monkeypatch.setenv("KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", "false")


def _submit_product_mission(control_plane: InMemoryControlPlane) -> Mission:
    mission = Mission(
        mission_id="msn-product-game",
        owner="user",
        objective="Ship a finished tablet game, not only passing gates.",
        task_type="product_development",
        status="contracted",
        current_plan_version="game-v1",
    )
    plan = TaskPlan(
        plan_id="plan-game-v1",
        mission_id=mission.mission_id,
        version="game-v1",
        objective=mission.objective,
        acceptance_criteria=["real player experience is good enough"],
        decomposition=["research", "design", "build", "browser playtest", "human acceptance"],
        worker_plan=["kun builds", "mission-director supervises", "nuo diagnoses", "qi replays"],
        test_plan=["build", "browser interaction replay", "target-user acceptance"],
        rollback_plan=["restore previous playable build"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-game-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_project", "run_browser_playtest"],
        delivery_contract={
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
            "human_acceptance_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-game-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="mission-director",
        scope="delivery supervision",
        summary="Mission Director supervision test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not treat gate pass as final product acceptance."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "delivering"})
    delivery_artifact = ArtifactRecord(
        artifact_id="artifact-game-delivery",
        kind="answer",
        path_or_uri="mem://game",
        content_hash="hash-game",
        created_by="kun-game-production-runner",
        mission_id=mission.mission_id,
        supports=["directly_playable_game"],
    )
    evidence_artifact = ArtifactRecord(
        artifact_id="artifact-game-test",
        kind="test_result",
        path_or_uri="mem://test",
        content_hash="hash-test",
        created_by="kun-game-production-runner",
        mission_id=mission.mission_id,
        supports=["internal_test_passed"],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-game-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=[delivery_artifact.artifact_id, evidence_artifact.artifact_id],
        primary_artifact_ref=delivery_artifact.artifact_id,
        evidence_refs=[evidence_artifact.artifact_id],
        created_by="kun-game-production-runner",
        content_hash="hash-manifest",
        supports_delivery=True,
    )
    control_plane.artifacts[delivery_artifact.artifact_id] = delivery_artifact
    control_plane.artifacts[evidence_artifact.artifact_id] = evidence_artifact
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.gate_evaluations["gate-game-ready"] = GateEvaluation(
        gate_evaluation_id="gate-game-ready",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="work-final-delivery",
        stage="acceptance",
        task_type="product_development",
        rubric_version="game-delivery-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        evidence_refs=[evidence_artifact.artifact_id],
        artifact_refs=[delivery_artifact.artifact_id],
        confidence=0.84,
        next_action="ready_to_deliver",
        next_state="delivering",
        created_by="kun-game-production-runner",
    )
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"artifact_manifest_refs": [manifest.manifest_id]})
    return control_plane.missions[mission.mission_id]


def test_mission_director_blocks_gate_pass_as_product_done_without_player_acceptance() -> None:
    control_plane = InMemoryControlPlane()
    mission = _submit_product_mission(control_plane)
    work_item = WorkItem(
        work_item_id="work-mission-director-game",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "game-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review final product readiness.",
    )
    runner = MissionDirectorRunner(
        control_plane=control_plane,
        model_config=MissionDirectorModelConfig(
            model_id="gpt-5.5-director",
            model_tier="top",
            provider="test-provider",
        ),
    )

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert result.gate_evaluation.next_state == "changing_plan"
    assert "gate_pass_not_product_done" in result.gate_evaluation.hard_gate_failures
    assert "human_or_target_user_acceptance_missing" in result.gate_evaluation.hard_gate_failures
    assert "player_perception_evidence_missing" in result.gate_evaluation.hard_gate_failures
    assert [item.owner for item in result.followup_work_items] == ["qi", "kun"]
    assert result.followup_work_items[0].work_item_id == (
        "work-qi-strategy-replay-work-mission-director-game"
    )
    assert "strategy replay and process audit" in result.followup_work_items[0].expected_output
    assert result.followup_work_items[1].work_item_id == (
        "work-kun-plan-change-work-mission-director-game"
    )
    assert result.artifacts[0].supports == [
        "mission_director_review",
        "north_star_supervision",
        "task_decomposition_review",
        "worker_distribution_review",
        "delivery_not_gate_only_guard",
        "mission_director_model:gpt-5-5-director",
        "mission_director_tier:top",
    ]


def test_mission_director_does_not_wait_for_human_acceptance_while_rework_is_queued() -> None:
    control_plane = InMemoryControlPlane()
    mission = _submit_product_mission(control_plane)
    control_plane.work_items["work-current-plan-rework"] = WorkItem(
        work_item_id="work-current-plan-rework",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "game-v1",
        type="execution",
        owner="kun-game-production-runner",
        expected_output="Continue product rework before asking for final acceptance.",
        status="queued",
    )
    work_item = WorkItem(
        work_item_id="work-mission-director-game",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "game-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review final product readiness.",
    )
    runner = MissionDirectorRunner(control_plane=control_plane)

    result = runner.run(work_item)

    assert result.status == "done"
    assert result.gate_evaluation is None
    assert "aligned enough to continue" in result.summary


def test_daemon_auto_runs_configured_mission_director_review_for_delivery_state() -> None:
    control_plane = InMemoryControlPlane()
    mission = _submit_product_mission(control_plane)
    runner = MissionDirectorRunner(
        control_plane=control_plane,
        model_config=MissionDirectorModelConfig(model_id="director-model", model_tier="strong"),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={MISSION_DIRECTOR_OWNER: runner},
        daemon_id="director-daemon",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )

    director_work_items = [
        work_item_id
        for work_item_id in report.created_work_item_ids
        if work_item_id.startswith("work-mission-director-")
    ]
    assert len(director_work_items) == 1
    assert report.ran_work_item_ids == director_work_items
    assert control_plane.work_items[director_work_items[0]].priority == 100
    assert any(
        work_item_id.startswith("work-msn-product-game-game-v1-acceptance-rework-")
        for work_item_id in report.created_work_item_ids
    )
    assert control_plane.missions[mission.mission_id].status in {"changing_plan", "waiting_human"}
    director_artifacts = [
        artifact
        for artifact in control_plane.artifacts.values()
        if "mission_director_review" in artifact.supports
    ]
    assert director_artifacts
    assert "mission_director_model:director-model" in director_artifacts[0].supports

    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW.replace(second=1),
        max_work_items=1,
    )
    assert not any(
        work_item_id.startswith("work-mission-director-")
        for work_item_id in [
            *second_report.created_work_item_ids,
            *second_report.ran_work_item_ids,
        ]
    )


def test_mission_director_blocks_rainflow_delivery_without_gap_audit_demo_and_rollback_evidence(
    tmp_path,
) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    control_plane.missions[mission.mission_id] = mission.model_copy(update={"status": "delivering"})
    delivery_artifact = ArtifactRecord(
        artifact_id="artifact-rainflow-delivery",
        kind="answer",
        path_or_uri="mem://rainflow/delivery",
        content_hash="hash-rainflow-delivery",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["delivery"],
    )
    delivery_manifest = ArtifactManifest(
        manifest_id="manifest-rainflow-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=[delivery_artifact.artifact_id],
        primary_artifact_ref=delivery_artifact.artifact_id,
        evidence_refs=[delivery_artifact.artifact_id],
        created_by="kun",
        content_hash="hash-rainflow-manifest",
        supports_delivery=True,
    )
    control_plane.artifacts[delivery_artifact.artifact_id] = delivery_artifact
    control_plane.artifact_manifests[delivery_manifest.manifest_id] = delivery_manifest
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"artifact_manifest_refs": [delivery_manifest.manifest_id]})
    work_item = WorkItem(
        work_item_id="work-mission-director-rainflow-review",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review RainFlow quality evidence.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_plan_change"
    assert "rainflow_gap_audit_missing" in result.gate_evaluation.hard_gate_failures
    assert "rainflow_demo_comparison_missing" in result.gate_evaluation.hard_gate_failures
    assert "rainflow_rollback_evidence_missing" in result.gate_evaluation.hard_gate_failures


def test_mission_director_blocks_rainflow_ai_stage_until_phase1_acceptance_passes(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    control_plane.work_items["work-msn-rainflow-ad-video-06-stage1-transition-generation"] = (
        WorkItem(
            work_item_id="work-msn-rainflow-ad-video-06-stage1-transition-generation",
            mission_id=mission.mission_id,
            task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
            type="execution",
            owner="kun",
            expected_output="Generate transition clips.",
            status="queued",
        )
    )
    control_plane.artifacts["artifact-rainflow-gap-audit"] = ArtifactRecord(
        artifact_id="artifact-rainflow-gap-audit",
        kind="report",
        path_or_uri="mem://rainflow/gap-audit",
        content_hash="hash-gap-audit",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["engineering_gap_report"],
    )
    control_plane.artifacts["artifact-rainflow-demo"] = ArtifactRecord(
        artifact_id="artifact-rainflow-demo",
        kind="evidence",
        path_or_uri="mem://rainflow/demo",
        content_hash="hash-demo",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["demo_comparison"],
    )
    rollback_artifact = ArtifactRecord(
        artifact_id="artifact-rainflow-rollback",
        kind="report",
        path_or_uri="mem://rainflow/rollback",
        content_hash="hash-rollback",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["rollback_plan"],
    )
    control_plane.artifacts[rollback_artifact.artifact_id] = rollback_artifact
    work_item = WorkItem(
        work_item_id="work-mission-director-rainflow-stage-gate",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review RainFlow stage gate.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert (
        "rainflow_phase1_gate_missing_before_ai_stage" in result.gate_evaluation.hard_gate_failures
    )


def test_mission_director_allows_dependency_blocked_rainflow_ai_stage_plan(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    control_plane.work_items["work-msn-rainflow-ad-video-06-stage1-transition-generation"] = (
        WorkItem(
            work_item_id="work-msn-rainflow-ad-video-06-stage1-transition-generation",
            mission_id=mission.mission_id,
            task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
            type="execution",
            owner="kun",
            dependencies=["work-msn-rainflow-ad-video-05-phase1-acceptance-review"],
            expected_output="Generate transition clips after Phase 1 passes.",
            status="queued",
        )
    )
    control_plane.artifacts["artifact-rainflow-gap-audit"] = ArtifactRecord(
        artifact_id="artifact-rainflow-gap-audit",
        kind="report",
        path_or_uri="mem://rainflow/gap-audit",
        content_hash="hash-gap-audit",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["engineering_gap_report"],
    )
    control_plane.artifacts["artifact-rainflow-demo"] = ArtifactRecord(
        artifact_id="artifact-rainflow-demo",
        kind="evidence",
        path_or_uri="mem://rainflow/demo",
        content_hash="hash-demo",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["demo_comparison"],
    )
    control_plane.artifacts["artifact-rainflow-rollback"] = ArtifactRecord(
        artifact_id="artifact-rainflow-rollback",
        kind="report",
        path_or_uri="mem://rainflow/rollback",
        content_hash="hash-rollback",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["rollback_plan"],
    )
    work_item = WorkItem(
        work_item_id="work-mission-director-rainflow-stage-plan",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review RainFlow stage plan.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "continue"
    assert result.gate_evaluation.hard_gate_failures == []
    assert "aligned enough to continue" in result.summary


def test_mission_director_requires_human_phase1_acceptance_for_rainflow_review(
    tmp_path,
) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    control_plane.contracts[contract.contract_id] = contract.model_copy(
        update={"delivery_contract": {"project_path": str(tmp_path / "rainflow")}}
    )
    control_plane.artifacts["artifact-rainflow-gap-audit"] = ArtifactRecord(
        artifact_id="artifact-rainflow-gap-audit",
        kind="report",
        path_or_uri="mem://rainflow/gap-audit",
        content_hash="hash-gap-audit",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["engineering_gap_report"],
    )
    control_plane.artifacts["artifact-rainflow-demo"] = ArtifactRecord(
        artifact_id="artifact-rainflow-demo",
        kind="evidence",
        path_or_uri="mem://rainflow/demo",
        content_hash="hash-demo",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["demo_comparison"],
    )
    control_plane.artifacts["artifact-rainflow-rollback"] = ArtifactRecord(
        artifact_id="artifact-rainflow-rollback",
        kind="report",
        path_or_uri="mem://rainflow/rollback",
        content_hash="hash-rollback",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["rollback_plan"],
    )
    work_item = WorkItem(
        work_item_id=f"work-{mission.mission_id}-05-phase1-acceptance-review",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review whether Phase 1 mixed-edit quality may unlock AI generation.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "needs_human"
    assert "rainflow_phase1_human_acceptance_missing" in result.gate_evaluation.hard_gate_failures


def test_mission_director_allows_rainflow_phase1_review_after_human_pass_gate(tmp_path) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    work_item_id = f"work-{mission.mission_id}-05-phase1-acceptance-review"
    control_plane.artifacts["artifact-rainflow-gap-audit"] = ArtifactRecord(
        artifact_id="artifact-rainflow-gap-audit",
        kind="report",
        path_or_uri="mem://rainflow/gap-audit",
        content_hash="hash-gap-audit",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["engineering_gap_report"],
    )
    control_plane.artifacts["artifact-rainflow-demo"] = ArtifactRecord(
        artifact_id="artifact-rainflow-demo",
        kind="evidence",
        path_or_uri="mem://rainflow/demo",
        content_hash="hash-demo",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["demo_comparison"],
    )
    control_plane.artifacts["artifact-rainflow-rollback"] = ArtifactRecord(
        artifact_id="artifact-rainflow-rollback",
        kind="report",
        path_or_uri="mem://rainflow/rollback",
        content_hash="hash-rollback",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["rollback_plan"],
    )
    control_plane.gate_evaluations["gate-human-phase1-pass"] = GateEvaluation(
        gate_evaluation_id="gate-human-phase1-pass",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        subject_ref=work_item_id,
        stage="acceptance",
        task_type="product_development",
        rubric_version="human-rainflow-phase1-v1",
        metric_pack_version="human-rainflow-phase1-v1",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.75,
        cost=0.8,
        risk=0.18,
        evidence_quality=0.86,
        collaboration_quality=0.9,
        confidence=0.88,
        next_action="continue",
        next_state="running",
        governance_signal="human_phase1_acceptance_passed",
        created_by="human-supervisor",
    )
    work_item = WorkItem(
        work_item_id=work_item_id,
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review whether Phase 1 mixed-edit quality may unlock AI generation.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "continue"
    assert result.gate_evaluation.hard_gate_failures == []


def test_mission_director_accepts_current_plan_phase1_summary_without_circular_gate(
    tmp_path,
) -> None:
    control_plane = InMemoryControlPlane()
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=package.work_items,
    )
    plan_version = mission.current_plan_version or "rainflow-phase1-mixed-edit-v1"
    output_root = (
        tmp_path
        / "outputs"
        / f"work-{mission.mission_id}-{plan_version}-04-phase1-demo-comparison-and-retest"
    )
    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "phase1_acceptance_summary.json").write_text(
        json.dumps(
            {
                "human_accepted": True,
                "human_decision": "accept_phase1_demo",
                "baseline_comparison_status": "accepted",
                "phase1_gate_status": "ready",
                "phase1_gate_blockers": [],
                "human_checks": {
                    "strong_hook": True,
                    "clear_structure": True,
                    "conversion_path": True,
                    "creator_spoken_sales_safe": True,
                    "smooth_pacing": True,
                    "render_review_ready": True,
                },
            }
        ),
        encoding="utf-8",
    )
    final_report = reports_dir / "final_execution_report.md"
    final_report.write_text(
        "Phase 1 retest passed. Baseline-vs-current comparison accepted all required repairs. "
        "Stronger hook, conversion CTA, creator spoken-sales handling, and pacing are present. "
        "Rollback / isolation evidence references workspace_diff_and_artifact_inventory.",
        encoding="utf-8",
    )
    control_plane.artifacts["artifact-rainflow-current-final-report"] = ArtifactRecord(
        artifact_id="artifact-rainflow-current-final-report",
        kind="report",
        path_or_uri=str(final_report),
        content_hash="hash-final-report",
        created_by="kun",
        mission_id=mission.mission_id,
        work_item_id=f"work-{mission.mission_id}-{plan_version}-04-phase1-demo-comparison-and-retest",
        supports=["demo_comparison", "runtime_report", "rollback_plan"],
    )
    work_item = WorkItem(
        work_item_id=f"work-{mission.mission_id}-{plan_version}-05-phase1-acceptance-review",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review whether Phase 1 mixed-edit quality may unlock AI generation.",
    )

    result = MissionDirectorRunner(control_plane=control_plane).run(work_item)

    assert result.gate_evaluation is not None
    assert result.gate_evaluation.next_action == "continue"
    assert result.gate_evaluation.hard_gate_failures == []
