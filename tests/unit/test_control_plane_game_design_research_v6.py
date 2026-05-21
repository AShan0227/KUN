from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kun.control_plane import (
    KUN_AUTONOMOUS_APP_RUNNER_OWNER,
    KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
    AppCommandResult,
    AutonomousAppDevelopmentRunner,
    ControlPlaneDaemon,
    ExecutionContract,
    FileControlPlaneStore,
    GameDesignResearchRunner,
    InMemoryControlPlane,
    Mission,
    ResearchSource,
    TaskPlan,
    WorkingContext,
    WorkItem,
)

NOW = datetime(2026, 5, 20, 9, 30, tzinfo=UTC)


def _mission(tmp_path: Path) -> tuple[InMemoryControlPlane, Path]:
    store = FileControlPlaneStore(tmp_path / "game-design-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "huohutu-research"
    final_project_path = tmp_path / "huohutu-research-gated"
    mission = Mission(
        mission_id="msn-game-design",
        owner="kun",
        objective="Build a playable research-backed children AI world MVP",
        task_type="product_development",
        status="contracted",
        risk_level="high",
    )
    plan = TaskPlan(
        plan_id="plan-game-design-research-first",
        mission_id=mission.mission_id,
        version="mvp-v2-research-first",
        objective="Research game, education, and child safety references before development.",
        known_facts=["User requires deep research before MVP development."],
        acceptance_criteria=[
            "research corpus exists",
            "game design spec exists",
            "playable MVP builds",
        ],
        constraints=["supervisor must not write game delivery code"],
        evidence_plan=["external sources", "game design spec", "build result"],
        approval_status="approved_with_limits",
    )
    sources = [
        ResearchSource(
            source_id=f"source-{index}",
            title=f"Source {index}",
            url=f"https://example.com/source-{index}",
            category="benchmark",
            organization="Example",
            design_takeaway=f"Takeaway {index}",
        ).model_dump(mode="json")
        for index in range(12)
    ]
    contract = ExecutionContract(
        contract_id="contract-game-design-research-first",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read_sources", "create_new_local_project", "install_npm_dependencies"],
        forbidden_actions=["manual_supervisor_delivery"],
        evidence_policy={"research_sources": sources},
        delivery_contract={
            "project_path": str(project_path),
            "final_project_path": str(final_project_path),
            "first_worlds": ["彩虹造物岛", "故事星球"],
        },
    )
    context = WorkingContext(
        working_context_id="ctx-game-design-research-first",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-design-research-runner",
        scope="research before game development",
        summary="Research-first game design context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_items = [
        WorkItem(
            work_item_id="work-huohutu-v2-00-research-source-corpus",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="research",
            owner=KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
            expected_output="research corpus",
        ),
        WorkItem(
            work_item_id="work-huohutu-v2-02-game-design-synthesis",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="research",
            owner=KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
            dependencies=["work-huohutu-v2-00-research-source-corpus"],
            expected_output="game design synthesis",
        ),
        WorkItem(
            work_item_id="work-huohutu-v2-03-research-design-gate",
            mission_id=mission.mission_id,
            task_plan_version=plan.version,
            type="review",
            owner=KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
            dependencies=["work-huohutu-v2-02-game-design-synthesis"],
            expected_output="research design gate",
        ),
    ]
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=work_items,
    )
    return control_plane, final_project_path


def test_game_design_runner_research_gate_queues_app_runner_and_builds_mvp(
    tmp_path: Path,
) -> None:
    control_plane, final_project_path = _mission(tmp_path)

    def fake_fetcher(source: ResearchSource) -> tuple[str, str, str]:
        return (
            "fetched",
            source.title,
            f"{source.organization} says {source.design_takeaway}",
        )

    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> AppCommandResult:
        commands.append(command)
        return AppCommandResult(exit_code=0, stdout="ok", stderr="")

    research_runner = GameDesignResearchRunner(
        control_plane=control_plane,
        fetcher=fake_fetcher,
    )
    app_runner = AutonomousAppDevelopmentRunner(
        control_plane=control_plane,
        command_runner=fake_command,
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="game-design-daemon-test",
        runners_by_owner={
            KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER: research_runner,
            KUN_AUTONOMOUS_APP_RUNNER_OWNER: app_runner,
        },
    )

    report = daemon.tick_once(
        mission_ids=["msn-game-design"],
        now=NOW,
        max_work_items=12,
    )

    assert report.no_runner_work_item_ids == []
    assert "work-huohutu-v2-03-research-design-gate" in report.ran_work_item_ids
    assert "work-huohutu-v2-15-qa-delivery" in report.ran_work_item_ids
    assert commands == [["npm", "install"], ["npm", "run", "build"]]
    assert (final_project_path / "docs" / "delivery-report.md").exists()
    assert (tmp_path / "huohutu-research" / "docs" / "game-design-spec.md").exists()
    assert control_plane.missions["msn-game-design"].current_plan_version == (
        "mvp-v2-design-gated"
    )
    assert control_plane.missions["msn-game-design"].status == "delivering"
    assert control_plane.contracts[
        "contract-msn-game-design-mvp-v2-design-gated"
    ].delivery_contract["research_first"]


def test_game_design_runner_supplements_contract_sources_to_threshold(tmp_path: Path) -> None:
    control_plane, _final_project_path = _mission(tmp_path)
    contract = control_plane.contracts["contract-game-design-research-first"]
    contract = contract.model_copy(
        update={
            "evidence_policy": {
                "research_sources": contract.evidence_policy["research_sources"][:6],
                "minimum_sources": 12,
                "minimum_live_fetches": 6,
            }
        }
    )
    control_plane.contracts[contract.contract_id] = contract

    def fake_fetcher(source: ResearchSource) -> tuple[str, str, str]:
        return (
            "fetched",
            source.title,
            f"{source.organization} says {source.design_takeaway}",
        )

    runner = GameDesignResearchRunner(control_plane=control_plane, fetcher=fake_fetcher)
    item = control_plane.work_items["work-huohutu-v2-00-research-source-corpus"]
    result = runner.run(item)

    assert result.status == "done"
    corpus_path = tmp_path / "huohutu-research" / "docs" / "research" / "source-corpus.json"
    payload = corpus_path.read_text(encoding="utf-8")
    assert payload.count('"fetch_status": "fetched"') == 12


def test_game_design_runner_owner_guard(tmp_path: Path) -> None:
    control_plane, _final_project_path = _mission(tmp_path)
    runner = GameDesignResearchRunner(control_plane=control_plane)
    assigned = control_plane.work_items["work-huohutu-v2-00-research-source-corpus"]
    unassigned = assigned.model_copy(update={"owner": "codex-supervisor"})

    assert runner.can_run(assigned) is True
    assert runner.can_run(unassigned) is False
