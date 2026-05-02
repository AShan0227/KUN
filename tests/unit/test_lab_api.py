from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from kun.api.lab import router
from kun.core.tenancy import TenantContext, tenant_scope
from kun.lab import (
    EnsembleConfig,
    EnsemblePathResult,
    EnsembleResult,
    LabRecipeEntry,
    get_experiment_log,
    get_recipe_registry,
    reset_experiment_log,
    reset_recipe_registry,
)


def _fake_result(experiment_id: str = "exp-api-1") -> EnsembleResult:
    return EnsembleResult(
        experiment_id=experiment_id,
        config=EnsembleConfig(n_paths=2, metadata={"prompt": "Q4 plan"}),
        path_results=[
            EnsemblePathResult(
                path_idx=0,
                config={"strategy": "tier_top_low_temp", "tier": "top"},
                output="winning_text",
                score=0.9,
                cost_usd=0.05,
            ),
            EnsemblePathResult(
                path_idx=1,
                config={"strategy": "tier_cheap_high_temp", "tier": "cheap"},
                output="other_text",
                score=0.3,
                cost_usd=0.01,
            ),
        ],
        winning_path_idx=0,
        winning_output="winning_text",
        total_cost_usd=0.06,
        selection_reason="best_score:0.90",
    )


@pytest.fixture()
def client() -> TestClient:
    reset_experiment_log()
    reset_recipe_registry()
    old_lab_mode = os.environ.pop("KUN_LAB_MODE", None)

    app = FastAPI()

    @app.middleware("http")
    async def tenant_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        raw_scopes = request.headers.get("X-Scopes", "")
        scopes = tuple(s.strip() for s in raw_scopes.split(",") if s.strip())
        with tenant_scope(TenantContext(tenant_id="tenant-test", scopes=scopes)):
            return await call_next(request)

    app.include_router(router)
    with TestClient(app) as c:
        yield c

    reset_experiment_log()
    reset_recipe_registry()
    if old_lab_mode is not None:
        os.environ["KUN_LAB_MODE"] = old_lab_mode
    else:
        os.environ.pop("KUN_LAB_MODE", None)


def test_list_and_get_lab_experiments(client: TestClient) -> None:
    get_experiment_log().record(task_type="ad", ensemble_result=_fake_result())

    listed = client.get("/api/lab/experiments?task_type=ad").json()
    detail = client.get("/api/lab/experiments/exp-api-1").json()

    assert listed[0]["experiment_id"] == "exp-api-1"
    assert listed[0]["winning_path_idx"] == 0
    assert detail["ensemble_result"]["winning_output"] == "winning_text"


def test_get_lab_experiment_404(client: TestClient) -> None:
    response = client.get("/api/lab/experiments/missing")

    assert response.status_code == 404


def test_recipe_endpoints_dump_registry(client: TestClient) -> None:
    get_recipe_registry().upsert(
        LabRecipeEntry(
            task_type="ad",
            target_module="execution_mode_classifier",
            strategy="tier_top_low_temp",
            win_rate=0.8,
            confidence=0.9,
        )
    )

    all_recipes = client.get("/api/lab/recipes").json()
    by_type = client.get("/api/lab/recipes/ad").json()

    assert all_recipes[0]["task_type"] == "ad"
    assert by_type[0]["strategy"] == "tier_top_low_temp"


def test_post_run_rejects_when_lab_mode_disabled(client: TestClient) -> None:
    response = client.post("/api/lab/run", json={"prompt": "hello"})

    assert response.status_code == 403
    assert "KUN_LAB_MODE" in response.json()["detail"]


def test_post_run_executes_and_records_when_lab_enabled(client: TestClient) -> None:
    os.environ["KUN_LAB_MODE"] = "1"
    fake_executor = AsyncMock()
    fake_executor.run = AsyncMock(return_value=_fake_result("exp-run-1"))

    with (
        patch("kun.lab.EnsembleExecutor", return_value=fake_executor),
        patch("kun.lab.make_default_adapter"),
    ):
        response = client.post(
            "/api/lab/run",
            json={"prompt": "hello", "task_type": "ad", "emit_events": False},
        )

    assert response.status_code == 200
    assert response.json()["experiment_id"] == "exp-run-1"
    assert len(get_experiment_log().list_all()) == 1


def test_promote_requires_admin_scope(client: TestClient) -> None:
    response = client.post("/api/lab/promote", json={})

    assert response.status_code == 403


def test_promote_runs_for_admin(client: TestClient) -> None:
    for idx in range(10):
        get_experiment_log().record(task_type="ad", ensemble_result=_fake_result(f"exp-{idx}"))

    response = client.post(
        "/api/lab/promote",
        headers={"X-Scopes": "lab:admin"},
        json={"min_total": 10, "min_winrate": 0.6},
    )

    assert response.status_code == 200
    assert response.json()["promoted"] >= 1


def test_ws_lab_experiment_stream(client: TestClient) -> None:
    get_experiment_log().record(task_type="ad", ensemble_result=_fake_result())

    with client.websocket_connect("/api/lab/ws/experiment/exp-api-1/stream") as ws:
        first = ws.receive_json()
        second = ws.receive_json()
        done = ws.receive_json()

    assert first["event"] == "path.completed"
    assert second["path_idx"] == 1
    assert done["event"] == "experiment.completed"
