from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.main import app, install_v6_control_plane_runtime
from kun.control_plane import (
    ExecutionContract,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)


def test_main_app_mounts_control_plane_router() -> None:
    routes = {getattr(route, "path", "") for route in app.routes}

    assert "/api/control-plane/v6/missions" in routes
    assert "/api/control-plane/v6/daemon-service/status" in routes


def test_main_installs_file_backed_control_plane_runtime(tmp_path) -> None:
    test_app = FastAPI()
    install_v6_control_plane_runtime(
        test_app,
        store_path=tmp_path / "control-plane.json",
        state_path=tmp_path / "daemon-state.json",
    )
    test_app.include_router(app.router)
    client = TestClient(test_app)
    payload = _mission_payload()

    created = client.post("/api/control-plane/v6/missions", json=payload)
    missions = client.get("/api/control-plane/v6/missions")

    assert created.status_code == 200
    assert missions.status_code == 200
    assert missions.json()[0]["mission_id"] == "msn-main-api-v6"
    assert (tmp_path / "control-plane.json").exists()


def _mission_payload() -> dict[str, object]:
    mission = Mission(
        mission_id="msn-main-api-v6",
        owner="kun",
        objective="Show real Control Plane state in the cockpit",
        task_type="ops_tooling",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-main-api-v6",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["Cockpit can list the mission."],
        constraints=["Use durable Control Plane state."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-main-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["read control plane state"],
        forbidden_actions=["use detached mock state"],
    )
    context = WorkingContext(
        working_context_id="ctx-main-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="operator",
        scope="cockpit-test",
        summary="The cockpit should read the same file-backed state as the daemon.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_item = WorkItem(
        work_item_id="work-main-api-v6",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="control-plane",
        expected_output="listed mission",
    )
    return {
        "mission": mission.model_dump(mode="json"),
        "task_plan": plan.model_dump(mode="json"),
        "execution_contract": contract.model_dump(mode="json"),
        "working_context": context.model_dump(mode="json"),
        "work_items": [work_item.model_dump(mode="json")],
        "actor": "test",
    }
