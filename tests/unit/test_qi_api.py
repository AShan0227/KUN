"""V2.3 启 (Qi) HTTP API tests."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from kun.api.qi import _qi_window_active
from kun.api.qi import router as qi_router
from starlette.datastructures import State


def _app_with_state():
    from fastapi import FastAPI

    app = FastAPI()
    app.state = State()
    app.include_router(qi_router)
    return app


def test_qi_window_active_force_true() -> None:
    app = SimpleNamespace(state=State())
    req = SimpleNamespace(app=app)
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        assert _qi_window_active(req) is True


def test_qi_window_active_force_disable() -> None:
    app = SimpleNamespace(state=State())
    req = SimpleNamespace(app=app)
    with patch.dict(os.environ, {"KUN_QI_FORCE_DISABLE": "1"}, clear=False):
        assert _qi_window_active(req) is False


def test_qi_status_endpoint_no_runtime() -> None:
    """Backend without qi_budget loaded → returns 0s gracefully."""
    app = _app_with_state()
    client = TestClient(app)
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        r = client.get("/api/qi/status")
    assert r.status_code == 200
    data = r.json()
    assert data["window_active"] is True
    assert data["daily_limit_usd"] == 0.0
    assert data["protocol_count"] == 0


def test_qi_status_endpoint_with_runtime() -> None:
    from kun.qi import QiDailyBudget
    from kun.qi.pheromone import InMemoryPheromoneStorage
    from kun.qi.protocol import InMemoryProtocolStorage, ProtocolRegistry

    app = _app_with_state()
    budget = QiDailyBudget()
    budget.set_daily_limit(7.5)
    app.state.qi_budget = budget
    app.state.protocol_registry = ProtocolRegistry(InMemoryProtocolStorage())
    app.state.pheromone_storage = InMemoryPheromoneStorage()

    client = TestClient(app)
    r = client.get("/api/qi/status")
    assert r.status_code == 200
    data = r.json()
    assert data["daily_limit_usd"] == 7.5
    assert data["remaining_usd"] == 7.5  # 没花钱
    assert data["protocol_count"] == 0  # 空 registry


def test_qi_status_reads_existing_problem_queue_without_sampling() -> None:
    from kun.qi.problem_queue import QiProblemQueue, QiProblemSignal

    app = _app_with_state()
    queue = QiProblemQueue()
    queue.enqueue(
        QiProblemSignal.build(
            tenant_id="u-sylvan",
            category="world_gateway",
            severity="warn",
            summary="WorldGateway handler 缺补偿",
            source="test",
        )
    )
    app.state.qi_problem_queue = queue

    client = TestClient(app)
    r = client.get("/api/qi/status")

    assert r.status_code == 200
    data = r.json()
    assert data["problem_signal_count"] == 1
    assert data["top_problem"] == "WorldGateway handler 缺补偿"


def test_qi_force_active_endpoint() -> None:
    app = _app_with_state()
    client = TestClient(app)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
        r = client.post("/api/qi/force_active")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_qi_release_endpoint() -> None:
    app = _app_with_state()
    client = TestClient(app)
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        r = client.post("/api/qi/release")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_qi_trigger_explore_unknown_job() -> None:
    app = _app_with_state()
    client = TestClient(app)
    r = client.post("/api/qi/trigger_explore", json={"job": "unknown"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "unknown job" in body["error"]
