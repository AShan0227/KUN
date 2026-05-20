from __future__ import annotations

from datetime import UTC, datetime

from kun.control_plane import (
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

    def fake_executor(prompt: str) -> KunTaskExecutionOutput:
        assert "Deliver a traceable real task result" in prompt
        assert "Produce a concrete audited result" in prompt
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

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)

    assert report.ran_work_item_ids == ["work-real-task"]
    assert report.finalized_mission_ids == [mission.mission_id]
    assert report.delivery_manifest_refs == ["manifest-kun-runtime-delivery-msn-real-task"]
    assert control_plane.work_items["work-real-task"].status == "done"
    assert control_plane.missions[mission.mission_id].status == "delivering"
    assert any(
        "real_task_execution" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
