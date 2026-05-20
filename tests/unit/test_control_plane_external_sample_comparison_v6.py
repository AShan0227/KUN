from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane import (
    KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER,
    ControlPlaneDaemon,
    ExecutionContract,
    ExternalSampleComparisonRunner,
    FileControlPlaneStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

NOW = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)


def _mission(tmp_path: Path) -> tuple[InMemoryControlPlane, Path]:
    source = tmp_path / "genesis"
    target = tmp_path / "kun"
    output = tmp_path / "comparison"
    (source / "docs").mkdir(parents=True)
    (target / "docs").mkdir(parents=True)
    (source / "docs" / "architecture.md").write_text(
        "# Genesis\n\nGateway sessions tools.\n\nHall of Fame.\n\nConsult injection.",
        encoding="utf-8",
    )
    (target / "docs" / "kun.md").write_text(
        "# KUN\n\nGateway sessions tools are handled through Control Plane.",
        encoding="utf-8",
    )
    store = FileControlPlaneStore(tmp_path / "external-sample-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-genesis-compare",
        owner="kun",
        objective="Compare Genesis with KUN and identify Ockham-safe capability gaps.",
        task_type="self_improvement",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-genesis-compare",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["gap matrix exists", "Ockham recommendations exist"],
        constraints=["do not copy external code"],
        evidence_plan=["source inventory", "gap matrix", "recommendations"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-genesis-compare",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read_local_repositories", "write_comparison_artifacts"],
        forbidden_actions=["copy_external_implementation_code"],
        evidence_policy={
            "external_sample_comparison": {
                "source_name": "Genesis",
                "source_repo_path": str(source),
                "target_repo_path": str(target),
                "output_dir": str(output),
                "max_source_files": 20,
                "max_target_files": 20,
            }
        },
    )
    context = WorkingContext(
        working_context_id="ctx-genesis-compare",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun",
        scope="external sample comparison",
        summary="Compare external sample against KUN.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_item = WorkItem(
        work_item_id="work-genesis-capability-comparison",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner=KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER,
        expected_output="Genesis vs KUN capability gap report",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    return control_plane, output


def test_external_sample_runner_compares_sample_and_writes_ockham_artifacts(
    tmp_path: Path,
) -> None:
    control_plane, output = _mission(tmp_path)
    runner = ExternalSampleComparisonRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER: runner},
        daemon_id="external-sample-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-genesis-compare"],
        now=NOW,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == ["work-genesis-capability-comparison"]
    assert (output / "source-inventory.json").exists()
    assert (output / "feature-gap-matrix.md").exists()
    assert (output / "ockham-recommendations.md").exists()
    assert (output / "qi-governance-actions.json").exists()
    inventory = json.loads((output / "source-inventory.json").read_text(encoding="utf-8"))
    assert inventory["source_signal_count"] >= 1
    assert inventory["governance_action_count"] >= 1
    recommendations = (output / "ockham-recommendations.md").read_text(encoding="utf-8")
    assert "Qi Candidate" in recommendations
    governance = json.loads((output / "qi-governance-actions.json").read_text(encoding="utf-8"))
    assert governance["schema"] == "kun-external-sample-governance-plan-v1"
    assert governance["default_runtime_allowed"] is False
    assert governance["actions"]
    assert all(action["default_runtime_allowed"] is False for action in governance["actions"])
    assert any(
        "do not copy external implementation code" in action["risk_controls"]
        for action in governance["actions"]
    )
    assert control_plane.work_items["work-genesis-capability-comparison"].status == "done"
    assert any("external_sample_gap_matrix" in artifact.supports for artifact in control_plane.artifacts.values())
    assert any(
        "external_sample_governance_plan" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
