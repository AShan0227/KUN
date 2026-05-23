from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import kun.control_plane.game_production as game_production_module
from kun.control_plane import (
    EXTERNAL_SUPERVISOR_GATE_OWNER,
    KUN_GAME_PRODUCTION_RUNNER_OWNER,
    ArtifactRecord,
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
from kun.control_plane.game_production import (
    GameProductionSpec,
    _final_delivery_markdown,
    _final_player_experience_payload,
)

NOW = datetime(2026, 5, 20, 10, 15, tzinfo=UTC)


def _activate_project_boundary(
    control_plane: InMemoryControlPlane,
    *,
    work_item_id: str,
    project_path: Path,
) -> None:
    work_item = control_plane.work_items[work_item_id]
    control_plane.work_items[work_item_id] = work_item.model_copy(
        update={
            "workspace_ref": f"workspace://{project_path}",
            "sandbox_ref": f"sandbox://{work_item.mission_id}/{work_item.work_item_id}",
            "resource_locks": [f"workspace:{project_path}"],
        }
    )


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
        delivery_contract={
            "project_path": str(project_path),
            "production_mode": "gameful_playtest",
            "app_name": "火火兔 Spark",
        },
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
    manifest = control_plane.artifact_manifests[
        "manifest-msn-game-production-playable-v1-playable-game"
    ]
    assert manifest.review_refs
    assert manifest.rollback_refs
    assert all("final-delivery-review" not in ref for ref in manifest.review_refs)
    assert any("internal-test-review" in ref for ref in manifest.review_refs)
    delivery_artifact = control_plane.artifacts[manifest.primary_artifact_ref]
    assert "visual_product_iteration" not in delivery_artifact.supports
    assert "final_player_experience_gate" not in delivery_artifact.supports
    trace_refs = [ref for ref in manifest.evidence_refs if "delivery-evidence-trace" in ref]
    assert trace_refs
    trace_artifact = control_plane.artifacts[trace_refs[0]]
    assert "delivery_evidence_trace" in trace_artifact.supports
    assert "internal_test_passed" in trace_artifact.supports
    assert (project_path / "docs" / "final-delivery-review.md").exists()
    assert (project_path / "docs" / "final-delivery-rollback-plan.md").exists()


def test_final_delivery_blocks_without_real_internal_test_artifact(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-04-final-delivery",
        project_path=project_path,
    )
    work_item = control_plane.work_items["work-huohutu-v3-04-final-delivery"]
    runner = GameProductionRunner(control_plane=control_plane)

    result = runner.run(work_item)

    assert result.status == "blocked"
    assert result.failure_category == "evidence_failure"
    assert "no real internal-test/playability gate artifact" in result.summary


def test_game_production_runner_blocks_missing_required_test_scripts(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    project_path.mkdir()
    (project_path / "docs").mkdir()
    (project_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build", "test:internal": "node internal.mjs"}}),
        encoding="utf-8",
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "gameful_playtest",
                "required_test_scripts": ["test:visual"],
            }
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-03-internal-test",
        project_path=project_path,
    )
    work_item = control_plane.work_items["work-huohutu-v3-03-internal-test"]
    runner = GameProductionRunner(control_plane=control_plane)

    result = runner.run(work_item)

    assert result.status == "failed"
    assert result.failure_category == "tool_failure"
    assert "Missing required package scripts" in (
        project_path / "docs" / "work-huohutu-v3-03-internal-test-failure.json"
    ).read_text(encoding="utf-8")


def test_game_production_runner_prefers_explicit_work_item_phase(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    project_path.mkdir()
    (project_path / "docs").mkdir()
    (project_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                    "test:internal": "node internal.mjs",
                    "test:user-sim": "node user-sim.mjs",
                }
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    work_item = control_plane.work_items["work-huohutu-v3-03-internal-test"].model_copy(
        update={"work_item_id": "work-custom-phase", "phase": "internal-test"}
    )
    control_plane.work_items = {work_item.work_item_id: work_item}
    _activate_project_boundary(
        control_plane,
        work_item_id=work_item.work_item_id,
        project_path=project_path,
    )
    work_item = control_plane.work_items[work_item.work_item_id]
    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)

    result = runner.run(work_item)

    assert result.status == "done"
    assert commands == [
        ["npm", "install"],
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
    ]


def test_game_production_runner_normalizes_supervisor_plan_phase_aliases(
    tmp_path: Path,
) -> None:
    control_plane, _project_path = _mission(tmp_path)
    base = control_plane.work_items["work-huohutu-v3-03-internal-test"]

    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(update={"phase": "commercial_first_screen_rebuild"})
        )
        == "commercial-game-polish-iteration"
    )
    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(update={"phase": "image_object_causal_interaction"})
        )
        == "image-object-interaction-iteration"
    )
    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(update={"phase": "browser_long_player_simulation"})
        )
        == "internal-test"
    )
    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(update={"phase": "external_final_player_feel_gate"})
        )
        == "supervisor-gate"
    )
    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(update={"phase": "delivery_only_after_human_grade_gate"})
        )
        == "final-delivery"
    )
    assert (
        game_production_module._phase_from_work_item(
            base.model_copy(
                update={"phase": None, "work_item_id": "work-benchmark-understanding-review"}
            )
        )
        == "benchmark-understanding-review"
    )


def test_game_production_runner_installs_commercial_game_polish_iteration(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    (project_path / "src" / "data").mkdir(parents=True)
    (project_path / "public" / "assets").mkdir(parents=True)
    (project_path / "docs").mkdir(parents=True)
    (project_path / "package.json").write_text(
        json.dumps({"scripts": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project_path / "src" / "App.tsx").write_text(
        """
export function App() {
  return (
    <main className="tabletWorkbench visual-polish-ready">
      <section className="stagePanel visualStage immersiveStage">
        <div className="stageScene illustratedScene visualFocusLayer sandboxDynamics imageObjectPlayfield">
          <p className="touchHint">拖到舞台，或点动作导演。</p>
          <img className="stageCompanionAvatar" src={activeVisual.companionPortrait} alt={`${activeWorld.companion} 正在舞台里等待孩子造物`} />
        </div>
      </section>
    </main>
  );
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (project_path / "src" / "data" / "visuals.ts").write_text(
        """
export const visualWorlds = [{
  companionPortrait: "/assets/companion-xiaobi.svg",
}];
export function generatedObjectImage(object: GeneratedObject): string {
  if (isFoodObject(object)) return generatedObjectTemplates.burger;
  if (isWolfObject(object)) return generatedObjectTemplates.wolf;
  return objectAsset(object);
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (project_path / "src" / "styles.css").write_text(
        ".stageCompanionAvatar{width:72px;height:72px}\n",
        encoding="utf-8",
    )
    work_item = WorkItem(
        work_item_id="work-wordforge-v29-05-commercial-game-polish-iteration",
        mission_id="msn-game-production",
        task_plan_version="playable-v1",
        type="execution",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        expected_output="commercial polish iteration",
    )
    control_plane.work_items[work_item.work_item_id] = work_item
    _activate_project_boundary(
        control_plane,
        work_item_id=work_item.work_item_id,
        project_path=project_path,
    )
    work_item = control_plane.work_items[work_item.work_item_id]

    result = GameProductionRunner(control_plane=control_plane).run(work_item)

    assert result.status == "done"
    assert (project_path / "public" / "assets" / "companion-spark-star.svg").exists()
    assert (project_path / "public" / "assets" / "object-burger.svg").exists()
    assert (project_path / "public" / "assets" / "object-wolf.svg").exists()
    app = (project_path / "src" / "App.tsx").read_text(encoding="utf-8")
    visuals = (project_path / "src" / "data" / "visuals.ts").read_text(encoding="utf-8")
    styles = (project_path / "src" / "styles.css").read_text(encoding="utf-8")
    package_json = json.loads((project_path / "package.json").read_text(encoding="utf-8"))
    assert "commercial-game-polish-ready" in app
    assert "commercialGamePolish" in app
    assert "premiumTouchStage" in app
    assert "referenceCharacterMotion" in app
    assert "referenceCharacterBadge" in app
    assert "companion-spark-star.svg" in visuals
    assert "object-burger.svg" in visuals
    assert "object-wolf.svg" in visuals
    assert ".commercialGamePolish" in styles
    assert "@keyframes starHop" in styles
    assert package_json["scripts"]["test:commercial"] == "node scripts/commercial-product-test.mjs"
    assert (project_path / "docs" / "character-reference-video" / "reference-notes.md").exists()


def test_game_production_write_text_recovers_existing_read_only_file(tmp_path: Path) -> None:
    target = tmp_path / "public" / "assets" / "companion-spark-star.svg"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    target.chmod(0o400)

    try:
        game_production_module._write_text(target, "new")
    finally:
        target.chmod(0o600)

    assert target.read_text(encoding="utf-8") == "new"


def test_final_delivery_requires_player_experience_gate_when_contract_demands_it(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "gameful_playtest",
                "app_name": "火火兔 Spark",
                "final_player_experience_required": True,
                "final_player_experience_threshold": 0.95,
            },
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-04-final-delivery",
        project_path=project_path,
    )
    work_item = control_plane.work_items["work-huohutu-v3-04-final-delivery"]
    runner = GameProductionRunner(control_plane=control_plane)

    result = runner.run(work_item)

    assert result.status == "blocked"
    assert result.failure_category == "delivery_failure"
    assert "KUN self scores" in result.summary
    assert "product-feel evidence" in result.summary


def test_final_delivery_requires_control_plane_player_experience_artifact(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "gameful_playtest",
                "app_name": "火火兔 Spark",
                "final_player_experience_required": True,
                "final_player_experience_threshold": 0.75,
            },
        }
    )
    control_plane.contracts[contract.contract_id] = contract

    def fake_command(
        _command: list[str],
        _cwd: Path,
        _timeout_sec: int,
    ) -> GameProductionCommandResult:
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={KUN_GAME_PRODUCTION_RUNNER_OWNER: runner},
        daemon_id="game-production-daemon-test",
    )
    daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=3,
    )
    (project_path / "docs" / "final-player-experience-gate.json").write_text(
        json.dumps({"score": 0.9, "threshold": 0.75, "pass": True}),
        encoding="utf-8",
    )
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-04-final-delivery",
        project_path=project_path,
    )

    result = runner.run(control_plane.work_items["work-huohutu-v3-04-final-delivery"])

    assert result.status == "blocked"
    assert result.failure_category == "evidence_failure"
    assert "Control Plane review artifact" in result.summary


def test_final_delivery_requires_browser_interaction_evidence_for_player_experience(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    docs_path = project_path / "docs"
    docs_path.mkdir(parents=True)
    (docs_path / "final-player-experience-gate.json").write_text(
        json.dumps({"score": 0.92, "threshold": 0.75, "pass": True}),
        encoding="utf-8",
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "gameful_playtest",
                "app_name": "火火兔 Spark",
                "final_player_experience_required": True,
                "final_player_experience_threshold": 0.75,
            },
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    test_work_id = "work-huohutu-v3-03-internal-test"
    artifact_defs = [
        (
            "artifact-internal-test",
            "test_result",
            ["internal_test_passed", "playability_gate"],
        ),
        (
            "artifact-internal-review",
            "review",
            ["automated_test_review", "playability_gate_review"],
        ),
        (
            "artifact-final-player-experience",
            "review",
            ["final_player_experience_gate", "not_self_score_only"],
        ),
    ]
    for artifact_id, kind, supports in artifact_defs:
        control_plane.artifacts[artifact_id] = ArtifactRecord(
            artifact_id=artifact_id,
            kind=kind,
            path_or_uri=f"mem://{artifact_id}",
            content_hash=f"hash-{artifact_id}",
            created_by="test",
            mission_id="msn-game-production",
            work_item_id=test_work_id,
            supports=supports,
        )
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-04-final-delivery",
        project_path=project_path,
    )
    runner = GameProductionRunner(control_plane=control_plane)

    result = runner.run(control_plane.work_items["work-huohutu-v3-04-final-delivery"])

    assert result.status == "blocked"
    assert result.failure_category == "evidence_failure"
    assert "browser interaction evidence" in result.summary


def test_final_delivery_blocks_when_visual_evidence_is_contract_required(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)

    def fake_command(
        _command: list[str],
        _cwd: Path,
        _timeout_sec: int,
    ) -> GameProductionCommandResult:
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={KUN_GAME_PRODUCTION_RUNNER_OWNER: runner},
        daemon_id="game-production-daemon-test",
    )
    daemon.tick_once(
        mission_ids=["msn-game-production"],
        now=NOW,
        max_work_items=3,
    )
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
                "production_mode": "gameful_playtest",
                "app_name": "火火兔 Spark",
                "visual_product_iteration_required": True,
            },
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-04-final-delivery",
        project_path=project_path,
    )

    result = runner.run(control_plane.work_items["work-huohutu-v3-04-final-delivery"])

    assert result.status == "blocked"
    assert result.failure_category == "evidence_failure"
    assert "requires visual product iteration evidence" in result.summary


def test_final_delivery_report_uses_latest_natural_browser_evidence(tmp_path: Path) -> None:
    project_path = tmp_path / "wordforge"
    docs_path = project_path / "docs"
    docs_path.mkdir(parents=True)
    (docs_path / "browser-playtest-v9-spatial-playfield.json").write_text("{}", encoding="utf-8")
    (docs_path / "browser-live-interaction-v26.json").write_text("{}", encoding="utf-8")
    (docs_path / "browser-static-playtest.json").write_text("{}", encoding="utf-8")
    (docs_path / "benchmark-residual-audit.json").write_text(
        json.dumps({"overall_residual": 0.0023, "threshold": 0.003, "pass": True}),
        encoding="utf-8",
    )
    (docs_path / "final-player-experience-gate.json").write_text(
        json.dumps({"score": 0.9928, "threshold": 0.98, "pass": True}),
        encoding="utf-8",
    )
    plan = TaskPlan(
        mission_id="msn-game-production",
        version="wordforge-v26",
        objective="deliver",
        acceptance_criteria=["fresh browser evidence is cited"],
        approval_status="approved",
    )
    spec = GameProductionSpec(
        project_path=project_path,
        app_name="Wordforge",
        production_mode="scribble_adventure_functional_parity_v1",
        benchmark_residual_required=True,
        benchmark_residual_threshold=0.003,
        final_player_experience_required=True,
        final_player_experience_threshold=0.98,
    )

    report = _final_delivery_markdown(task_plan=plan, spec=spec)

    assert "docs/browser-live-interaction-v26.json" in report
    assert "docs/browser-playtest-v9-spatial-playfield.json" not in report
    assert "--port 5178" in report
    assert "--port 5179" not in report


def test_final_player_experience_gate_rejects_dashboard_shell_even_with_checklist_markers(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "wordforge"
    (project_path / "src" / "engine").mkdir(parents=True)
    (project_path / "docs").mkdir()
    (project_path / "public" / "assets").mkdir(parents=True)
    for index in range(18):
        (project_path / "public" / "assets" / f"asset-{index}.svg").write_text(
            "<svg/>",
            encoding="utf-8",
        )
    (project_path / "src" / "App.tsx").write_text(
        "tabletWorkbench playGrid toolPanel visual-polish-ready companionPortrait "
        "worldBackdrop objectSprite generatedObjectImage generatedSprite questDeck directManipulation objectRelationGraph "
        "sandboxDynamics playerQuestLab customQuestLog masteryCelebration failureCoach "
        "tryNextHint stagePhysicsOverlay objectStageStyle objectTrajectoryLabel rewardShards",
        encoding="utf-8",
    )
    (project_path / "src" / "styles.css").write_text(
        "grid-template-columns:250px minmax(460px,1fr) 330px",
        encoding="utf-8",
    )
    (project_path / "src" / "engine" / "wordToWorld.ts").write_text(
        "semanticFallbackEntry normalizedCreativeName semanticRuleHeuristics",
        encoding="utf-8",
    )
    for name in [
        "browser-static-playtest.json",
        "long-playtest-result.json",
        "visual-product-test-result.json",
        "experience-product-test-result.json",
        "sandbox-product-test-result.json",
        "creative-product-test-result.json",
        "mastery-product-test-result.json",
        "semantic-synthesis-test-result.json",
        "spatial-product-test-result.json",
        "original-asset-provenance.json",
    ]:
        (project_path / "docs" / name).write_text('{"ok": true}\n', encoding="utf-8")
    (project_path / "docs" / "internal-test-result.json").write_text(
        json.dumps(
            {
                "npm_run_test_fun": {"exit_code": 0},
                "npm_run_test_browser_static": {"exit_code": 0},
                "npm_run_test_long": {"exit_code": 0},
            }
        ),
        encoding="utf-8",
    )

    payload = _final_player_experience_payload(
        GameProductionSpec(
            project_path=project_path,
            production_mode="scribble_adventure_functional_parity_v1",
            final_player_experience_required=True,
            final_player_experience_threshold=0.98,
        )
    )

    assert payload["schema"] == "kun-final-player-experience-gate-v2"
    assert payload["checks"]["visual_character_world_density"] is True
    assert payload["dimensions"]["immersive_game_stage"]["score"] < payload["dimension_floor"]
    assert "dimension:immersive_game_stage" in payload["failures"]
    assert payload["pass"] is False


def test_final_player_experience_gate_requires_viewport_safe_initial_living_stage(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "wordforge"
    (project_path / "src" / "engine").mkdir(parents=True)
    (project_path / "docs").mkdir()
    (project_path / "public" / "assets").mkdir(parents=True)
    for index in range(18):
        (project_path / "public" / "assets" / f"asset-{index}.svg").write_text(
            "<svg/>",
            encoding="utf-8",
        )
    (project_path / "src" / "App.tsx").write_text(
        "immersiveGameShell immersiveStage floatingQuestRail floatingCreatorTray "
        "visualFocusLayer stageCompanionAvatar starterObjectPreview visual-polish-ready "
        "companionPortrait worldBackdrop objectSprite generatedObjectImage generatedSprite questDeck directManipulation "
        "objectRelationGraph sandboxDynamics playerQuestLab customQuestLog "
        "masteryCelebration failureCoach tryNextHint stagePhysicsOverlay "
        "objectStageStyle objectTrajectoryLabel rewardShards",
        encoding="utf-8",
    )
    (project_path / "src" / "styles.css").write_text(
        "grid-template-columns:minmax(0,1fr) .stageCompanionAvatar{bottom:170px}"
        ".starterObjectPreview{bottom:92px}.stageScene{min-height:620px}",
        encoding="utf-8",
    )
    (project_path / "src" / "engine" / "wordToWorld.ts").write_text(
        "semanticFallbackEntry normalizedCreativeName semanticRuleHeuristics",
        encoding="utf-8",
    )
    for name in [
        "browser-static-playtest.json",
        "long-playtest-result.json",
        "visual-product-test-result.json",
        "experience-product-test-result.json",
        "sandbox-product-test-result.json",
        "creative-product-test-result.json",
        "mastery-product-test-result.json",
        "semantic-synthesis-test-result.json",
        "spatial-product-test-result.json",
        "original-asset-provenance.json",
    ]:
        (project_path / "docs" / name).write_text('{"ok": true}\n', encoding="utf-8")
    (project_path / "docs" / "internal-test-result.json").write_text(
        json.dumps(
            {
                "npm_run_test_fun": {"exit_code": 0},
                "npm_run_test_browser_static": {"exit_code": 0},
                "npm_run_test_long": {"exit_code": 0},
            }
        ),
        encoding="utf-8",
    )

    payload = _final_player_experience_payload(
        GameProductionSpec(
            project_path=project_path,
            production_mode="scribble_adventure_functional_parity_v1",
            final_player_experience_required=True,
            final_player_experience_threshold=0.98,
        )
    )

    assert payload["dimensions"]["initial_scene_liveliness"]["score"] < payload["dimension_floor"]
    assert "dimension:initial_scene_liveliness" in payload["failures"]
    assert payload["pass"] is False


def test_image_object_interaction_iteration_installs_strict_human_feedback_gates(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "wordforge-image-interaction-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "wordforge"
    (project_path / "src").mkdir(parents=True)
    (project_path / "scripts").mkdir()
    (project_path / "docs").mkdir()
    (project_path / "package.json").write_text('{"scripts":{}}\n', encoding="utf-8")
    mission = Mission(
        mission_id="msn-wordforge-image-interaction",
        owner="kun",
        objective="Repair image object interaction parity.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-image-interaction",
        mission_id=mission.mission_id,
        version="wordforge-v28",
        objective=mission.objective,
        acceptance_criteria=["image-first generated objects and real object interactions"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-image-interaction",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        delivery_contract={
            "project_path": str(project_path),
            "production_mode": "scribble_adventure_functional_parity_v1",
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-image-interaction",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="image interaction repair",
        summary="Repair human rejected label-card drag behavior.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["The rework must not copy commercial game expression."],
    )
    work_item = WorkItem(
        work_item_id="work-wordforge-v28-01-image-object-interaction-iteration",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        expected_output="strict human-feedback rework gates",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    _activate_project_boundary(
        control_plane,
        work_item_id=work_item.work_item_id,
        project_path=project_path,
    )
    work_item = control_plane.work_items[work_item.work_item_id]
    runner = GameProductionRunner(control_plane=control_plane)

    result = runner.run(work_item)

    assert result.status == "done"
    visual_script = (project_path / "scripts" / "visual-product-test.mjs").read_text(
        encoding="utf-8"
    )
    sandbox_script = (project_path / "scripts" / "sandbox-product-test.mjs").read_text(
        encoding="utf-8"
    )
    package_json = json.loads((project_path / "package.json").read_text(encoding="utf-8"))
    assert "generatedObjectImage" in visual_script
    assert "generatedSprite" in visual_script
    assert "isFoodObject" in visual_script
    assert "application/x-wordforge-object-id" in sandbox_script
    assert "finishStageInteraction" in sandbox_script
    assert "dropObjectOnStage(touchDragName)" in sandbox_script
    assert 'setData("text/plain", object.name)' in sandbox_script
    assert package_json["scripts"]["test:visual"] == "node scripts/visual-product-test.mjs"
    assert package_json["scripts"]["test:sandbox"] == "node scripts/sandbox-product-test.mjs"
    assert (project_path / "docs" / "image-object-interaction-iteration.md").exists()


def test_game_production_runner_skips_npm_install_when_dependencies_are_present(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    (project_path / "docs").mkdir(parents=True)
    (project_path / "node_modules").mkdir()
    (project_path / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                    "test:internal": "node scripts/internal-test.mjs",
                    "test:user-sim": "node scripts/user-sim.mjs",
                    "test:fun": "node scripts/fun-playtest.mjs",
                }
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_command(
        command: list[str],
        _cwd: Path,
        _timeout: int,
    ) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = GameProductionRunner(control_plane=control_plane, command_runner=fake_command)
    _activate_project_boundary(
        control_plane,
        work_item_id="work-huohutu-v3-03-internal-test",
        project_path=project_path,
    )
    work_item = control_plane.work_items["work-huohutu-v3-03-internal-test"]

    result = runner.run(work_item)

    assert result.status == "done"
    assert ["npm", "install"] not in commands
    assert commands == [
        ["npm", "run", "build"],
        ["npm", "run", "test:internal"],
        ["npm", "run", "test:user-sim"],
        ["npm", "run", "test:fun"],
    ]
    payload = json.loads(
        (project_path / "docs" / "internal-test-result.json").read_text(encoding="utf-8")
    )
    assert payload["npm_install"] == {"skipped": True}


def test_failed_command_result_falls_back_to_local_artifact_on_permission_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "external-game"
    project_path.mkdir()
    original_write_text = game_production_module._write_text

    def guarded_write(path: Path, content: str) -> None:
        if path.is_relative_to(project_path):
            raise PermissionError("external project is read-only")
        original_write_text(path, content)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(game_production_module, "_write_text", guarded_write)
    work_item = WorkItem(
        work_item_id="work-wordforge-readonly-build",
        mission_id="msn-wordforge-readonly",
        task_plan_version="wordforge-readonly",
        type="test",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        expected_output="build gate",
    )
    command = GameProductionCommandResult(
        exit_code=1,
        stdout="error TS5033: Could not write file",
        stderr="EPERM: operation not permitted",
    )

    result = game_production_module._failed_command_result(
        work_item,
        "npm run build",
        command,
        project_path,
    )

    assert result.failure_category == "permission_failure"
    assert result.artifacts[0].path_or_uri.startswith(".kun-local/game-production-failures/")
    assert "permission_boundary_fallback" in result.artifacts[0].supports
    assert (tmp_path / result.artifacts[0].path_or_uri).exists()


def test_game_production_runner_blocks_missing_production_mode_to_prevent_template_leakage(
    tmp_path: Path,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    contract = control_plane.contracts["contract-game-production"].model_copy(
        update={
            "delivery_contract": {
                "project_path": str(project_path),
            },
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    runner = GameProductionRunner(control_plane=control_plane)
    work_item = control_plane.work_items["work-huohutu-v3-01-interaction-design"]

    result = runner.run(work_item)

    assert result.status == "failed"
    assert result.failure_category == "tool_failure"
    assert "production_mode is required" in result.summary
    assert "template leakage" in result.summary
    assert not (project_path / "docs" / "interaction-design.md").exists()


def test_game_production_runner_owner_guard(tmp_path: Path) -> None:
    control_plane, _project_path = _mission(tmp_path)
    runner = GameProductionRunner(control_plane=control_plane)
    assigned = control_plane.work_items["work-huohutu-v3-01-interaction-design"]
    unassigned = assigned.model_copy(update={"owner": "codex-supervisor"})

    assert runner.can_run(assigned) is True
    assert runner.can_run(unassigned) is False


def test_interaction_design_requires_workspace_sandbox_and_resource_lock(tmp_path: Path) -> None:
    control_plane, project_path = _mission(tmp_path)
    runner = GameProductionRunner(control_plane=control_plane)
    work_item = control_plane.work_items["work-huohutu-v3-01-interaction-design"]

    result = runner.run(work_item)

    assert result.status == "failed"
    assert result.failure_category == "permission_failure"
    assert "workspace_ref" in result.summary
    assert not (project_path / "docs" / "interaction-design.md").exists()


def test_game_production_runner_classifies_permission_boundary_as_permission_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_plane, project_path = _mission(tmp_path)
    work_item = WorkItem(
        work_item_id="work-game-permission-interaction-design",
        mission_id="msn-game-production",
        task_plan_version="playable-v1",
        type="execution",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        workspace_ref=f"workspace://{project_path}",
        sandbox_ref="sandbox://msn-game-production/work-game-permission-interaction-design",
        resource_locks=[f"workspace:{project_path}"],
        expected_output="interaction design",
    )

    def blocked_write(_path: Path, _content: str) -> None:
        raise PermissionError("sandbox denied write")

    monkeypatch.setattr(game_production_module, "_write_text", blocked_write)

    result = GameProductionRunner(control_plane=control_plane).run(work_item)

    assert result.status == "failed"
    assert result.failure_category == "permission_failure"
    assert "permission boundary" in result.summary


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

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
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

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
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
    generated_rules = (project_path / "src" / "engine" / "worldRules.ts").read_text(
        encoding="utf-8"
    )
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
    control_plane.work_items[work_item.work_item_id] = work_item
    _activate_project_boundary(
        control_plane,
        work_item_id=work_item.work_item_id,
        project_path=project_path,
    )
    work_item = control_plane.work_items[work_item.work_item_id]

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

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
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

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
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
    assert [
        item_id for item_id in report.ran_work_item_ids if item_id in core_run_ids
    ] == core_run_ids
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
