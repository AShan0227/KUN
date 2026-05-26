from __future__ import annotations

from pathlib import Path

from kun.control_plane import FileControlPlaneStore, InMemoryControlPlane, WorkingContext, WorkItem
from kun.control_plane.daemon import _acceptance_rework_task_plan, _acceptance_rework_work_items
from kun.control_plane.rainflow_ad_mission import (
    RAINFLOW_AD_PRODUCTION_MODE,
    build_rainflow_ad_mission,
    discover_rainflow_mission_packages,
)
from kun.control_plane.v6 import ExecutionContract, GateEvaluation, Mission, TaskPlan


def test_build_rainflow_ad_mission_creates_isolated_workspace_and_stage_gates(
    tmp_path: Path,
) -> None:
    package = build_rainflow_ad_mission(root_dir=tmp_path)

    mission = package.mission
    plan = package.task_plan
    contract = package.execution_contract
    context = package.working_context
    workspace_path = (tmp_path / mission.mission_id / "workspace").resolve()
    output_path = (tmp_path / mission.mission_id / "outputs").resolve()

    assert mission.task_type == "product_development"
    assert mission.status == "contracted"
    assert plan.version == "rainflow-phase1-mixed-edit-v1"
    assert any("Phase 1 mixed-edit output" in item for item in plan.acceptance_criteria)
    assert any("Stage 1 AI video generation" in item for item in plan.decomposition)
    assert contract.delivery_contract["production_mode"] == RAINFLOW_AD_PRODUCTION_MODE
    assert contract.delivery_contract["workspace_path"] == str(workspace_path)
    assert contract.delivery_contract["output_dir"] == str(output_path)
    assert contract.delivery_contract["workspace_ref"] == f"workspace://{workspace_path}"
    assert (
        contract.delivery_contract["sandbox_ref"]
        == f"sandbox://workspace-snapshot/{mission.mission_id}"
    )
    assert context.scope == "isolated-rainflow-ad-mission"
    assert all(item.workspace_ref == f"workspace://{workspace_path}" for item in package.work_items)
    assert all(
        item.sandbox_ref == f"sandbox://workspace-snapshot/{mission.mission_id}"
        for item in package.work_items
    )
    assert any("phase1-mixed-edit-repair" in item.work_item_id for item in package.work_items)
    assert workspace_path.exists()
    assert output_path.exists()


def test_build_rainflow_ad_mission_skips_occupied_preferred_ports(tmp_path: Path) -> None:
    first = build_rainflow_ad_mission(
        root_dir=tmp_path,
        mission_id="msn-rainflow-port-a",
        preferred_ports=(3411, 3412),
    )
    second = build_rainflow_ad_mission(
        root_dir=tmp_path,
        mission_id="msn-rainflow-port-b",
        preferred_ports=(3411, 3412),
    )

    first_ports = first.execution_contract.delivery_contract["ports"]
    second_ports = second.execution_contract.delivery_contract["ports"]
    assert first_ports == {"app": 3411, "preview": 3412}
    assert second_ports["app"] == 3413
    assert second_ports["preview"] == 3414


def test_discover_rainflow_mission_packages_recovers_external_workspace_from_store(
    tmp_path: Path,
) -> None:
    external_workspace = (tmp_path / "external-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-output").resolve()
    external_output.mkdir()
    recovery_store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(recovery_store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Advance RainFlow Phase 1 mixed-edit quality in an isolated external workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-recovery",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["recover the persisted external RainFlow workspace"],
        constraints=["keep AI stages gated behind Phase 1"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
            "phase1_must_pass_before_ai_video": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission from persisted control-plane state.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue the recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    records = discover_rainflow_mission_packages(
        root_dir=tmp_path / "rainflow-missions",
        recovery_store_paths=[recovery_store_path],
    )

    assert len(records) == 1
    record = records[0]
    assert record.mission_id == mission.mission_id
    assert record.workspace_path == str(external_workspace)
    assert record.output_dir == str(external_output)
    assert record.ports == {"app": 5188, "preview": 5189}


def test_rainflow_acceptance_rework_plan_and_work_items_hold_ai_stages_until_phase1_passes() -> (
    None
):
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Ship polished information-flow ad videos.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="rainflow-phase1-mixed-edit-v2",
    )
    base_plan = TaskPlan(
        plan_id="plan-rainflow-phase1",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v2",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 must clearly improve ad quality against baseline."],
        constraints=["Do not ship without fresh demo evidence."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow",
        mission_id=mission.mission_id,
        task_plan_version=base_plan.version,
        allowed_actions=["write isolated workspace"],
        delivery_contract={
            "production_mode": RAINFLOW_AD_PRODUCTION_MODE,
            "project_path": "/tmp/rainflow",
        },
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-phase1-rejected",
        mission_id=mission.mission_id,
        task_plan_version=base_plan.version,
        subject_ref="acceptance-ticket-rainflow",
        stage="acceptance",
        task_type="product_development",
        rubric_version="rainflow-ad-quality-v1",
        metric_pack_version="rainflow-hook-cta-v1",
        north_star_verdict="partial",
        result_quality=0.68,
        speed=0.76,
        cost=0.81,
        risk=0.42,
        evidence_quality=0.66,
        collaboration_quality=0.73,
        hard_gate_failures=["hook_gap", "cta_gap", "creator_integration_gap"],
        failure_category="delivery_failure",
        root_cause=(
            "Mixed edit still lacks a strong opening hook, conversion flow, creator integration, "
            "and a convincing CTA."
        ),
        responsibility_scope="kun_auto",
        confidence=0.84,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="rainflow_phase1_quality_below_bar",
        created_by="mission-director",
    )

    plan = _acceptance_rework_task_plan(
        mission=mission,
        gate=gate,
        base_plan=base_plan,
        plan_version="rainflow-phase1-mixed-edit-v2-acceptance-rework-abc12345",
        contract=contract,
    )
    work_items = _acceptance_rework_work_items(
        mission=mission,
        gate=gate,
        plan_version=plan.version,
        contract=contract,
    )

    assert any(
        "Phase 1 mixed-edit demo comparison proves stronger hook" in item
        for item in plan.acceptance_criteria
    )
    assert any(
        "Keep AI transition/regeneration/advanced-creative stages disabled" in item
        for item in plan.decomposition
    )
    created_ids = {item.work_item_id for item in work_items}
    assert any("01-phase1-mixed-edit-repair" in item_id for item_id in created_ids)
    assert any("05-phase1-acceptance-review" in item_id for item_id in created_ids)
    assert any("06-stage1-transition-generation" in item_id for item_id in created_ids)
    assert any("08-stage2-visual-regeneration" in item_id for item_id in created_ids)
    assert any("10-stage3-advanced-creative-generation" in item_id for item_id in created_ids)
    assert any("12-final-delivery" in item_id for item_id in created_ids)
    phase1_review = next(
        item for item in work_items if "05-phase1-acceptance-review" in item.work_item_id
    )
    stage1_generation = next(
        item for item in work_items if "06-stage1-transition-generation" in item.work_item_id
    )
    stage2_generation = next(
        item for item in work_items if "08-stage2-visual-regeneration" in item.work_item_id
    )
    stage3_generation = next(
        item for item in work_items if "10-stage3-advanced-creative-generation" in item.work_item_id
    )
    assert phase1_review.owner == "mission-director"
    assert stage1_generation.dependencies == [phase1_review.work_item_id]
    assert any(
        "07-stage1-retest-and-gate" in dependency for dependency in stage2_generation.dependencies
    )
    assert any(
        "09-stage2-retest-and-gate" in dependency for dependency in stage3_generation.dependencies
    )


def test_rainflow_acceptance_rework_work_items_preserve_isolation_metadata(tmp_path: Path) -> None:
    package = build_rainflow_ad_mission(root_dir=tmp_path)
    mission = package.mission
    contract = package.execution_contract
    gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-phase1-rejected",
        mission_id=mission.mission_id,
        task_plan_version=package.task_plan.version,
        subject_ref="acceptance-ticket-rainflow",
        stage="acceptance",
        task_type="product_development",
        rubric_version="rainflow-ad-quality-v1",
        metric_pack_version="rainflow-hook-cta-v1",
        north_star_verdict="partial",
        result_quality=0.68,
        speed=0.76,
        cost=0.81,
        risk=0.42,
        evidence_quality=0.66,
        collaboration_quality=0.73,
        hard_gate_failures=["hook_gap", "cta_gap"],
        failure_category="delivery_failure",
        root_cause="Mixed edit still lacks a strong hook and a convincing CTA.",
        responsibility_scope="kun_auto",
        confidence=0.84,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="rainflow_phase1_quality_below_bar",
        created_by="mission-director",
    )

    work_items = _acceptance_rework_work_items(
        mission=mission,
        gate=gate,
        plan_version=f"{package.task_plan.version}-acceptance-rework-abc12345",
        contract=contract,
    )

    expected_workspace_ref = contract.delivery_contract["workspace_ref"]
    expected_sandbox_ref = contract.delivery_contract["sandbox_ref"]
    expected_workspace_lock = f"workspace:{contract.delivery_contract['workspace_path']}"
    expected_output_lock = f"output_dir:{contract.delivery_contract['output_dir']}"
    for item in work_items:
        assert item.workspace_ref == expected_workspace_ref
        assert item.sandbox_ref == expected_sandbox_ref
        assert f"mission:{mission.mission_id}" in item.resource_locks
        assert expected_workspace_lock in item.resource_locks
        assert expected_output_lock in item.resource_locks
