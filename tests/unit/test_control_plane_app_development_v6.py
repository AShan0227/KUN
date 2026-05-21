from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane import (
    KUN_AUTONOMOUS_APP_RUNNER_OWNER,
    AppCommandResult,
    AutonomousAppDevelopmentRunner,
    ControlPlaneDaemon,
    ExecutionContract,
    FileControlPlaneStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)
from kun.control_plane.app_development import _command_env, _resolve_command

NOW = datetime(2026, 5, 20, 1, 0, tzinfo=UTC)


def _mission(tmp_path: Path) -> tuple[InMemoryControlPlane, Path]:
    store = FileControlPlaneStore(tmp_path / "app-dev-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "huohutu-spark-mvp"
    mission = Mission(
        mission_id="msn-app-dev",
        owner="kun",
        objective="Build a playable tablet-first children AI world MVP",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-app-dev",
        mission_id=mission.mission_id,
        version="mvp-v1",
        objective=mission.objective,
        known_facts=["KUN must autonomously create the MVP app project."],
        acceptance_criteria=[
            "project can be built",
            "two worlds exist",
            "mock AI and Spark telemetry exist",
        ],
        constraints=["supervisor must not create game files"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-app-dev",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["create_new_local_project", "install_npm_dependencies"],
        forbidden_actions=["manual_supervisor_delivery"],
        delivery_contract={"project_path": str(project_path), "delivery_type": "playable_mvp"},
    )
    context = WorkingContext(
        working_context_id="ctx-app-dev",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-autonomous-app-runner",
        scope="app-dev-test",
        summary="Test app development runner context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_items = [
        WorkItem(
            work_item_id="work-huohutu-00-kun-autonomous-runner-activation",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="governance",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            expected_output="runner activation",
        ),
        WorkItem(
            work_item_id="work-huohutu-01-mvp-scope-worlds",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=["work-huohutu-00-kun-autonomous-runner-activation"],
            expected_output="scope",
        ),
        WorkItem(
            work_item_id="work-huohutu-02-app-scaffold",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=["work-huohutu-01-mvp-scope-worlds"],
            expected_output="scaffold",
        ),
        WorkItem(
            work_item_id="work-huohutu-03-core-game-loop",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=["work-huohutu-02-app-scaffold"],
            expected_output="core",
        ),
        WorkItem(
            work_item_id="work-huohutu-04-ui-safety-report",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=["work-huohutu-03-core-game-loop"],
            expected_output="ui",
        ),
        WorkItem(
            work_item_id="work-huohutu-05-qa-delivery",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
            dependencies=["work-huohutu-04-ui-safety-report"],
            expected_output="qa",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=work_items,
    )
    return control_plane, project_path


def test_autonomous_app_runner_materializes_playable_project_and_delivery_gate(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> AppCommandResult:
        commands.append(command)
        return AppCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = AutonomousAppDevelopmentRunner(
        control_plane=control_plane,
        command_runner=fake_command,
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={KUN_AUTONOMOUS_APP_RUNNER_OWNER: runner},
        daemon_id="app-dev-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-app-dev"],
        now=NOW,
        max_work_items=10,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [
        "work-huohutu-00-kun-autonomous-runner-activation",
        "work-huohutu-01-mvp-scope-worlds",
        "work-huohutu-02-app-scaffold",
        "work-huohutu-03-core-game-loop",
        "work-huohutu-04-ui-safety-report",
        "work-huohutu-05-qa-delivery",
    ]
    assert commands == [["npm", "install"], ["npm", "run", "build"]]
    assert (project_path / "src" / "App.tsx").exists()
    assert (project_path / "src" / "engine" / "mockAi.ts").exists()
    assert (project_path / "docs" / "delivery-report.md").exists()
    assert control_plane.missions["msn-app-dev"].status == "delivering"
    assert (
        "manifest-msn-app-dev-mvp-v1-huohutu-mvp-delivery"
        in control_plane.missions["msn-app-dev"].artifact_manifest_refs
    )
    assert control_plane.artifact_manifests[
        "manifest-msn-app-dev-mvp-v1-huohutu-mvp-delivery"
    ].supports_delivery


def test_autonomous_app_runner_can_run_only_assigned_product_work(tmp_path: Path) -> None:
    control_plane, _project_path = _mission(tmp_path)
    runner = AutonomousAppDevelopmentRunner(control_plane=control_plane)
    assigned = control_plane.work_items["work-huohutu-02-app-scaffold"]
    unassigned = assigned.model_copy(update={"owner": "codex-worker"})

    assert runner.can_run(assigned) is True
    assert runner.can_run(unassigned) is False


def test_autonomous_app_runner_qa_uses_daemon_safe_npm_path(tmp_path: Path) -> None:
    env = _command_env(tmp_path)
    resolved = _resolve_command(["npm", "--version"], env)

    assert ".kun-local/npm-tool/bin" in env["PATH"]
    assert resolved[0].endswith("/npm")
    assert env["npm_config_cache"] == str(tmp_path / ".npm-cache")
