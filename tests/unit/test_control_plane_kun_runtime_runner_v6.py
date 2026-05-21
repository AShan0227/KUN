from __future__ import annotations

from datetime import UTC, datetime

from kun.control_plane import (
    ArtifactRecord,
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

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == ["work-real-task"]
    assert report.finalized_mission_ids == [mission.mission_id]
    assert report.delivery_manifest_refs == ["manifest-kun-runtime-delivery-msn-real-task"]
    assert control_plane.work_items["work-real-task"].status == "done"
    assert control_plane.missions[mission.mission_id].status == "delivering"
    assert any(
        "real_task_execution" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )


def test_kun_runtime_runner_handles_strategy_optimization_without_external_executor() -> None:
    control_plane, mission = _runtime()
    work = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-plan-change-quality-gate",
            "type": "research",
            "expected_output": (
                "Create or revise a stricter continuation plan from the failed quality gate. "
                "Require clean retest evidence before delivery can close."
            ),
            "recovery_refs": ["gate-low-quality"],
        }
    )
    control_plane.work_items = {work.work_item_id: work}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    def failing_executor(_prompt: str) -> KunTaskExecutionOutput:
        raise AssertionError("strategy optimization should not need the external executor")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=failing_executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-strategy-optimization-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [work.work_item_id]
    assert control_plane.work_items[work.work_item_id].status == "done"
    assert any(
        "strategy_optimization_plan" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    gate = control_plane.gate_evaluations[
        "gate-kun-strategy-optimization-work-kun-plan-change-quality-gate"
    ]
    assert gate.next_action == "needs_plan_change"
    assert gate.next_state == "changing_plan"
    assert {
        "work-kun-implement-after-work-kun-plan-change-quality-gate",
        "work-kun-retest-after-work-kun-plan-change-quality-gate",
        "work-kun-merge-after-work-kun-plan-change-quality-gate",
    }.issubset(control_plane.work_items)


def test_kun_runtime_runner_does_not_recurse_strategy_followups() -> None:
    control_plane, mission = _runtime()
    followup = control_plane.work_items["work-real-task"].model_copy(
        update={
            "work_item_id": "work-kun-implement-after-work-kun-plan-change-quality-gate",
            "type": "execution",
            "expected_output": (
                "Implement the stricter continuation plan and record concrete changed artifacts."
            ),
            "dependencies": [],
        }
    )
    control_plane.work_items = {followup.work_item_id: followup}
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "changing_plan"})

    def executor(_prompt: str) -> KunTaskExecutionOutput:
        return KunTaskExecutionOutput(status="done", answer="implemented")

    runner = KunRuntimeTaskRunner(control_plane=control_plane, executor=executor)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-strategy-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=3)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert all(
        not work_id.startswith("work-kun-implement-after-work-kun-implement-after")
        for work_id in control_plane.work_items
    )


def test_kun_runtime_merge_blocks_overlapping_artifact_outputs() -> None:
    control_plane, mission = _runtime()
    first = WorkItem(
        work_item_id="work-upstream-a",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
    )
    second = first.model_copy(update={"work_item_id": "work-upstream-b"})
    merge = WorkItem(
        work_item_id="work-merge-conflict",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="merge",
        owner="kun",
        dependencies=[first.work_item_id, second.work_item_id],
        expected_output="Merge upstream implementation artifacts.",
    )
    control_plane.work_items = {
        first.work_item_id: first,
        second.work_item_id: second,
        merge.work_item_id: merge,
    }
    for item in (first, second):
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{item.work_item_id}",
            kind="diff",
            path_or_uri="repo://src/app.ts",
            content_hash=f"hash-{item.work_item_id}",
            created_by=item.work_item_id,
            mission_id=mission.mission_id,
            work_item_id=item.work_item_id,
            supports=["writes:src/app.ts"],
            source_quality="primary",
        )
        control_plane.artifacts[artifact.artifact_id] = artifact
    runner = KunRuntimeTaskRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-merge-conflict-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [merge.work_item_id]
    gate = control_plane.gate_evaluations["gate-kun-merge-work-merge-conflict"]
    assert gate.north_star_verdict == "fail"
    assert "overlapping_artifact_output" in gate.hard_gate_failures
    assert control_plane.work_items[merge.work_item_id].status == "failed"
