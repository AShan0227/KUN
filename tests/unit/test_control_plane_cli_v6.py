from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from kun.cli import app
from kun.control_plane import (
    ExecutionContract,
    FileControlPlaneStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
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
    payload = json.loads(result.output)
    assert payload["status"] == "stopped"
    assert payload["state"] is None
    assert payload["pending_stop_request"] is None


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
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["report"]["daemon_id"] == "daemon-cli-test"
    assert payload["report"]["stopped_reason"] == "max_ticks"
    assert payload["service_state"]["status"] == "stopped"
    assert payload["service_state"]["stopped_reason"] == "max_ticks"
    assert payload["service_state"]["active_mission_ids"] == ["msn-cli-v6"]
    recovered = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    assert any("daemon_progress" in artifact.supports for artifact in recovered.artifacts.values())


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
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["platform"] == "launchd"
    assert payload["service_name"] == "com.kun.control-plane.cli-test"
    assert "daemon-run" in payload["command"]
    assert "--max-ticks" not in payload["command"]
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
