from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane import (
    EXTERNAL_SUPERVISOR_GATE_OWNER,
    KUN_GAME_PRODUCTION_RUNNER_OWNER,
    ControlPlaneDaemon,
    ExecutionContract,
    FileControlPlaneStore,
    GameProductionCommandResult,
    GameProductionRunner,
    InMemoryControlPlane,
    Mission,
    QiRuntimeGovernanceRunner,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

NOW = datetime(2026, 5, 20, 10, 15, tzinfo=UTC)


def _mission(tmp_path: Path) -> tuple[InMemoryControlPlane, Path]:
    store = FileControlPlaneStore(tmp_path / "game-production-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "huohutu-playable"
    mission = Mission(
        mission_id="msn-game-production",
        owner="kun",
        objective="Continue Fire Rabbit game until directly playable internal-test build.",
        task_type="product_development",
        status="contracted",
        risk_level="high",
    )
    plan = TaskPlan(
        plan_id="plan-game-production",
        mission_id=mission.mission_id,
        version="playable-v1",
        objective=mission.objective,
        known_facts=["KUN must continue beyond MVP into internal-test-ready game."],
        acceptance_criteria=[
            "playable app builds",
            "internal playtest route exists",
            "parent report exists",
            "safety redirect exists",
        ],
        constraints=["supervisor must not write game delivery code"],
        approval_status="approved_with_limits",
    )
    contract = ExecutionContract(
        contract_id="contract-game-production",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "install_npm_dependencies", "run_build"],
        forbidden_actions=["manual_supervisor_delivery"],
        delivery_contract={"project_path": str(project_path)},
    )
    context = WorkingContext(
        working_context_id="ctx-game-production",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game production",
        summary="KUN production runner test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_items = [
        WorkItem(
            work_item_id="work-huohutu-v3-01-interaction-design",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            expected_output="interaction design",
        ),
        WorkItem(
            work_item_id="work-huohutu-v3-02-game-production",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-v3-01-interaction-design"],
            expected_output="game production",
        ),
        WorkItem(
            work_item_id="work-huohutu-v3-03-internal-test",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-v3-02-game-production"],
            expected_output="internal test",
        ),
        WorkItem(
            work_item_id="work-huohutu-v3-04-final-delivery",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="merge",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-v3-03-internal-test"],
            expected_output="final delivery",
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


def test_game_production_runner_creates_internal_test_ready_delivery(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    commands: list[list[str]] = []

    def fake_command(
        command: list[str],
        _cwd: Path,
        _timeout_sec: int,
    ) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(
        control_plane=control_plane,
        command_runner=fake_command,
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={KUN_GAME_PRODUCTION_RUNNER_OWNER: runner},
        daemon_id="game-production-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=8,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [
        "work-huohutu-v3-01-interaction-design",
        "work-huohutu-v3-02-game-production",
        "work-huohutu-v3-03-internal-test",
        "work-huohutu-v3-04-final-delivery",
    ]
    assert commands == [
        ["npm", "install"],
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
    ]
    assert (project_path / "src" / "App.tsx").exists()
    assert (project_path / "scripts" / "user-sim.mjs").exists()
    assert (project_path / "docs" / "final-playable-delivery.md").exists()
    assert control_plane.missions["msn-game-production"].status == "delivering"
    assert (
        "manifest-msn-game-production-playable-v1-playable-game"
        in control_plane.missions["msn-game-production"].artifact_manifest_refs
    )


def test_game_production_runner_owner_guard(tmp_path: Path) -> None:
    control_plane, _project_path = _mission(tmp_path)
    runner = GameProductionRunner(control_plane=control_plane)
    assigned = control_plane.work_items["work-huohutu-v3-01-interaction-design"]
    unassigned = assigned.model_copy(update={"owner": "codex-supervisor"})

    assert runner.can_run(assigned) is True
    assert runner.can_run(unassigned) is False


def test_game_production_runner_handles_scribble_benchmark_v2(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    mission = control_plane.missions["msn-game-production"]
    plan = control_plane.task_plans["plan-game-production"].model_copy(
        update={
            "version": "scribble-benchmark-v2",
            "acceptance_criteria": [
                "word-to-world parser exists",
                "two worlds each have three goals",
                "each goal has three systemic solution paths",
                "fun gate must run before final delivery",
            ],
        }
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "task_plan_version": "scribble-benchmark-v2",
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "scribble_spark_final_v2",
            },
        }
    )
    mission = mission.model_copy(
        update={
            "current_plan_version": "scribble-benchmark-v2",
            "execution_contract_ref": contract.contract_id,
            "status": "contracted",
        }
    )
    work_items = [
        WorkItem(
            work_item_id="work-huohutu-scribble-v2-03-system-redesign",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            expected_output="system redesign",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribble-v2-04-rebuild-word-to-world-game",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribble-v2-03-system-redesign"],
            expected_output="word-to-world game",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribble-v2-05-fun-and-browser-retest",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribble-v2-04-rebuild-word-to-world-game"],
            expected_output="fun and browser retest",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribble-v2-06-external-supervisor-gate",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=EXTERNAL_SUPERVISOR_GATE_OWNER,
            dependencies=["work-huohutu-scribble-v2-05-fun-and-browser-retest"],
            expected_output="external supervisor gate",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribble-v2-07-final-delivery-only-if-gated",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="merge",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribble-v2-06-external-supervisor-gate"],
            expected_output="final delivery",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=control_plane.working_contexts["ctx-game-production"].model_copy(
            update={"task_plan_version": plan.version}
        ),
        work_items=work_items,
    )
    for item_id in [
        "work-huohutu-v3-01-interaction-design",
        "work-huohutu-v3-02-game-production",
        "work-huohutu-v3-03-internal-test",
        "work-huohutu-v3-04-final-delivery",
    ]:
        control_plane.work_items[item_id] = control_plane.work_items[item_id].model_copy(
            update={"status": "done"}
        )
    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_PRODUCTION_RUNNER_OWNER: runner,
            EXTERNAL_SUPERVISOR_GATE_OWNER: runner,
        },
        daemon_id="scribble-v2-game-production-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=8,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [
        "work-huohutu-scribble-v2-03-system-redesign",
        "work-huohutu-scribble-v2-04-rebuild-word-to-world-game",
        "work-huohutu-scribble-v2-05-fun-and-browser-retest",
        "work-huohutu-scribble-v2-06-external-supervisor-gate",
        "work-huohutu-scribble-v2-07-final-delivery-only-if-gated",
    ]
    assert commands == [
        ["npm", "install"],
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
        ["npm", "run", "test:fun"],
        ["npm", "run", "test:browser-static"],
    ]
    assert (project_path / "src" / "engine" / "wordToWorld.ts").exists()
    assert (project_path / "src" / "engine" / "worldRules.ts").exists()
    assert (project_path / "scripts" / "fun-playtest.mjs").exists()
    assert (project_path / "docs" / "external-supervisor-gate.json").exists()
    assert (
        "manifest-msn-game-production-scribble-benchmark-v2-playable-game"
        in control_plane.missions["msn-game-production"].artifact_manifest_refs
    )


def test_game_production_runner_supports_v5_functional_parity_mode(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    mission = control_plane.missions["msn-game-production"]
    plan = control_plane.task_plans["plan-game-production"].model_copy(
        update={
            "version": "scribblenauts-functional-parity-v5",
            "acceptance_criteria": [
                "at least 120 object nouns",
                "at least 40 properties",
                "at least 12 actions",
                "at least 4 worlds",
                "at least 24 goals and 72 solution patterns",
                "object editing, combination, attachment, NPC requests, rewards, and open exploration must be playable",
            ],
        }
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "task_plan_version": plan.version,
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "scribble_spark_functional_parity_v5",
            },
        }
    )
    mission = mission.model_copy(
        update={
            "current_plan_version": plan.version,
            "execution_contract_ref": contract.contract_id,
            "status": "contracted",
        }
    )
    work_items = [
        WorkItem(
            work_item_id="work-huohutu-scribblenauts-v5-03-system-redesign",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            expected_output="expanded functional parity system redesign",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribblenauts-v5-04-rebuild-word-to-world-game",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribblenauts-v5-03-system-redesign"],
            expected_output="expanded word-to-world parity game",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribblenauts-v5-05-fun-and-browser-retest",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribblenauts-v5-04-rebuild-word-to-world-game"],
            expected_output="parity fun and user simulation tests",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribblenauts-v5-06-external-supervisor-gate",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=EXTERNAL_SUPERVISOR_GATE_OWNER,
            dependencies=["work-huohutu-scribblenauts-v5-05-fun-and-browser-retest"],
            expected_output="external supervisor parity gate",
        ),
        WorkItem(
            work_item_id="work-huohutu-scribblenauts-v5-07-final-delivery-only-if-gated",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="merge",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-huohutu-scribblenauts-v5-06-external-supervisor-gate"],
            expected_output="final parity delivery",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=control_plane.working_contexts["ctx-game-production"].model_copy(
            update={"task_plan_version": plan.version}
        ),
        work_items=work_items,
    )
    for item_id in [
        "work-huohutu-v3-01-interaction-design",
        "work-huohutu-v3-02-game-production",
        "work-huohutu-v3-03-internal-test",
        "work-huohutu-v3-04-final-delivery",
    ]:
        control_plane.work_items[item_id] = control_plane.work_items[item_id].model_copy(
            update={"status": "done"}
        )
    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_PRODUCTION_RUNNER_OWNER: runner,
            EXTERNAL_SUPERVISOR_GATE_OWNER: runner,
        },
        daemon_id="scribble-v5-parity-game-production-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=8,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [
        "work-huohutu-scribblenauts-v5-03-system-redesign",
        "work-huohutu-scribblenauts-v5-04-rebuild-word-to-world-game",
        "work-huohutu-scribblenauts-v5-05-fun-and-browser-retest",
        "work-huohutu-scribblenauts-v5-06-external-supervisor-gate",
        "work-huohutu-scribblenauts-v5-07-final-delivery-only-if-gated",
    ]
    assert commands == [
        ["npm", "install"],
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
        ["npm", "run", "test:fun"],
        ["npm", "run", "test:browser-static"],
    ]
    parity_report = json.loads(
        (project_path / "docs" / "parity-runtime-report.json").read_text(encoding="utf-8")
    )
    worlds_source = (project_path / "src" / "data" / "worlds.ts").read_text(encoding="utf-8")
    worlds_payload = json.loads(worlds_source.split("= ", 1)[1].rsplit(";", 1)[0])
    generated_rules = (project_path / "src" / "engine" / "worldRules.ts").read_text(encoding="utf-8")
    solution_actions = [
        action
        for world in worlds_payload.values()
        for goal in world["goals"]
        for solution in goal["solutions"]
        for action in solution["actions"]
    ]
    assert parity_report["objectCount"] >= 120
    assert parity_report["propertyCount"] >= 40
    assert parity_report["actionCount"] >= 12
    assert parity_report["worldCount"] >= 4
    assert parity_report["goalCount"] >= 24
    assert parity_report["solutionCount"] >= 72
    assert "create" not in solution_actions
    assert "nonCreateActionMatch" in generated_rules
    assert (project_path / "src" / "engine" / "wordToWorld.ts").exists()
    assert (project_path / "src" / "engine" / "worldRules.ts").exists()
    assert (project_path / "docs" / "external-supervisor-gate.json").exists()
    assert (
        "manifest-msn-game-production-scribblenauts-functional-parity-v5-playable-game"
        in control_plane.missions["msn-game-production"].artifact_manifest_refs
    )


def test_portless_gate_repair_only_installs_static_browser_gate(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    project_path.mkdir()
    (project_path / "scripts").mkdir()
    (project_path / "docs").mkdir()
    (project_path / "package.json").write_text(
        json.dumps(
            {
                "name": "existing-fire-rabbit-package",
                "scripts": {
                    "build": "tsc -b && vite build",
                    "test:fun": "node scripts/fun-playtest.mjs",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    mission = control_plane.missions["msn-game-production"].model_copy(
        update={"current_plan_version": "portless-browser-gate-repair"}
    )
    plan = control_plane.task_plans["plan-game-production"].model_copy(
        update={"version": "portless-browser-gate-repair"}
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "task_plan_version": plan.version,
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "scribble_spark_functional_parity_v5",
            },
        }
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    control_plane.contracts[contract.contract_id] = contract
    work_item = WorkItem(
        work_item_id="work-huohutu-scribblenauts-v5r3-01-portless-gate-repair",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        expected_output="install only the portless browser gate",
    )

    result = GameProductionRunner(control_plane=control_plane).run(work_item)

    assert result.status == "done"
    package_payload = json.loads((project_path / "package.json").read_text(encoding="utf-8"))
    assert (
        package_payload["scripts"]["test:browser-static"]
        == "node scripts/browser-static-playtest.mjs"
    )
    assert (project_path / "scripts" / "browser-static-playtest.mjs").exists()
    assert (project_path / "docs" / "portless-browser-gate-repair.md").exists()
    assert not (project_path / "src" / "engine" / "wordToWorld.ts").exists()


def test_game_production_runner_supports_original_scribble_adventure_mode(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    mission = control_plane.missions["msn-game-production"]
    plan = control_plane.task_plans["plan-game-production"].model_copy(
        update={
            "version": "wordforge-adventure-functional-parity-v1",
            "acceptance_criteria": [
                "original word-to-world adventure",
                "at least 120 object nouns",
                "at least 24 goals and 72 solution patterns",
                "no protected commercial game expression",
            ],
        }
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "task_plan_version": plan.version,
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "scribble_adventure_functional_parity_v1",
            },
        }
    )
    mission = mission.model_copy(
        update={
            "current_plan_version": plan.version,
            "execution_contract_ref": contract.contract_id,
            "status": "contracted",
        }
    )
    work_items = [
        WorkItem(
            work_item_id="work-wordforge-v1-03-system-redesign",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            expected_output="original functional parity system redesign",
        ),
        WorkItem(
            work_item_id="work-wordforge-v1-04-rebuild-word-to-world-game",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v1-03-system-redesign"],
            expected_output="original word-to-world game",
        ),
        WorkItem(
            work_item_id="work-wordforge-v1-045-visual-product-iteration",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v1-04-rebuild-word-to-world-game"],
            expected_output="original character, world, object, and tablet UI visuals",
        ),
        WorkItem(
            work_item_id="work-wordforge-v1-05-fun-and-browser-retest",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v1-045-visual-product-iteration"],
            expected_output="fun and user simulation tests",
        ),
        WorkItem(
            work_item_id="work-wordforge-v1-06-external-supervisor-gate",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=EXTERNAL_SUPERVISOR_GATE_OWNER,
            dependencies=["work-wordforge-v1-05-fun-and-browser-retest"],
            expected_output="external supervisor gate",
        ),
        WorkItem(
            work_item_id="work-wordforge-v1-07-final-delivery-only-if-gated",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="merge",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v1-06-external-supervisor-gate"],
            expected_output="final original delivery",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=control_plane.working_contexts["ctx-game-production"].model_copy(
            update={"task_plan_version": plan.version}
        ),
        work_items=work_items,
    )
    for item_id in [
        "work-huohutu-v3-01-interaction-design",
        "work-huohutu-v3-02-game-production",
        "work-huohutu-v3-03-internal-test",
        "work-huohutu-v3-04-final-delivery",
    ]:
        control_plane.work_items[item_id] = control_plane.work_items[item_id].model_copy(
            update={"status": "done"}
        )

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> GameProductionCommandResult:
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_PRODUCTION_RUNNER_OWNER: runner,
            EXTERNAL_SUPERVISOR_GATE_OWNER: runner,
        },
        daemon_id="wordforge-game-production-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=8,
    )

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [
        "work-wordforge-v1-03-system-redesign",
        "work-wordforge-v1-04-rebuild-word-to-world-game",
        "work-wordforge-v1-045-visual-product-iteration",
        "work-wordforge-v1-05-fun-and-browser-retest",
        "work-wordforge-v1-06-external-supervisor-gate",
        "work-wordforge-v1-07-final-delivery-only-if-gated",
    ]
    package_json = json.loads((project_path / "package.json").read_text(encoding="utf-8"))
    app = (project_path / "src" / "App.tsx").read_text(encoding="utf-8")
    readme = (project_path / "README.md").read_text(encoding="utf-8")
    assert package_json["name"] == "wordforge-adventure-functional-parity"
    assert "文字造物冒险" in app
    assert "火火兔" not in app
    assert "Maxwell" not in readme
    assert "Starite" not in readme
    assert (
        "manifest-msn-game-production-wordforge-adventure-functional-parity-v1-playable-game"
        in control_plane.missions["msn-game-production"].artifact_manifest_refs
    )


def test_game_production_runner_requires_residual_audit_before_final_stop(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    mission = control_plane.missions["msn-game-production"]
    plan = control_plane.task_plans["plan-game-production"].model_copy(
        update={
            "version": "wordforge-adventure-functional-parity-v2",
            "acceptance_criteria": [
                "functional parity remains original expression",
                "causal physics and player-facing why feedback exist",
                "long-playtest gate passes",
                "benchmark residual audit must pass before final delivery",
            ],
        }
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "task_plan_version": plan.version,
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "scribble_adventure_functional_parity_v1",
                "benchmark_residual_required": True,
                "benchmark_residual_threshold": 0.18,
            },
        }
    )
    mission = mission.model_copy(
        update={
            "current_plan_version": plan.version,
            "execution_contract_ref": contract.contract_id,
            "status": "contracted",
        }
    )
    work_items = [
        WorkItem(
            work_item_id="work-wordforge-v2-03-system-redesign",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            expected_output="system redesign",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-04-rebuild-word-to-world-game",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-03-system-redesign"],
            expected_output="original word-to-world game",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-045-benchmark-quality-iteration",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-04-rebuild-word-to-world-game"],
            expected_output="add causal physics, long-playtest, and richer feedback",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-046-visual-product-iteration",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="execution",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-045-benchmark-quality-iteration"],
            expected_output="add original character, world, object, and tablet UI visuals",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-05-fun-and-browser-retest",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="test",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-046-visual-product-iteration"],
            expected_output="fun, user simulation, and long playtest",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-06-external-supervisor-gate",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=EXTERNAL_SUPERVISOR_GATE_OWNER,
            dependencies=["work-wordforge-v2-05-fun-and-browser-retest"],
            expected_output="external supervisor gate",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-07-benchmark-residual-audit",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-06-external-supervisor-gate"],
            expected_output="benchmark residual audit",
        ),
        WorkItem(
            work_item_id="work-wordforge-v2-08-final-delivery-only-if-gated",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="merge",
            owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
            dependencies=["work-wordforge-v2-07-benchmark-residual-audit"],
            expected_output="final delivery only if residual audit passes",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=control_plane.working_contexts["ctx-game-production"].model_copy(
            update={"task_plan_version": plan.version}
        ),
        work_items=work_items,
    )
    for item_id in [
        "work-huohutu-v3-01-interaction-design",
        "work-huohutu-v3-02-game-production",
        "work-huohutu-v3-03-internal-test",
        "work-huohutu-v3-04-final-delivery",
    ]:
        control_plane.work_items[item_id] = control_plane.work_items[item_id].model_copy(
            update={"status": "done"}
        )
    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_PRODUCTION_RUNNER_OWNER: runner,
            EXTERNAL_SUPERVISOR_GATE_OWNER: runner,
            "qi": QiRuntimeGovernanceRunner(control_plane=control_plane),
        },
        daemon_id="wordforge-residual-audit-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=20,
    )

    assert report.no_runner_work_item_ids == []
    core_run_ids = [
        "work-wordforge-v2-03-system-redesign",
        "work-wordforge-v2-04-rebuild-word-to-world-game",
        "work-wordforge-v2-045-benchmark-quality-iteration",
        "work-wordforge-v2-046-visual-product-iteration",
        "work-wordforge-v2-05-fun-and-browser-retest",
        "work-wordforge-v2-06-external-supervisor-gate",
        "work-wordforge-v2-07-benchmark-residual-audit",
        "work-wordforge-v2-08-final-delivery-only-if-gated",
    ]
    assert [item_id for item_id in report.ran_work_item_ids if item_id in core_run_ids] == core_run_ids
    assert commands == [
        ["npm", "install"],
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
        ["npm", "run", "test:fun"],
        ["npm", "run", "test:browser-static"],
        ["npm", "run", "test:long"],
        ["npm", "run", "test:visual"],
        ["npm", "run", "test:experience"],
        ["npm", "run", "test:sandbox"],
        ["npm", "run", "test:creative"],
        ["npm", "run", "test:mastery"],
        ["npm", "run", "test:semantic"],
        ["npm", "run", "test:spatial"],
    ]
    residual = json.loads(
        (project_path / "docs" / "benchmark-residual-audit.json").read_text(encoding="utf-8")
    )
    assert residual["pass"] is True
    assert residual["overall_residual"] <= 0.18
    assert (project_path / "src" / "engine" / "causalPhysics.ts").exists()
    assert "因果实验室" in (project_path / "src" / "App.tsx").read_text(encoding="utf-8")
    assert (
        "manifest-msn-game-production-wordforge-adventure-functional-parity-v2-playable-game"
        in control_plane.missions["msn-game-production"].artifact_manifest_refs
    )
    manifest = control_plane.artifact_manifests[
        "manifest-msn-game-production-wordforge-adventure-functional-parity-v2-playable-game"
    ]
    assert "artifact-work-wordforge-v2-05-fun-and-browser-retest-internal-test-result" in (
        manifest.test_refs
    )
    assert "artifact-work-wordforge-v2-06-external-supervisor-gate-external-supervisor-gate" in (
        manifest.evidence_refs
    )
    assert (
        "artifact-work-wordforge-v2-07-benchmark-residual-audit-benchmark-residual-audit"
        in manifest.evidence_refs
    )
