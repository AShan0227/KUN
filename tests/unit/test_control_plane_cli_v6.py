from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kun.cli import _resolve_cli_local_state_path, _write_rainflow_mission_package_manifest, app
from kun.control_plane import (
    DaemonServiceState,
    ExecutionContract,
    FileControlPlaneStore,
    FileDaemonServiceStateStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
    build_rainflow_ad_mission,
)
from typer.testing import CliRunner


def _seed_control_plane_store(path: Path) -> None:
    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(path))
    mission = Mission(
        mission_id="msn-cli-v6",
        owner="kun",
        objective="Run V6 Control Plane daemon from CLI",
        task_type="ops_tooling",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-cli-v6",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["daemon writes auditable heartbeat"],
        constraints=["do not require manual terminal babysitting"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-cli-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["wake daemon"],
        forbidden_actions=["drop durable state"],
    )
    context = WorkingContext(
        working_context_id="ctx-cli-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="cli-test",
        summary="CLI daemon entrypoint test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-cli-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=70,
        expected_output="daemon-visible work item",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )


def _seed_qi_followup_store(path: Path) -> None:
    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(path))
    mission = Mission(
        mission_id="msn-cli-qi-v6",
        owner="kun",
        objective="Run a Qi runtime follow-up from CLI daemon",
        task_type="self_improvement",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-cli-qi-v6",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["Qi follow-up is executed by default daemon routing"],
        constraints=["non-production learning cannot become default runtime ability"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-cli-qi-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["record runtime learning evidence"],
        forbidden_actions=["enable replay candidates by default"],
    )
    context = WorkingContext(
        working_context_id="ctx-cli-qi-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="qi",
        scope="runtime-followup",
        summary="Qi CLI activation test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-qi-runtime-learning-cli",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        priority=90,
        expected_output="Review this runtime signal as a capability candidate.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )


def test_control_plane_daemon_status_reports_empty_state(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-status",
            "--state-path",
            str(tmp_path / "daemon-state.json"),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.find("{\n") :])
    assert payload["status"] == "stopped"
    assert payload["state"] is None
    assert payload["healthy"] is False
    assert payload["stale"] is False
    assert payload["pending_stop_request"] is None


def test_control_plane_daemon_status_flags_stale_heartbeat(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    FileDaemonServiceStateStore(state_path).save(
        DaemonServiceState(
            daemon_id="daemon-cli-stale",
            status="idle",
            started_at=datetime.now(UTC) - timedelta(hours=2),
            updated_at=datetime.now(UTC) - timedelta(hours=1),
            process_id=999_999_999,
            last_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-status",
            "--state-path",
            str(state_path),
            "--stale-heartbeat-after-sec",
            "60",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "unhealthy"
    assert payload["healthy"] is False
    assert payload["stale"] is True
    assert payload["state"]["status"] == "idle"
    assert payload["process_alive"] is False


def test_control_plane_daemon_status_flags_missing_live_process(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    FileDaemonServiceStateStore(state_path).save(
        DaemonServiceState(
            daemon_id="daemon-cli-missing-process",
            status="idle",
            started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            process_id=999_999_999,
            last_heartbeat_at=datetime.now(UTC),
        )
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-status",
            "--state-path",
            str(state_path),
            "--stale-heartbeat-after-sec",
            "60",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "unhealthy"
    assert payload["healthy"] is False
    assert payload["stale"] is False
    assert payload["process_alive"] is False


def test_control_plane_daemon_status_accepts_live_process(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    FileDaemonServiceStateStore(state_path).save(
        DaemonServiceState(
            daemon_id="daemon-cli-live-process",
            status="idle",
            started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            process_id=os.getpid(),
            last_heartbeat_at=datetime.now(UTC),
        )
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-status",
            "--state-path",
            str(state_path),
            "--stale-heartbeat-after-sec",
            "60",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "idle"
    assert payload["healthy"] is True
    assert payload["process_alive"] is True


def test_control_plane_rainflow_ad_mission_registers_isolated_mission(tmp_path) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["requested_mission_id"] == "msn-rainflow-ad-video"
    assert payload["mission_id"] == "msn-rainflow-ad-video"
    assert payload["recovered_mission_adopted"] is False
    assert payload["recovered_mission_adoption_reason"] is None
    assert payload["recovered_source_workspace_path"] is None
    assert payload["mission_status"] == "queued"
    assert payload["ports"] == {"app": 3401, "preview": 3402}
    assert payload["workspace_ref"].startswith("workspace://")
    assert payload["sandbox_ref"] == "sandbox://workspace-snapshot/msn-rainflow-ad-video"
    assert any("phase1-mixed-edit-repair" in item_id for item_id in payload["work_item_ids"])
    assert payload["active_work_item_ids"] == payload["work_item_ids"]
    assert payload["active_work_item_count"] == len(payload["work_item_ids"])
    assert payload["historical_work_item_count"] == len(payload["work_item_ids"])
    assert payload["completed_work_item_count"] == 0
    assert payload["cancelled_work_item_count"] == 0
    assert payload["failed_work_item_count"] == 0

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = control_plane.missions["msn-rainflow-ad-video"]
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    assert mission.status == "queued"
    assert contract.delivery_contract["workspace_path"] == payload["workspace_path"]
    assert (root_dir / "msn-rainflow-ad-video" / "mission-package.json").exists()


def test_resolve_cli_local_state_path_falls_back_when_repo_is_not_writable(
    tmp_path, monkeypatch
) -> None:
    readonly_root = tmp_path / "readonly-worktree"
    readonly_root.mkdir()
    readonly_root.chmod(0o555)
    monkeypatch.delenv("KUN_LOCAL_STATE_ROOT", raising=False)
    temp_root = str((Path(tempfile.gettempdir()) / "kun-local-state").resolve())

    resolved = _resolve_cli_local_state_path(
        Path(".kun-local/v6-control-plane.json"),
        cwd=readonly_root,
    )

    assert resolved.name == "v6-control-plane.json"
    assert str(resolved).startswith(temp_root)
    assert str(resolved).endswith("v6-control-plane.json")
    readonly_root.chmod(0o755)


def test_control_plane_rainflow_ad_mission_relocates_dot_kun_local_paths_when_cwd_is_readonly(
    tmp_path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    readonly_root = tmp_path / "readonly-worktree"
    readonly_root.mkdir()
    readonly_root.chmod(0o555)
    monkeypatch.chdir(readonly_root)
    monkeypatch.delenv("KUN_LOCAL_STATE_ROOT", raising=False)
    temp_root = str((Path(tempfile.gettempdir()) / "kun-local-state").resolve())

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            ".kun-local/v6-control-plane.json",
            "--root-dir",
            ".kun-local/rainflow-missions",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert str(payload["store_path"]).startswith(temp_root)
    assert str(payload["root_dir"]).startswith(temp_root)
    assert Path(payload["workspace_path"]).exists()
    assert Path(payload["store_path"]).exists()
    readonly_root.chmod(0o755)


def test_write_rainflow_mission_package_manifest_accepts_parent_root_or_mission_root(
    tmp_path: Path,
) -> None:
    missions_root = tmp_path / "rainflow-missions"
    mission_root = missions_root / "msn-rainflow-ad-video"
    workspace_path = str(mission_root / "workspace")
    output_dir = str(mission_root / "outputs")
    sandbox_root = str(mission_root / "sandbox")

    from_parent = _write_rainflow_mission_package_manifest(
        root_dir=missions_root,
        mission_id="msn-rainflow-ad-video",
        workspace_path=workspace_path,
        output_dir=output_dir,
        sandbox_root=sandbox_root,
        ports={"app": 3401, "preview": 3402},
    )
    from_mission_root = _write_rainflow_mission_package_manifest(
        root_dir=mission_root,
        mission_id="msn-rainflow-ad-video",
        workspace_path=workspace_path,
        output_dir=output_dir,
        sandbox_root=sandbox_root,
        ports={"app": 3401, "preview": 3402},
    )

    assert from_parent == mission_root / "mission-package.json"
    assert from_mission_root == mission_root / "mission-package.json"
    assert not (mission_root / "msn-rainflow-ad-video" / "mission-package.json").exists()


def test_control_plane_rainflow_ad_mission_is_idempotent_for_existing_store_mission(
    tmp_path,
) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"
    first = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--preferred-app-port",
            "3499",
            "--preferred-preview-port",
            "3500",
            "--json",
        ],
    )

    assert second.exit_code == 0
    payload = json.loads(second.output)
    assert payload["created"] is False
    assert payload["ports"] == {"app": 3401, "preview": 3402}

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    work_items = [
        item
        for item in control_plane.work_items.values()
        if item.mission_id == "msn-rainflow-ad-video"
    ]
    assert len(work_items) == 12


def test_control_plane_rainflow_ad_mission_existing_summary_hides_terminal_history(
    tmp_path,
) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"
    package = build_rainflow_ad_mission(root_dir=root_dir, mission_id="msn-rainflow-active-summary")

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=[],
    )
    mission = mission.model_copy(update={"status": "running"})
    control_plane.missions[mission.mission_id] = mission
    control_plane.store.put_mission(mission)
    plan_version = mission.current_plan_version or package.task_plan.version
    active = WorkItem(
        work_item_id="work-rainflow-active",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="execution",
        owner="kun",
        priority=100,
        expected_output="Continue the live RainFlow mission.",
        status="running",
    )
    done = WorkItem(
        work_item_id="work-rainflow-done",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="Historical completed RainFlow work.",
        status="done",
    )
    cancelled = WorkItem(
        work_item_id="work-rainflow-cancelled",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="execution",
        owner="kun",
        priority=80,
        expected_output="Historical cancelled RainFlow work.",
        status="cancelled",
    )
    control_plane.work_items[active.work_item_id] = active
    control_plane.work_items[done.work_item_id] = done
    control_plane.work_items[cancelled.work_item_id] = cancelled
    control_plane.store.put_work_item(active)
    control_plane.store.put_work_item(done)
    control_plane.store.put_work_item(cancelled)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--mission-id",
            mission.mission_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is False
    assert payload["work_item_ids"] == ["work-rainflow-active"]
    assert payload["active_work_item_ids"] == ["work-rainflow-active"]
    assert payload["active_work_item_count"] == 1
    assert payload["historical_work_item_count"] == 3
    assert payload["completed_work_item_count"] == 1
    assert payload["cancelled_work_item_count"] == 1
    assert payload["failed_work_item_count"] == 0


def test_control_plane_rainflow_ad_mission_prioritizes_failed_current_plan_over_provider_blocker(
    tmp_path,
) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"
    package = build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="msn-rainflow-failed-current-plan",
    )

    workspace_path = Path(package.execution_contract.delivery_contract["workspace_path"])
    reports_dir = workspace_path / "outputs" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "ready_provider_count": 0,
                "providers": [
                    {"provider": "seedance", "credentials_ready": False},
                ],
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "Run provider-backed Stage 1 after credentials are available.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = control_plane.submit_mission(
        mission=package.mission,
        task_plan=package.task_plan,
        execution_contract=package.execution_contract,
        working_context=package.working_context,
        work_items=[],
    )
    mission = mission.model_copy(update={"status": "running"})
    control_plane.missions[mission.mission_id] = mission
    control_plane.store.put_mission(mission)
    plan_version = mission.current_plan_version or package.task_plan.version
    failed = WorkItem(
        work_item_id="work-rainflow-phase1-failed",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="execution",
        owner="kun",
        priority=100,
        expected_output="Repair Phase 1 mixed-edit quality.",
        status="failed",
    )
    blocked = WorkItem(
        work_item_id="work-rainflow-stage1-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan_version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="Generate Stage 1 transition clips after Phase 1 repair passes.",
        dependencies=[failed.work_item_id],
        status="queued",
    )
    control_plane.work_items[failed.work_item_id] = failed
    control_plane.work_items[blocked.work_item_id] = blocked
    control_plane.store.put_work_item(failed)
    control_plane.store.put_work_item(blocked)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--mission-id",
            mission.mission_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rainflow_ai_stage_status"] == "planning_ready_provider_blocked"
    assert payload["current_plan_failed_work_item_ids"] == ["work-rainflow-phase1-failed"]
    assert payload["current_plan_failed_work_item_count"] == 1
    assert payload["runnable_work_item_ids"] == []
    assert payload["runnable_work_item_count"] == 0
    assert payload["dependency_blocked_work_item_ids"] == ["work-rainflow-stage1-blocked"]
    assert payload["dependency_blocked_work_item_count"] == 1
    assert payload["effective_mission_status"] == "repairing_failed_current_plan_work"
    assert payload["rainflow_next_blocker"] == "current_plan_failed_work"
    assert "repair and retest" in payload["rainflow_next_action"]


def test_control_plane_rainflow_ad_mission_ignores_stale_failed_plan_when_fresher_stage1_mock_exists(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "seedance", "credentials_ready": False}],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "Phase 1 is accepted. Wait for provider credentials.",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stage1_dir = external_workspace / "outputs" / "ai-video-stage1-transition-package"
    stage1_dir.mkdir(parents=True)
    stage1_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "status": "mock_verified",
                "phase1_acceptance": {
                    "ready": True,
                    "path": str(external_workspace / "outputs" / "phase1-improved-demo-packages"),
                    "history": {
                        "latest_accepted_path": str(
                            external_workspace
                            / "outputs"
                            / "phase1-improved-demo-packages"
                            / "manifest.json"
                        )
                    },
                },
                "transition_evidence": {
                    "status": "mock_verified",
                    "reason": "accepted_phase1_boundary_lab_mock_inserted",
                    "message": "Mock insertion path is verified.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-stale-failure-superseded-v1",
        owner="kun",
        objective="Ignore stale failed work when fresher accepted Stage 1 mock evidence exists.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-stale-failure-superseded",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["prefer fresher accepted evidence over stale failed work state"],
        constraints=["preserve isolated workspace metadata"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-stale-failure-superseded",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-stale-failure-superseded",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with fresher Stage 1 mock evidence.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    active_work = WorkItem(
        work_item_id="work-rainflow-stage1-transition",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    stale_failed = WorkItem(
        work_item_id="work-rainflow-phase1-stale-failed",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        priority=91,
        expected_output="old failed retest that has been superseded by fresher evidence",
        status="failed",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[active_work],
    )
    control_plane.work_items[stale_failed.work_item_id] = stale_failed
    control_plane.store.put_work_item(stale_failed)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--mission-id",
            mission.mission_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["current_plan_failed_work_item_ids"] == ["work-rainflow-phase1-stale-failed"]
    assert payload["rainflow_phase1_evidence_consistency"] == "stale_stage_audit_pointer"
    assert payload["rainflow_stage1_transition_status"] == "mock_verified"
    assert payload["effective_mission_status"] == "waiting_external_provider_credentials"
    assert payload["rainflow_next_blocker"] == "provider_credentials_missing"
    assert payload["rainflow_next_action"] == "Mock insertion path is verified."


def test_control_plane_rainflow_ad_mission_keeps_failed_current_plan_ahead_of_provider_blocker_when_downstream_work_is_blocked(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "seedance", "credentials_ready": False}],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stage1_dir = external_workspace / "outputs" / "ai-video-stage1-transition-package"
    stage1_dir.mkdir(parents=True)
    stage1_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "status": "mock_verified",
                "phase1_acceptance": {
                    "ready": True,
                    "path": str(external_workspace / "outputs" / "phase1-improved-demo-packages"),
                    "history": {
                        "latest_accepted_path": str(
                            external_workspace
                            / "outputs"
                            / "phase1-improved-demo-packages"
                            / "manifest.json"
                        )
                    },
                },
                "transition_evidence": {
                    "status": "mock_verified",
                    "reason": "accepted_phase1_boundary_lab_mock_inserted",
                    "message": "Mock insertion path is verified.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-failed-plan-priority-v1",
        owner="kun",
        objective="Keep failed current-plan work ahead of provider blocker when delivery chain is still blocked.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-failed-plan-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=[
            "failed current-plan work stays visible until downstream chain is cleared"
        ],
        constraints=["preserve isolated workspace metadata"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-failed-plan-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-failed-plan-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with stale audit pointer but blocked downstream work.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    blocked = WorkItem(
        work_item_id="work-rainflow-stage1-blocked-after-failure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
        dependencies=["work-rainflow-phase1-failed-priority"],
    )
    stale_failed = WorkItem(
        work_item_id="work-rainflow-phase1-failed-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        priority=91,
        expected_output="failed retest still blocks the next work item",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[stale_failed, blocked],
    )
    repairing = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "repairing"}
    )
    control_plane.missions[repairing.mission_id] = repairing
    control_plane.store.put_mission(repairing)
    stale_failed = control_plane.work_items[stale_failed.work_item_id].model_copy(
        update={"status": "failed"}
    )
    control_plane.work_items[stale_failed.work_item_id] = stale_failed
    control_plane.store.put_work_item(stale_failed)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--mission-id",
            mission.mission_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["current_plan_failed_work_item_ids"] == ["work-rainflow-phase1-failed-priority"]
    assert payload["dependency_blocked_work_item_ids"] == [
        "work-rainflow-stage1-blocked-after-failure"
    ]
    assert payload["dependency_blocked_work_item_count"] == 1
    assert payload["rainflow_phase1_evidence_consistency"] == "stale_stage_audit_pointer"
    assert payload["rainflow_stage1_transition_status"] == "mock_verified"
    assert payload["effective_mission_status"] == "repairing_failed_current_plan_work"
    assert payload["rainflow_next_blocker"] == "current_plan_failed_work"
    assert "repair and retest" in payload["rainflow_next_action"]


def test_control_plane_rainflow_ad_mission_reuses_existing_isolated_package_when_store_is_empty(
    tmp_path,
) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"
    package = build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="rainflow-kun-isolated-20260523-125127",
        preferred_ports=(3481, 3482),
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["mission_id"] == "rainflow-kun-isolated-20260523-125127"
    assert payload["ports"] == {"app": 3481, "preview": 3482}
    assert (
        payload["workspace_path"] == package.execution_contract.delivery_contract["workspace_path"]
    )
    assert payload["recovered_mission_adopted"] is True
    assert payload["recovered_mission_adoption_reason"] == "recovered_existing_isolated_mission"
    assert (
        payload["recovered_source_workspace_path"]
        == package.execution_contract.delivery_contract["workspace_path"]
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = control_plane.missions["rainflow-kun-isolated-20260523-125127"]
    contract = control_plane.contracts[mission.execution_contract_ref or ""]
    assert contract.delivery_contract["ports"] == {"app": 3481, "preview": 3482}


def test_control_plane_rainflow_ad_mission_same_id_recovery_does_not_claim_adoption(
    tmp_path,
) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    root_dir = tmp_path / "rainflow-missions"
    package = build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="msn-rainflow-ad-video",
        preferred_ports=(3401, 3402),
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["mission_id"] == "msn-rainflow-ad-video"
    assert (
        payload["workspace_path"] == package.execution_contract.delivery_contract["workspace_path"]
    )
    assert payload["recovered_mission_adopted"] is False
    assert payload["recovered_mission_adoption_reason"] is None
    assert payload["recovered_source_workspace_path"] is None


def test_control_plane_rainflow_ad_mission_recovers_external_workspace_from_sibling_store(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    recovery_store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    recovery_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(recovery_store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow Phase 1 mixed-edit quality work from the persisted external workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-external-recovery",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=[
            "attach the persisted RainFlow workspace instead of creating a blank one"
        ],
        constraints=["do not orphan the active isolated RainFlow line"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-external-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
            "phase1_must_pass_before_ai_video": True,
            "human_playtest_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-external-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow CLI mission from sibling control-plane state.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-external-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    recovery_control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["mission_id"] == mission.mission_id
    assert payload["workspace_path"] == str(external_workspace)
    assert payload["output_dir"] == str(external_output)
    assert payload["ports"] == {"app": 5188, "preview": 5189}

    registered_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    registered_mission = registered_control_plane.missions[mission.mission_id]
    registered_contract = registered_control_plane.contracts[
        registered_mission.execution_contract_ref or ""
    ]
    assert registered_contract.delivery_contract["workspace_path"] == str(external_workspace)
    assert registered_contract.delivery_contract["output_dir"] == str(external_output)
    assert (root_dir / mission.mission_id / "mission-package.json").exists()


def test_control_plane_rainflow_ad_mission_prefers_recovered_nonempty_workspace_over_blank_scaffold(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    recovery_store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"

    build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="msn-rainflow-ad-video",
        preferred_ports=(3401, 3402),
    )

    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    (external_workspace / "pipeline.py").write_text(
        "def build_adflow_timeline():\n    return ['hook', 'proof', 'cta']\n",
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    recovery_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(recovery_store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow Phase 1 mixed-edit quality work from the persisted external workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["attach the richer recovered RainFlow workspace"],
        constraints=["do not pin the automation to an empty scaffold"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with non-empty workspace.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    recovery_control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is True
    assert payload["mission_id"] == mission.mission_id
    assert payload["workspace_path"] == str(external_workspace)
    assert payload["output_dir"] == str(external_output)


def test_control_plane_rainflow_ad_mission_replaces_blank_persisted_scaffold_with_richer_recovery(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    recovery_store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"

    scaffold_package = build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="msn-rainflow-ad-video",
        preferred_ports=(3401, 3402),
    )
    scaffold_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    scaffold_control_plane.submit_mission(
        mission=scaffold_package.mission,
        task_plan=scaffold_package.task_plan,
        execution_contract=scaffold_package.execution_contract,
        working_context=scaffold_package.working_context,
        work_items=scaffold_package.work_items,
    )

    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    (external_workspace / "pipeline.py").write_text(
        "def build_adflow_timeline():\n    return ['hook', 'proof', 'cta']\n",
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    recovery_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(recovery_store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow Phase 1 mixed-edit quality work from the persisted external workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["attach the richer recovered RainFlow workspace"],
        constraints=["do not pin the automation to an empty scaffold"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with non-empty workspace.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-rich-workspace-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    recovery_control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested_mission_id"] == "msn-rainflow-ad-video"
    assert payload["mission_id"] == mission.mission_id
    assert payload["recovered_mission_adopted"] is True
    assert (
        payload["recovered_mission_adoption_reason"]
        == "requested_mission_scaffold_was_blank_and_recovered_workspace_had_more_real_files"
    )
    assert payload["recovered_source_workspace_path"] == str(external_workspace)
    assert payload["workspace_path"] == str(external_workspace)
    assert payload["output_dir"] == str(external_output)


def test_control_plane_rainflow_ad_mission_replaces_pristine_nonempty_scaffold_with_richer_recovery(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "control-plane.json"
    recovery_store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"

    scaffold_package = build_rainflow_ad_mission(
        root_dir=root_dir,
        mission_id="msn-rainflow-ad-video",
        preferred_ports=(3401, 3402),
    )
    scaffold_workspace = Path(
        scaffold_package.execution_contract.delivery_contract["workspace_path"]
    ).resolve()
    (scaffold_workspace / "local-scaffold-note.md").write_text(
        "# Local scaffold\n\nThis queued workspace exists but has no real progress.\n",
        encoding="utf-8",
    )
    scaffold_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    scaffold_control_plane.submit_mission(
        mission=scaffold_package.mission,
        task_plan=scaffold_package.task_plan,
        execution_contract=scaffold_package.execution_contract,
        working_context=scaffold_package.working_context,
        work_items=scaffold_package.work_items,
    )

    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    (external_workspace / "pipeline.py").write_text(
        "def build_adflow_timeline():\n    return ['hook', 'proof', 'cta']\n",
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    recovery_control_plane = InMemoryControlPlane(store=FileControlPlaneStore(recovery_store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow Phase 1 mixed-edit quality work from the persisted external workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-rich-workspace-recovery-nonempty-scaffold",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["attach the richer recovered RainFlow workspace"],
        constraints=["do not pin the automation to a pristine local scaffold"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-rich-workspace-recovery-nonempty-scaffold",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-rich-workspace-recovery-nonempty-scaffold",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with richer external workspace.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-rich-workspace-recovery-nonempty-scaffold",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    recovery_control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requested_mission_id"] == "msn-rainflow-ad-video"
    assert payload["mission_id"] == mission.mission_id
    assert payload["recovered_mission_adopted"] is True
    assert (
        payload["recovered_mission_adoption_reason"]
        == "requested_mission_scaffold_was_blank_and_recovered_workspace_had_more_real_files"
    )
    assert payload["workspace_path"] == str(external_workspace)
    assert payload["output_dir"] == str(external_output)


def test_control_plane_rainflow_ad_mission_backfills_missing_isolation_metadata_for_existing_recovery(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow Phase 1 mixed-edit quality work from the recovered workspace.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-existing-recovery",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["keep the recovered RainFlow mission isolated and resumable"],
        constraints=["preserve the recovered external workspace path"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-existing-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-existing-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission that still needs isolation receipts backfilled.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-existing-recovery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["created"] is False
    assert payload["mission_id"] == mission.mission_id
    assert payload["sandbox_root"] == str((root_dir / mission.mission_id / "sandbox").resolve())
    assert payload["workspace_ref"] == f"workspace://{external_workspace}"
    assert payload["sandbox_ref"] == f"sandbox://workspace-snapshot/{mission.mission_id}"
    assert payload["resource_locks"] == [
        f"mission:{mission.mission_id}",
        f"workspace:{external_workspace}",
        f"output_dir:{external_output}",
    ]
    assert payload["ports"] == {"app": 5188, "preview": 5189}

    reloaded = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    stored_mission = reloaded.missions[mission.mission_id]
    stored_contract = reloaded.contracts[stored_mission.execution_contract_ref or ""]
    assert stored_contract.delivery_contract["sandbox_root"] == str(
        (root_dir / mission.mission_id / "sandbox").resolve()
    )
    assert stored_contract.delivery_contract["workspace_ref"] == f"workspace://{external_workspace}"
    assert stored_contract.delivery_contract["sandbox_ref"] == (
        f"sandbox://workspace-snapshot/{mission.mission_id}"
    )
    assert stored_contract.delivery_contract["resource_locks"] == [
        f"mission:{mission.mission_id}",
        f"workspace:{external_workspace}",
        f"output_dir:{external_output}",
    ]
    stored_work = reloaded.work_items["work-rainflow-existing-recovery"]
    assert stored_work.workspace_ref == f"workspace://{external_workspace}"
    assert stored_work.sandbox_ref == f"sandbox://workspace-snapshot/{mission.mission_id}"
    assert stored_work.resource_locks == [
        f"mission:{mission.mission_id}",
        f"workspace:{external_workspace}",
        f"output_dir:{external_output}",
    ]
    manifest_path = root_dir / mission.mission_id / "mission-package.json"
    assert manifest_path.exists()
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload == {
        "mission_id": mission.mission_id,
        "output_dir": str(external_output),
        "ports": {"app": 5188, "preview": 5189},
        "sandbox_root": str((root_dir / mission.mission_id / "sandbox").resolve()),
        "workspace_path": str(external_workspace),
    }


def test_control_plane_rainflow_ad_mission_reports_provider_blocked_stage_audit(tmp_path) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    reports_dir = external_workspace / "outputs" / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                    "auto_discovered": True,
                },
                "providers": [
                    {"provider": "kling", "credentials_ready": False},
                    {"provider": "runway", "credentials_ready": False},
                ],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "wait for provider credentials",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Continue RainFlow AI stage work after accepted Phase 1 evidence.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-provider-blocked",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["surface real provider blockers in supervision output"],
        constraints=["preserve the recovered external workspace path"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-provider-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-provider-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission blocked on provider credentials.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-provider-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )
    running = control_plane.missions[mission.mission_id].model_copy(update={"status": "running"})
    control_plane.missions[running.mission_id] = running
    control_plane.store.put_mission(running)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mission_status"] == "running"
    assert payload["effective_mission_status"] == "waiting_external_provider_credentials"
    assert payload["rainflow_stage_audit_status"] == "next"
    assert payload["rainflow_phase1_ready"] is True
    assert payload["rainflow_phase1_evidence_reason"] == "phase1_acceptance_ready"
    assert (
        payload["rainflow_phase1_evidence_path"]
        == "outputs/phase1-improved-demo-packages/manifest.json"
    )
    assert payload["rainflow_phase1_evidence_auto_discovered"] is True
    assert (
        payload["rainflow_phase1_effective_evidence_path"]
        == "outputs/phase1-improved-demo-packages/manifest.json"
    )
    assert payload["rainflow_phase1_effective_evidence_source"] == "stage_audit"
    assert payload["rainflow_phase1_evidence_consistency"] == "aligned"
    assert payload["rainflow_ai_stage_status"] == "planning_ready_provider_blocked"
    assert payload["rainflow_next_blocker"] == "provider_credentials_missing"
    assert payload["rainflow_ready_provider_count"] == 0
    assert payload["rainflow_missing_providers"] == ["kling", "runway"]
    assert payload["rainflow_provider_blocked_stages"] == ["stage1_transition_clips"]
    assert payload["rainflow_ready_stages"] == []


def test_control_plane_rainflow_ad_mission_reports_phase1_blocked_stage_audit(tmp_path) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    reports_dir = external_workspace / "outputs" / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "blocked",
                "phase1_acceptance": {
                    "ready": False,
                    "reason": "phase1_acceptance_evidence_missing",
                },
                "providers": [],
                "ready_provider_count": 0,
                "stages": [],
                "next_action": "repair Phase 1 acceptance first",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-phase1-blocked-v1",
        owner="kun",
        objective="Repair Phase 1 before any RainFlow AI stage unlock.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-phase1-blocked",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["surface Phase 1 blocker in supervision output"],
        constraints=["preserve the recovered external workspace path"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-phase1-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-phase1-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission still blocked on Phase 1 acceptance.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-phase1-blocked",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )
    running = control_plane.missions[mission.mission_id].model_copy(update={"status": "running"})
    control_plane.missions[running.mission_id] = running
    control_plane.store.put_mission(running)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective_mission_status"] == "repairing_phase1"
    assert payload["rainflow_stage_audit_status"] == "blocked"
    assert payload["rainflow_phase1_ready"] is False
    assert payload["rainflow_next_blocker"] == "phase1_acceptance_evidence_missing"


def test_control_plane_rainflow_ad_mission_reports_stage1_ready_audit(tmp_path) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    reports_dir = external_workspace / "outputs" / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "kling", "credentials_ready": True}],
                "ready_provider_count": 1,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "ready",
                    }
                ],
                "next_action": "start Stage 1 on the isolated line",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    external_output = (tmp_path / "external-rainflow-output").resolve()
    external_output.mkdir()

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-stage1-ready-v1",
        owner="kun",
        objective="Start RainFlow Stage 1 once a provider is ready.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-stage1-ready",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["surface Stage 1 readiness in supervision output"],
        constraints=["preserve the recovered external workspace path"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-stage1-ready",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-stage1-ready",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission is ready for Stage 1.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-stage1-ready",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )
    running = control_plane.missions[mission.mission_id].model_copy(update={"status": "running"})
    control_plane.missions[running.mission_id] = running
    control_plane.store.put_mission(running)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["effective_mission_status"] == "ready_for_stage1"
    assert payload["rainflow_stage_audit_status"] == "ready"


def test_control_plane_rainflow_ad_mission_reads_stage_audit_from_isolated_output_dir(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "kling", "credentials_ready": True}],
                "ready_provider_count": 1,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "ready",
                    }
                ],
                "next_action": "start Stage 1 on the isolated line",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-stage1-output-dir-v1",
        owner="kun",
        objective="Surface Stage 1 readiness from the isolated output directory.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-stage1-output-dir",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["read the stage audit from output_dir, not only workspace/outputs"],
        constraints=["preserve the recovered external workspace path"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-stage1-output-dir",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-stage1-output-dir",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with audit evidence in the isolated output dir.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-stage1-output-dir",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rainflow_stage_audit_path"] == str(
        reports_dir / "ai-video-stage-gate-audit-latest.json"
    )
    assert payload["rainflow_stage_audit_status"] == "ready"
    assert payload["effective_mission_status"] == "ready_for_stage1"
    assert payload["rainflow_phase1_ready"] is True
    assert payload["rainflow_ready_provider_count"] == 1
    assert payload["rainflow_missing_providers"] == []
    assert payload["rainflow_ready_stages"] == ["stage1_transition_clips"]
    assert payload["rainflow_provider_blocked_stages"] == []


def test_control_plane_rainflow_ad_mission_surfaces_stage1_transition_evidence_gap(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "kling", "credentials_ready": False}],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "provider credentials are still missing",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stage1_dir = (
        external_output / "phase1-mixed-edit-repair-latest" / "ai-video-stage1-transition-package"
    )
    stage1_dir.mkdir(parents=True)
    stage1_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "status": "planning_ready_provider_blocked",
                "transition_evidence": {
                    "status": "planning_ready_provider_blocked",
                    "reason": "stage1_transition_timeline_missing",
                    "message": "Render a bridge-needed accepted timeline and capture bridge tasks before claiming Stage 1 evidence.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-stage1-evidence-gap-v1",
        owner="kun",
        objective="Do not overstate Stage 1 readiness when bridge evidence is missing.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-stage1-evidence-gap",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["surface the Stage 1 transition evidence gap before provider creds"],
        constraints=["keep the recovered external workspace metadata intact"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-stage1-evidence-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-stage1-evidence-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with a Stage 1 evidence gap.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-stage1-evidence-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rainflow_stage_audit_status"] == "next"
    assert payload["effective_mission_status"] == "waiting_external_provider_credentials"
    assert payload["rainflow_stage1_transition_package_path"] == str(stage1_dir / "manifest.json")
    assert payload["rainflow_stage1_transition_status"] == "planning_ready_provider_blocked"
    assert (
        payload["rainflow_stage1_transition_evidence_reason"]
        == "stage1_transition_timeline_missing"
    )
    assert payload["rainflow_next_blocker"] == "stage1_transition_timeline_missing"
    assert (
        payload["rainflow_next_action"]
        == "Render a bridge-needed accepted timeline and capture bridge tasks before claiming Stage 1 evidence."
    )


def test_control_plane_rainflow_ad_mission_prefers_fresher_transition_phase1_evidence(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                    "auto_discovered": True,
                },
                "providers": [{"provider": "kling", "credentials_ready": False}],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "provider credentials are still missing",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stage1_dir = (
        external_output / "phase1-mixed-edit-repair-latest" / "ai-video-stage1-transition-package"
    )
    stage1_dir.mkdir(parents=True)
    stage1_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "status": "mock_verified",
                "phase1_acceptance": {
                    "ready": True,
                    "path": str(
                        external_output
                        / "adflow-assembly-creator-rework-20260525-002223"
                        / "reference-master-batch-rendered"
                        / "phase1-improved-demo-packages"
                        / "manifest.json"
                    ),
                    "history": {
                        "latest_accepted_path": str(
                            external_output
                            / "adflow-assembly-creator-rework-20260525-002223"
                            / "reference-master-batch-rendered"
                            / "phase1-improved-demo-packages"
                            / "manifest.json"
                        )
                    },
                },
                "transition_evidence": {
                    "status": "mock_verified",
                    "reason": "accepted_phase1_boundary_lab_mock_inserted",
                    "message": "Mock insertion path is verified.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-phase1-evidence-linking-v1",
        owner="kun",
        objective="Prefer the freshest accepted Phase 1 evidence when Stage 1 packaging has newer links.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-phase1-evidence-linking",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["surface the freshest accepted Phase 1 evidence path"],
        constraints=["preserve isolated workspace metadata"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-phase1-evidence-linking",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-phase1-evidence-linking",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with newer accepted evidence in Stage 1 packaging.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-phase1-evidence-linking",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert (
        payload["rainflow_phase1_evidence_path"]
        == "outputs/phase1-improved-demo-packages/manifest.json"
    )
    assert payload["rainflow_phase1_effective_evidence_path"] == str(
        external_output
        / "adflow-assembly-creator-rework-20260525-002223"
        / "reference-master-batch-rendered"
        / "phase1-improved-demo-packages"
        / "manifest.json"
    )
    assert payload["rainflow_phase1_effective_evidence_source"] == "stage1_transition_history"
    assert payload["rainflow_phase1_evidence_consistency"] == "stale_stage_audit_pointer"


def test_control_plane_rainflow_ad_mission_prefers_fresh_workspace_stage1_transition_package(
    tmp_path,
) -> None:
    runner = CliRunner()
    root_dir = tmp_path / "rainflow-missions"
    store_path = tmp_path / "rainflow-adflow-extreme-control-plane.json"
    external_workspace = (tmp_path / "external-rainflow-workspace").resolve()
    external_workspace.mkdir()
    external_output = (tmp_path / "external-rainflow-output").resolve()
    reports_dir = external_output / "reports"
    reports_dir.mkdir(parents=True)
    reports_dir.joinpath("ai-video-stage-gate-audit-latest.json").write_text(
        json.dumps(
            {
                "status": "next",
                "phase1_acceptance": {
                    "ready": True,
                    "reason": "phase1_acceptance_ready",
                    "path": "outputs/phase1-improved-demo-packages/manifest.json",
                },
                "providers": [{"provider": "kling", "credentials_ready": False}],
                "ready_provider_count": 0,
                "stages": [
                    {
                        "stage_id": "stage1_transition_clips",
                        "status": "planning_ready_provider_blocked",
                        "provider_blocker": "provider_credentials_missing",
                    }
                ],
                "next_action": "provider credentials are still missing",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stale_stage1_dir = (
        external_output / "phase1-mixed-edit-repair-latest" / "ai-video-stage1-transition-package"
    )
    stale_stage1_dir.mkdir(parents=True)
    stale_manifest = stale_stage1_dir / "manifest.json"
    stale_manifest.write_text(
        json.dumps(
            {
                "status": "planning_ready_provider_blocked",
                "transition_evidence": {
                    "status": "planning_ready_provider_blocked",
                    "reason": "stage1_transition_timeline_missing",
                    "message": "Render a bridge-needed accepted timeline and capture bridge tasks before claiming Stage 1 evidence.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    workspace_stage1_dir = (
        external_workspace
        / "k_output"
        / "attempt-20260525-013624-stage1-transition-generation"
        / "ai-video-stage1-transition-package"
    )
    workspace_stage1_dir.mkdir(parents=True)
    fresh_manifest = workspace_stage1_dir / "manifest.json"
    fresh_manifest.write_text(
        json.dumps(
            {
                "status": "mock_verified",
                "phase1_acceptance": {
                    "ready": True,
                    "path": str(
                        external_workspace
                        / "outputs"
                        / "phase1-improved-demo-packages"
                        / "manifest.json"
                    ),
                },
                "transition_evidence": {
                    "status": "mock_verified",
                    "reason": "accepted_phase1_boundary_lab_mock_inserted",
                    "message": "Mock insertion path is verified.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    generic_stage1_dir = external_workspace / "outputs" / "ai-video-stage1-transition-package"
    generic_stage1_dir.mkdir(parents=True)
    generic_manifest = generic_stage1_dir / "manifest.json"
    generic_manifest.write_text(
        json.dumps(
            {
                "status": "blocked",
                "transition_evidence": {
                    "status": "blocked",
                    "reason": "phase1_gate_blocked",
                    "message": "Stage 1 remains blocked until Phase 1 acceptance is ready.",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stale_time = datetime(2026, 5, 25, 1, 0, tzinfo=UTC).timestamp()
    fresh_time = datetime(2026, 5, 25, 2, 0, tzinfo=UTC).timestamp()
    generic_time = datetime(2026, 5, 25, 3, 0, tzinfo=UTC).timestamp()
    os.utime(stale_manifest, (stale_time, stale_time))
    os.utime(fresh_manifest, (fresh_time, fresh_time))
    os.utime(generic_manifest, (generic_time, generic_time))

    control_plane = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-rainflow-stage1-fresh-workspace-package-v1",
        owner="kun",
        objective="Prefer the freshest Stage 1 transition package across isolated output roots.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-stage1-fresh-workspace-package",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["pick the freshest Stage 1 package even when it lives under k_output"],
        constraints=["preserve isolated workspace metadata"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-stage1-fresh-workspace-package",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(external_workspace),
            "workspace_path": str(external_workspace),
            "output_dir": str(external_output),
            "preferred_port": 5188,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-stage1-fresh-workspace-package",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Recovered RainFlow mission with fresher Stage 1 evidence under k_output.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work = WorkItem(
        work_item_id="work-rainflow-stage1-fresh-workspace-package",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        expected_output="continue recovered RainFlow mission",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work],
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "rainflow-ad-mission",
            "--store-path",
            str(store_path),
            "--root-dir",
            str(root_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rainflow_stage1_transition_package_path"] == str(fresh_manifest)
    assert payload["rainflow_stage1_transition_status"] == "mock_verified"
    assert (
        payload["rainflow_stage1_transition_evidence_reason"]
        == "accepted_phase1_boundary_lab_mock_inserted"
    )
    assert payload["rainflow_next_blocker"] == "provider_credentials_missing"
    assert payload["rainflow_next_action"] == "Mock insertion path is verified."


def test_control_plane_daemon_run_persists_service_state_and_progress(tmp_path) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    state_path = tmp_path / "daemon-state.json"
    _seed_control_plane_store(store_path)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-run",
            "--store-path",
            str(store_path),
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-test",
            "--mission-ids",
            "msn-cli-v6",
            "--poll-interval-sec",
            "0",
            "--max-ticks",
            "1",
            "--disable-mission-director",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.find("{\n") :])
    assert payload["report"]["daemon_id"] == "daemon-cli-test"
    assert payload["report"]["stopped_reason"] == "max_ticks"
    assert payload["service_state"]["status"] == "stopped"
    assert payload["service_state"]["stopped_reason"] == "max_ticks"
    assert payload["service_state"]["active_mission_ids"] == ["msn-cli-v6"]
    recovered = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    assert any("daemon_progress" in artifact.supports for artifact in recovered.artifacts.values())


def test_control_plane_daemon_run_executes_default_qi_followup_runner(tmp_path) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane-qi.json"
    state_path = tmp_path / "daemon-state-qi.json"
    _seed_qi_followup_store(store_path)

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-run",
            "--store-path",
            str(store_path),
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-qi-test",
            "--mission-ids",
            "msn-cli-qi-v6",
            "--poll-interval-sec",
            "0",
            "--max-ticks",
            "1",
            "--disable-mission-director",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.find("{\n") :])
    assert payload["report"]["tick_reports"][0]["ran_work_item_ids"] == [
        "work-qi-runtime-learning-cli"
    ]
    recovered = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    assert recovered.capability_profiles
    profile = next(iter(recovered.capability_profiles.values()))
    assert profile.promotion_stage == "replay"
    assert profile.runtime_enabled is False


def test_control_plane_daemon_stop_and_status_show_pending_stop_request(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    stopped = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-stop",
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-test",
            "--requested-by",
            "operator",
            "--reason",
            "maintenance",
            "--json",
        ],
    )
    status = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-status",
            "--state-path",
            str(state_path),
            "--json",
        ],
    )

    assert stopped.exit_code == 0
    assert datetime.fromisoformat(json.loads(stopped.output)["stop_request"]["requested_at"])
    assert status.exit_code == 0
    payload = json.loads(status.output)
    assert payload["pending_stop_request"]["daemon_id"] == "daemon-cli-test"
    assert payload["pending_stop_request"]["reason"] == "maintenance"


def test_control_plane_daemon_stop_clear_removes_pending_stop_request(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    runner.invoke(
        app,
        [
            "control-plane",
            "daemon-stop",
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-test",
        ],
    )

    cleared = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-stop",
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-test",
            "--clear",
            "--json",
        ],
    )

    assert cleared.exit_code == 0
    payload = json.loads(cleared.output)
    assert payload["cleared"] is True
    assert (
        FileDaemonServiceStateStore(state_path).stop_requested(daemon_id="daemon-cli-test") is False
    )


def test_control_plane_daemon_stop_clear_keeps_other_daemon_stop_request(tmp_path) -> None:
    runner = CliRunner()
    state_path = tmp_path / "daemon-state.json"
    FileDaemonServiceStateStore(state_path).request_stop(
        daemon_id="daemon-a",
        requested_by="operator",
        reason="maintenance",
    )

    cleared = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-stop",
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-b",
            "--clear",
            "--json",
        ],
    )

    assert cleared.exit_code == 0
    payload = json.loads(cleared.output)
    assert payload["cleared"] is False
    assert payload["mismatch"] is True
    assert payload["pending_stop_request"]["daemon_id"] == "daemon-a"
    assert FileDaemonServiceStateStore(state_path).stop_requested(daemon_id="daemon-a") is True
    assert FileDaemonServiceStateStore(state_path).stop_requested(daemon_id="daemon-b") is False


def test_control_plane_daemon_run_can_clear_stale_stop_request_before_start(tmp_path) -> None:
    runner = CliRunner()
    store_path = tmp_path / "control-plane.json"
    state_path = tmp_path / "daemon-state.json"
    _seed_control_plane_store(store_path)
    FileDaemonServiceStateStore(state_path).request_stop(
        daemon_id="daemon-cli-test",
        requested_by="operator",
        reason="previous stop",
    )

    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-run",
            "--store-path",
            str(store_path),
            "--state-path",
            str(state_path),
            "--daemon-id",
            "daemon-cli-test",
            "--mission-ids",
            "msn-cli-v6",
            "--poll-interval-sec",
            "0",
            "--max-ticks",
            "1",
            "--clear-stop-request",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.find("{\n") :])
    assert payload["report"]["stopped_reason"] == "max_ticks"
    assert payload["report"]["tick_count"] == 1
    assert (
        FileDaemonServiceStateStore(state_path).stop_requested(daemon_id="daemon-cli-test") is False
    )


def test_control_plane_daemon_service_plan_outputs_launchd_payload(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-service-plan",
            "--platform",
            "launchd",
            "--service-name",
            "com.kun.control-plane.cli-test",
            "--working-directory",
            str(tmp_path),
            "--install-path",
            str(tmp_path / "kun.plist"),
            "--ab-round-dir",
            str(tmp_path / "ab-round"),
            "--ab-round-id",
            "round-02-regression",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["platform"] == "launchd"
    assert payload["service_name"] == "com.kun.control-plane.cli-test"
    assert "daemon-run" in payload["command"]
    assert "--max-ticks" not in payload["command"]
    assert "--ab-round-dir" in payload["command"]
    assert str(tmp_path / "ab-round") in payload["command"]
    assert "--ab-round-id" in payload["command"]
    assert payload["install_path"] == str(tmp_path / "kun.plist")


def test_control_plane_daemon_service_install_writes_file(tmp_path) -> None:
    runner = CliRunner()
    service_path = tmp_path / "kun-control-plane.service"
    result = runner.invoke(
        app,
        [
            "control-plane",
            "daemon-service-install",
            "--platform",
            "systemd",
            "--service-name",
            "kun-control-plane",
            "--working-directory",
            str(tmp_path),
            "--install-path",
            str(service_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["written_path"] == str(service_path)
    assert service_path.exists()
    assert "Restart=always" in service_path.read_text(encoding="utf-8")
