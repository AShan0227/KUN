from __future__ import annotations

import json

from kun.control_plane import (
    ArtifactRecord,
    CapabilityCandidate,
    CapabilityEvaluation,
    ExecutionContract,
    FileControlPlaneStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    build_capability_promotion,
    build_capability_rollback,
)
from kun.control_plane.productization import (
    ProductizationDogfoodRunner,
    accept_productization_dogfood_delivery,
    audit_control_plane_productization,
    audit_productization_code_boundary,
    behavior_signal_ref,
    build_capability_candidates_from_signals,
    build_dashboard_card,
    build_productization_dogfood_mission,
    build_productization_work_items,
    build_recovery_bundle,
    close_productization_collaboration_loop,
    compare_external_behavior_signals,
    discover_external_behavior_source_paths,
    distill_external_behavior_from_paths,
    distill_external_behavior_signals,
    load_external_behavior_sources,
    materialize_external_behavior_distillation,
    materialize_productization_code_boundary_audit,
    productionize_external_behavior_capabilities,
    productionize_external_behavior_from_source_paths,
    run_productization_dogfood_execution,
    submit_productization_dogfood_mission,
)


def _runtime(tmp_path):
    store = FileControlPlaneStore(tmp_path / "control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-productize",
        owner="kun",
        objective="Productize KUN V6 Control Plane",
        task_type="self_improvement",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-productize",
        mission_id=mission.mission_id,
        version="v6",
        objective=mission.objective,
        known_facts=["V6 product plan requires durable long-task closure"],
        acceptance_criteria=["state is recoverable", "pollution is not counted as KUN failure"],
        constraints=["do not optimize comparator agents"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-productize",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read external source samples", "write KUN-native tests"],
        forbidden_actions=["copy external implementation code", "ship without replay gates"],
    )
    context = WorkingContext(
        working_context_id="ctx-productize",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="control-plane",
        scope="productization",
        summary="Build missing KUN V6 productization closure loops.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_items = build_productization_work_items(
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=work_items,
    )
    return control_plane, store, mission


def _write_passing_ab_round(tmp_path):
    round_dir = tmp_path / "ab-round"
    round_dir.mkdir()
    (round_dir / "report.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "rankings": [
                    {
                        "agent_ref": "kun",
                        "avg_overall_score": 0.95,
                        "avg_effect_score": 0.93,
                        "avg_speed_score": 0.82,
                        "avg_cost_score": 0.81,
                        "avg_engineering_score": 1.0,
                    },
                    {
                        "agent_ref": "hermes",
                        "avg_overall_score": 0.9,
                        "avg_effect_score": 0.89,
                        "avg_speed_score": 0.8,
                        "avg_cost_score": 0.8,
                        "avg_engineering_score": 0.92,
                    },
                ],
                "task_scores": [{"task_id": f"frontier50-r02-t{index}"} for index in range(1, 6)],
                "gaps": [{"capability": "workflow", "delta": 0.0367}],
            }
        ),
        encoding="utf-8",
    )
    (round_dir / "comparator_health.json").write_text(
        json.dumps({"comparator_unhealthy": False}),
        encoding="utf-8",
    )
    (round_dir / "repair_tickets.json").write_text("[]", encoding="utf-8")
    (round_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps({"run": index}) for index in range(20)) + "\n",
        encoding="utf-8",
    )
    (round_dir / "reviews.jsonl").write_text(
        "\n".join(json.dumps({"review": index}) for index in range(45)) + "\n",
        encoding="utf-8",
    )
    return round_dir


def _promote_productization_capability(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    evidence_refs: list[str],
) -> None:
    candidate = CapabilityCandidate(
        candidate_id="cand-productization-runtime-default",
        capability_name="KUN-native external behavior runtime default",
        source="real_task_review",
        source_ref=mission_id,
        hypothesis="Only production-promoted distilled behaviors can become KUN Runtime defaults.",
        target_task_types=["self_improvement", "ops_tooling"],
        evidence_refs=evidence_refs or ["artifact-productization-evidence"],
        known_limits=["Production default must remain rollbackable."],
    )
    promotion = build_capability_promotion(
        candidate,
        [
            _capability_evaluation(stage)
            for stage in ["replay", "holdout", "shadow", "canary", "production"]
        ],
        target_stage="production",
        capability_id="cap-productization-runtime-default",
    )
    control_plane.apply_capability_promotion(promotion)


def _capability_evaluation(stage: str) -> CapabilityEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": f"eval-productization-{stage}",
        "candidate_id": "cand-productization-runtime-default",
        "stage": stage,
        "mission_id": "msn-productize",
        "task_plan_version": "v6",
        "subject_ref": f"work-productization-{stage}",
        "passed": True,
        "result_quality": 0.93,
        "speed": 0.75,
        "cost": 0.7,
        "risk": 0.2,
        "evidence_refs": [f"artifact-productization-evidence-{stage}"],
        "artifact_refs": [f"artifact-productization-report-{stage}"],
        "review_refs": [f"artifact-productization-review-{stage}"],
    }
    if stage in {"holdout", "canary", "production"}:
        payload["holdout_refs"] = ["artifact-productization-holdout"]
    if stage in {"canary", "production"}:
        payload["regression_refs"] = ["artifact-productization-regression"]
        payload["rollback_plan"] = ["disable productization runtime default"]
    return CapabilityEvaluation.model_validate(payload)


def _materialize_productization_evidence(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    artifact_id: str,
    *,
    support: str,
    created_by: str = "control-plane",
) -> ArtifactRecord:
    artifact = ArtifactRecord(
        artifact_id=artifact_id,
        kind="review" if "review" in support or "supervision" in support else "report",
        path_or_uri=f"control-plane://productization/{mission_id}/{artifact_id}",
        content_hash=f"hash-{artifact_id}",
        created_by=created_by,
        mission_id=mission_id,
        supports=[support, "productization_dogfood"],
        freshness="fresh",
        source_quality="primary",
    )
    control_plane.artifacts[artifact.artifact_id] = artifact
    if control_plane.store is not None:
        control_plane.store.put_artifact_record(artifact)
    return artifact


def test_productization_recovery_bundle_survives_file_store_hydration(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    before = build_recovery_bundle(control_plane, mission.mission_id)

    recovered = InMemoryControlPlane(store=store)
    after = build_recovery_bundle(recovered, mission.mission_id)

    assert before.mission_id == after.mission_id
    assert after.current_plan_version == "v6"
    assert "work-v6-qi-ab-runner" in after.ready_work_item_ids
    assert after.resume_policy == "resume_next_ready_work_item"
    assert after.ledger_event_count >= 1


def test_productization_dogfood_mission_is_submit_ready_and_recoverable(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "dogfood.json")
    control_plane = InMemoryControlPlane(store=store)
    package = build_productization_dogfood_mission()

    mission = submit_productization_dogfood_mission(control_plane, package)
    recovered = InMemoryControlPlane(store=store)
    audit = audit_control_plane_productization(recovered, mission.mission_id)

    assert mission.status == "queued"
    assert recovered.missions[mission.mission_id].current_plan_version == "v6-productization"
    assert "work-v6-persistence-recovery" in audit.recovery_bundle.ready_work_item_ids
    assert "collaboration_tickets" in audit.missing_subsystems
    assert package.execution_contract.risk_policy["quality_precedes_speed_cost"] is True


def test_productization_dashboard_is_nontechnical_and_gate_aware(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    progress = control_plane.progress_report(mission.mission_id)

    card = build_dashboard_card(progress)

    assert card.headline == "Mission is moving under Control Plane."
    assert "任务已排队" in card.status_text
    assert card.next_step
    assert card.safe_to_continue is True
    assert "work-v6-persistence-recovery" in card.technical_refs


def test_productization_audit_creates_repair_items_for_missing_closures(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)

    report = audit_control_plane_productization(control_plane, mission.mission_id)

    assert report.ready is False
    assert "collaboration_tickets" in report.missing_subsystems
    assert "qi_capability_evolution" in report.missing_subsystems
    assert "external_behavior_distillation" in report.missing_subsystems
    gap_items = {gap.subsystem: gap.repair_work_item for gap in report.gaps}
    assert gap_items["collaboration_tickets"].owner == "control-plane"
    assert gap_items["qi_capability_evolution"].owner == "qi"
    assert gap_items["external_behavior_distillation"].type == "research"


def test_productization_audit_passes_when_all_closure_loops_exist(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    signals = distill_external_behavior_signals(
        {
            "external_repos/openclaw/README.md": "Gateway sessions tools and multi-agent isolated workspace.",
            "external_repos/hermes-agent/RELEASE_v0.8.0.md": (
                "Background notify_on_complete with inactivity timeout and approval buttons."
            ),
        }
    )
    ticket = close_productization_collaboration_loop(
        control_plane,
        mission.mission_id,
        context_ref="ctx-productize",
    )
    materialized = materialize_external_behavior_distillation(
        control_plane,
        mission.mission_id,
        signals,
    )
    _promote_productization_capability(
        control_plane,
        mission_id=mission.mission_id,
        evidence_refs=materialized.artifact_refs,
    )
    recovered = InMemoryControlPlane(store=store)

    report = audit_control_plane_productization(
        recovered,
        mission.mission_id,
    )

    assert report.ready is True
    assert report.missing_subsystems == []
    assert ticket.status == "answered"
    assert materialized.candidate_count == len(signals)
    assert materialized.artifact_refs
    assert materialized.capability_profile_refs
    assert all(
        control_plane.capability_profiles[profile_ref].runtime_enabled is False
        for profile_ref in materialized.capability_profile_refs
    )
    assert "external_behavior_distillation" in report.present_subsystems
    assert "qi_capability_evolution" in report.present_subsystems
    assert recovered.list_default_runtime_capabilities()[0].promotion_stage == "production"


def test_productization_audit_does_not_count_rolled_back_production_capability(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    _promote_productization_capability(
        control_plane,
        mission_id=mission.mission_id,
        evidence_refs=["artifact-productization-distillation"],
    )
    profile = control_plane.list_default_runtime_capabilities()[0]
    rollback = build_capability_rollback(
        profile,
        _capability_evaluation("production").model_copy(
            update={
                "evaluation_id": "eval-productization-production-failed",
                "passed": False,
                "result_quality": 0.7,
                "hard_gate_failures": ["production_runtime_regression"],
                "evidence_refs": ["artifact-productization-dogfood-failure"],
            }
        ),
        reason="dogfood regression invalidated production default",
    )
    control_plane.apply_capability_rollback(rollback)
    recovered = InMemoryControlPlane(store=store)

    report = audit_control_plane_productization(recovered, mission.mission_id)

    assert "qi_capability_evolution" in report.missing_subsystems
    assert "qi_capability_evolution" not in report.present_subsystems
    assert recovered.list_default_runtime_capabilities() == []


def test_productization_collaboration_requires_closed_ticket_loop(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)

    report = audit_control_plane_productization(control_plane, mission.mission_id)

    assert "collaboration_tickets" in report.missing_subsystems

    close_productization_collaboration_loop(
        control_plane,
        mission.mission_id,
        context_ref="ctx-productize",
    )
    closed_report = audit_control_plane_productization(control_plane, mission.mission_id)

    assert "collaboration_tickets" in closed_report.present_subsystems


def test_productization_dogfood_execution_runs_queue_and_ab_regression(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    signals = distill_external_behavior_signals(
        {
            "external_repos/openclaw/README.md": "Gateway sessions tools and multi-agent isolated workspace.",
            "external_repos/hermes-agent/RELEASE_v0.8.0.md": (
                "Background notify_on_complete with inactivity timeout, behavioral benchmark, "
                "structured logging, approval buttons, and tool result file persistence."
            ),
        }
    )
    materialize_external_behavior_distillation(control_plane, mission.mission_id, signals)
    _promote_productization_capability(
        control_plane,
        mission_id=mission.mission_id,
        evidence_refs=["artifact-productization-distillation"],
    )
    close_productization_collaboration_loop(
        control_plane,
        mission.mission_id,
        context_ref="ctx-productize",
    )
    runner = ProductizationDogfoodRunner(
        control_plane=control_plane,
        ab_round_dir=_write_passing_ab_round(tmp_path),
        ab_round_id="round-02-regression",
    )

    execution = run_productization_dogfood_execution(
        control_plane,
        mission.mission_id,
        runner=runner,
    )
    recovered = InMemoryControlPlane(store=store)

    assert execution.stopped_reason == "delivery_ready"
    assert execution.mission_status == "delivering"
    assert len(execution.run_refs) == 7
    assert execution.ab_regression_gate_ref == "gate-qi-ab-round-02-regression"
    assert execution.delivery_manifest_ref == "manifest-msn-productize-delivery"
    assert execution.final_gate_ref == "gate-msn-productize-delivery"
    assert execution.recovery_bundle_artifact_ref == (
        "artifact-msn-productize-dogfood-recovery-bundle"
    )
    assert execution.execution_report_artifact_ref == (
        "artifact-msn-productize-dogfood-execution-report"
    )
    assert all(
        item.status == "done"
        for item in recovered.work_items.values()
        if item.mission_id == mission.mission_id
    )
    assert recovered.missions[mission.mission_id].status == "delivering"
    assert recovered.runs
    assert execution.recovery_bundle_artifact_ref in recovered.artifacts
    assert execution.execution_report_artifact_ref in recovered.artifacts
    assert (
        "real_long_task_dogfood"
        in recovered.artifacts[execution.execution_report_artifact_ref].supports
    )
    assert (
        "cross_restart_resume"
        in recovered.artifacts[execution.recovery_bundle_artifact_ref].supports
    )
    assert recovered.artifact_manifests["manifest-msn-productize-delivery"].supports_delivery
    progress = recovered.progress_report(mission.mission_id)
    assert progress.latest_gate_ref == "gate-msn-productize-delivery"
    assert progress.latest_failure_category is None

    acceptance = accept_productization_dogfood_delivery(
        recovered,
        mission.mission_id,
        close_after_learning=True,
    )
    closed = InMemoryControlPlane(store=store)

    assert acceptance.closed is True
    assert acceptance.learning_artifact_ref == "artifact-msn-productize-learning-writeback"
    assert "candidate-real-task-review-msn-productize" in acceptance.learning_candidate_refs
    assert acceptance.mission_status == "closed"
    assert closed.missions[mission.mission_id].acceptance_ref == "accept-msn-productize-delivery"
    assert closed.missions[mission.mission_id].status == "closed"
    assert closed.acceptance_reviews["accept-msn-productize-delivery"].decision == "accepted"
    assert acceptance.learning_artifact_ref in closed.artifacts
    assert "qi_capability_evolution" in closed.artifacts[acceptance.learning_artifact_ref].supports


def test_external_behavior_distillation_creates_kun_native_candidates() -> None:
    signals = distill_external_behavior_signals(
        {
            "external_repos/openclaw/README.md": (
                "Local-first Gateway with sessions, tools, multi-agent routing, "
                "isolated agent workspace, and approval buttons."
            ),
            "external_repos/hermes-agent/RELEASE_v0.8.0.md": (
                "Background process notify_on_complete, inactivity timeout, "
                "behavioral benchmarking, structured logging, and tool result file persistence."
            ),
        }
    )

    refs = {behavior_signal_ref(signal) for signal in signals}
    assert "openclaw:local-first-gateway-session-tool-event-routing:persistence_recovery" in refs
    assert (
        "openclaw:explicit-approval-interaction-with-resumable-tickets:collaboration_tickets"
        in refs
    )
    assert (
        "hermes:activity-based-long-run-timeout-instead-of-wall-clock-kill:persistence_recovery"
        in refs
    )
    assert "hermes:behavioral-benchmark-driven-tool-use-guidance:qi_capability_evolution" in refs

    candidates = build_capability_candidates_from_signals(signals)

    assert candidates
    assert all(candidate.source == "open_source_project" for candidate in candidates)
    assert all(
        "kun-control-plane:" in candidate.proposed_change_refs[0] for candidate in candidates
    )
    assert all(
        candidate.target_task_types == ["self_improvement", "ops_tooling"]
        for candidate in candidates
    )


def test_external_behavior_productionization_requires_dogfood_and_promotes_defaults(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    signals = distill_external_behavior_signals(
        {
            "external_repos/openclaw/README.md": (
                "Gateway sessions tools with multi-agent isolated workspace and approval buttons."
            ),
            "external_repos/hermes-agent/agent/context_compressor.py": (
                "Structured logging with background notify and activity inactivity timeout."
            ),
        }
    )
    comparisons = compare_external_behavior_signals(signals)

    assert comparisons
    assert {comparison.decision for comparison in comparisons}.issubset({"adopt", "merge"})

    try:
        productionize_external_behavior_capabilities(
            control_plane,
            mission.mission_id,
            signals,
            dogfood_validation_refs=[],
            regression_refs=["artifact-regression"],
            supervisor_review_ref="review-gpt-5.5",
        )
    except ValueError as exc:
        assert "dogfood_validation_refs" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected dogfood evidence to be required")

    try:
        productionize_external_behavior_capabilities(
            control_plane,
            mission.mission_id,
            signals,
            dogfood_validation_refs=["artifact-real-long-task-dogfood"],
            regression_refs=["artifact-ab-regression-round-02"],
            supervisor_review_ref="review-gpt-5.5-source-behavior",
        )
    except ValueError as exc:
        assert "existing Control Plane evidence" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected evidence refs to be materialized before production")

    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "artifact-real-long-task-dogfood",
        support="real_long_task_dogfood",
    )
    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "artifact-ab-regression-round-02",
        support="ab_regression_gate",
    )
    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "review-gpt-5.5-source-behavior",
        support="gpt55_supervision_review",
        created_by="gpt-5.5",
    )
    record = productionize_external_behavior_capabilities(
        control_plane,
        mission.mission_id,
        signals,
        dogfood_validation_refs=["artifact-real-long-task-dogfood"],
        regression_refs=["artifact-ab-regression-round-02"],
        supervisor_review_ref="review-gpt-5.5-source-behavior",
    )
    recovered = InMemoryControlPlane(store=store)

    assert record.adopted_count >= 1
    assert record.merged_count >= 1
    assert len(record.capability_profile_refs) == len(signals)
    assert all(
        profile.promotion_stage == "production"
        for profile in recovered.list_default_runtime_capabilities()
    )
    assert all(
        "no_external_code_copy" in recovered.artifacts[artifact_ref].supports
        for artifact_ref in record.artifact_refs
    )


def test_external_behavior_distillation_reads_only_allowed_source_paths(tmp_path) -> None:
    openclaw_root = tmp_path / "external_repos" / "openclaw"
    hermes_root = tmp_path / "external_repos" / "hermes-agent"
    openclaw_root.mkdir(parents=True)
    hermes_root.mkdir(parents=True)
    openclaw_readme = openclaw_root / "README.md"
    hermes_release = hermes_root / "RELEASE.md"
    openclaw_readme.write_text(
        "Gateway sessions tools with multi-agent isolated workspace.",
        encoding="utf-8",
    )
    hermes_release.write_text(
        "Background notify_on_complete with inactivity timeout.",
        encoding="utf-8",
    )

    sources = load_external_behavior_sources(
        [openclaw_readme, hermes_release],
        allowed_roots=[openclaw_root, hermes_root],
    )
    signals = distill_external_behavior_from_paths(
        [openclaw_readme, hermes_release],
        allowed_roots=[openclaw_root, hermes_root],
    )

    assert str(openclaw_readme.resolve()) in sources
    assert any(signal.origin == "openclaw" for signal in signals)
    assert any(signal.origin == "hermes" for signal in signals)
    try:
        load_external_behavior_sources(
            [tmp_path / "outside.md"],
            allowed_roots=[openclaw_root],
        )
    except ValueError as exc:
        assert "outside allowed roots" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected outside source to be rejected")


def test_external_behavior_source_batch_productionizes_latest_allowed_sources(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    openclaw_root = tmp_path / "external_repos" / "openclaw"
    hermes_root = tmp_path / "external_repos" / "hermes-agent"
    openclaw_root.mkdir(parents=True)
    hermes_root.mkdir(parents=True)
    (openclaw_root / "README.md").write_text(
        "Gateway sessions tools with multi-agent isolated workspace and approval buttons.",
        encoding="utf-8",
    )
    (hermes_root / "hermes_logging.py").write_text(
        "Structured logging with background notify and activity inactivity timeout. "
        "Tool result file persistence keeps large output as evidence.",
        encoding="utf-8",
    )
    ignored = openclaw_root / "node_modules" / "ignored.md"
    ignored.parent.mkdir()
    ignored.write_text("gateway sessions tools", encoding="utf-8")
    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "artifact-real-long-task-dogfood",
        support="real_long_task_dogfood",
    )
    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "artifact-ab-regression-round-02",
        support="ab_regression_gate",
    )
    _materialize_productization_evidence(
        control_plane,
        mission.mission_id,
        "review-gpt-5.5-source-behavior",
        support="gpt55_supervision_review",
        created_by="gpt-5.5",
    )

    discovered = discover_external_behavior_source_paths([openclaw_root, hermes_root])
    run = productionize_external_behavior_from_source_paths(
        control_plane,
        mission.mission_id,
        discovered,
        allowed_roots=[openclaw_root, hermes_root],
        dogfood_validation_refs=["artifact-real-long-task-dogfood"],
        regression_refs=["artifact-ab-regression-round-02"],
        supervisor_review_ref="review-gpt-5.5-source-behavior",
    )
    recovered = InMemoryControlPlane(store=store)

    assert str(ignored.resolve()) not in discovered
    assert run.source_paths == discovered
    assert run.signal_refs
    assert run.distillation_record.candidate_count == len(run.signal_refs)
    assert set(run.productionization_record.capability_profile_refs) == set(
        run.runtime_capability_refs
    )
    assert all(
        profile.promotion_stage == "production"
        for profile in recovered.list_default_runtime_capabilities()
    )


def test_productization_code_boundary_audit_accepts_clean_submission(tmp_path) -> None:
    changed_paths = [
        "docs/v6/KUN-V6.md",
        "docs/v6/KUN-V6-DEVELOPMENT-PLAN.md",
        "kun/control_plane/productization.py",
        "kun/control_plane/daemon_service.py",
        "kun/control_plane/v6.py",
        "kun/api/control_plane.py",
        "kun/cli.py",
        "tests/unit/test_control_plane_productization_v6.py",
        "tests/unit/test_control_plane_daemon_service_v6.py",
        "tests/unit/test_control_plane_v6.py",
        "tests/unit/test_control_plane_api_v6.py",
        "tests/unit/test_control_plane_cli_v6.py",
        "frontend/src/app/control-plane/page.tsx",
        "frontend/src/app/layout.tsx",
        "frontend/src/kunApiClient.ts",
        "artifacts/control_plane/kun-v6-productization-dogfood.json",
    ]

    report = audit_productization_code_boundary(changed_paths, repo_root=tmp_path)

    assert report.ready is True
    assert report.checked_path_count == len(changed_paths)
    assert "kun/control_plane/productization.py" in report.formal_code_paths
    assert "kun/control_plane/daemon_service.py" in report.formal_code_paths
    assert "kun/control_plane/v6.py" in report.formal_code_paths
    assert "kun/cli.py" in report.formal_code_paths
    assert "tests/unit/test_control_plane_productization_v6.py" in report.test_paths
    assert "tests/unit/test_control_plane_daemon_service_v6.py" in report.test_paths
    assert "tests/unit/test_control_plane_v6.py" in report.test_paths
    assert "tests/unit/test_control_plane_cli_v6.py" in report.test_paths
    assert "frontend/src/kunApiClient.ts" in report.frontend_paths
    assert (
        "artifacts/control_plane/kun-v6-productization-dogfood.json" in report.artifact_state_paths
    )
    assert "Control Plane runtime changes" in report.recommended_pr_sections
    assert "Dogfood state and evidence" in report.recommended_pr_sections
    assert report.findings == []


def test_productization_code_boundary_audit_classifies_collapsed_git_directories(
    tmp_path,
) -> None:
    report = audit_productization_code_boundary(
        [
            "docs/v6",
            "kun/control_plane",
            "tests",
            "frontend/src/app/control-plane",
            "artifacts/control_plane",
        ],
        repo_root=tmp_path,
    )

    assert report.ready is True
    assert report.product_doc_paths == ["docs/v6"]
    assert report.formal_code_paths == ["kun/control_plane"]
    assert report.test_paths == ["tests"]
    assert report.frontend_paths == ["frontend/src/app/control-plane"]
    assert report.artifact_state_paths == ["artifacts/control_plane"]


def test_productization_code_boundary_audit_blocks_mixed_state_and_code(tmp_path) -> None:
    changed_paths = [
        "kun/control_plane/dogfood-state.json",
        "artifacts/control_plane/temp_runner.py",
        ".next/server/app.js",
        "scratch/notes.txt",
    ]

    report = audit_productization_code_boundary(changed_paths, repo_root=tmp_path)

    assert report.ready is False
    assert "kun/control_plane/dogfood-state.json" in report.formal_code_paths
    assert "artifacts/control_plane/temp_runner.py" in report.artifact_state_paths
    assert "scratch/notes.txt" in report.unknown_paths
    summaries = {finding.summary for finding in report.findings}
    assert "Runtime state or generated artifact data is stored under formal code." in summaries
    assert "Executable source code is stored under dogfood artifact state." in summaries
    assert "Generated output should not be mixed into the productization submission." in summaries
    assert "Changed path is outside the known KUN V6 productization boundary." in summaries


def test_productization_code_boundary_audit_requires_regression_test_for_new_code(
    tmp_path,
) -> None:
    report = audit_productization_code_boundary(
        ["kun/control_plane/daemon_runtime.py"],
        repo_root=tmp_path,
    )

    assert report.ready is False
    assert any(
        "tests/unit/test_control_plane_daemon_runtime_v6.py" in finding.summary
        for finding in report.findings
    )


def test_productization_code_boundary_audit_materializes_as_control_plane_evidence(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    report = audit_productization_code_boundary(
        [
            "kun/control_plane/productization.py",
            "tests/unit/test_control_plane_productization_v6.py",
        ],
        repo_root=tmp_path,
    )

    artifact = materialize_productization_code_boundary_audit(
        control_plane,
        mission.mission_id,
        report,
    )
    recovered = InMemoryControlPlane(store=store)

    assert artifact.artifact_id == "artifact-msn-productize-code-boundary-audit"
    assert artifact.artifact_id in recovered.artifacts
    assert "code_boundary_audit" in recovered.artifacts[artifact.artifact_id].supports

    blocked = audit_productization_code_boundary(["scratch/notes.txt"], repo_root=tmp_path)
    try:
        materialize_productization_code_boundary_audit(
            control_plane,
            mission.mission_id,
            blocked,
        )
    except ValueError as exc:
        assert "blockers" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected blocked audit to be rejected")
