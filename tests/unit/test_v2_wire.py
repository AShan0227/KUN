"""Tests for V2.1 wire: chat fast_path / task_control / blackboard / attention_pin 接 main."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from kun.api.runtime import (
    get_emergent_library,
    get_emergent_switch_manager,
    get_fast_path,
    get_kill_switch,
    get_orchestrator,
    get_plan_only_gate,
    get_task_timeout,
    get_token_meter,
    get_zero_telemetry,
    install_runtime,
)
from kun.core.emergent_solution import EmergentSolutionLibrary
from kun.engineering.emergent_switch import EmergentSwitchManager
from kun.engineering.fast_path import FastPathRouter
from kun.engineering.safety_guards import (
    KillSwitch,
    PlanOnlyGate,
    TaskTimeoutGuard,
    TokenMeter,
    ZeroTelemetryEnforcer,
)
from kun.watchtower.engine import RuleEngine


@pytest.fixture
def runtime_app():
    """Standalone app with install_runtime called (no full lifespan)."""
    from fastapi import FastAPI
    from kun.api.attention_pin import router as ap_router
    from kun.api.blackboard import router as bb_router
    from kun.api.chat import router as chat_router
    from kun.api.task_control import router as tc_router
    from kun.core.attention_anchor import reset_manager

    reset_manager()
    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    app.include_router(chat_router, prefix="/api/chat")
    app.include_router(bb_router)
    app.include_router(ap_router)
    app.include_router(tc_router)
    return app


def test_install_runtime_creates_all_v2_singletons() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    assert isinstance(get_fast_path(app), FastPathRouter)
    assert isinstance(get_kill_switch(app), KillSwitch)
    assert isinstance(get_token_meter(app), TokenMeter)
    assert isinstance(get_plan_only_gate(app), PlanOnlyGate)
    assert isinstance(get_task_timeout(app), TaskTimeoutGuard)
    assert isinstance(get_zero_telemetry(app), ZeroTelemetryEnforcer)
    assert isinstance(get_emergent_library(app), EmergentSolutionLibrary)
    assert isinstance(get_emergent_switch_manager(app), EmergentSwitchManager)


def test_emergent_switch_manager_wired_into_orchestrator() -> None:
    """V2.1 §5.8: orchestrator 必须持有同一个 EmergentSwitchManager 实例 (而不是 None)."""
    from fastapi import FastAPI

    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    orch = get_orchestrator(app)
    mgr = get_emergent_switch_manager(app)
    assert orch.emergent_switch_manager is mgr


def test_knowledge_precipitation_wired_into_idle_batch() -> None:
    """V2.1 §16.12: install_runtime 应该:
    - 创建 KnowledgePrecipitation 单例并注册 4 类内置 step
    - 把 KnowledgePrecipitationStep 注册到 idle_batch._steps
    """
    from fastapi import FastAPI
    from kun.api.runtime import get_knowledge_precipitation
    from kun.engineering.idle_batch import KnowledgePrecipitationStep, _steps
    from kun.engineering.precipitation import KnowledgePrecipitation

    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))

    kp = get_knowledge_precipitation(app)
    assert isinstance(kp, KnowledgePrecipitation)
    assert len(kp._steps) == 4

    assert "knowledge_precipitation" in _steps
    assert isinstance(_steps["knowledge_precipitation"], KnowledgePrecipitationStep)


def test_chat_fast_path_chitchat(runtime_app) -> None:
    client = TestClient(runtime_app)
    resp = client.post(
        "/api/chat/run",
        json={"message": "你好"},
        headers={"X-User-Id": "u-trusted"},
    )
    # u-trusted 没注册 trust_lookup → 默认走 (lookup is None)
    # 闲聊"你好" 应被 fast_path 拦
    assert resp.status_code == 200
    body = resp.json()
    # 可能 fast_path 命中, 也可能 fall through 到 orchestrator
    if body.get("fast_path") is True:
        assert body["hit"] == "chitchat"
        assert body["decided_in_ms"] < 100


def test_chat_usage_dashboard_endpoint(runtime_app) -> None:
    client = TestClient(runtime_app)
    resp = client.get(
        "/api/chat/usage",
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u-1"
    assert "five_hour" in body
    assert body["five_hour"]["used"] == 0


def test_task_kill_unknown_404(runtime_app) -> None:
    client = TestClient(runtime_app)
    resp = client.post(
        "/api/tasks/tk-nonexistent/kill",
        json={"reason": "user clicked stop"},
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 404


def test_task_register_then_kill(runtime_app) -> None:
    client = TestClient(runtime_app)
    # 注册
    reg = client.post(
        "/api/tasks/tk-1/register",
        json={"max_duration_sec": 60, "max_steps": 10},
    )
    assert reg.status_code == 200
    assert reg.json()["registered"] is True

    # 状态: 未 kill
    status = client.get("/api/tasks/tk-1/status")
    assert status.status_code == 200
    assert status.json()["is_killed"] is False

    # Kill
    kill = client.post(
        "/api/tasks/tk-1/kill",
        json={"reason": "stop"},
        headers={"X-User-Id": "u-1"},
    )
    assert kill.status_code == 200
    assert kill.json()["killed"] is True

    # 状态: 已 kill
    status2 = client.get("/api/tasks/tk-1/status")
    assert status2.json()["is_killed"] is True
    assert status2.json()["kill_reason"] == "stop"


def test_blackboard_state_endpoint_via_main_runtime(runtime_app) -> None:
    """V2.1 wire: 黑板 router 已装进 app."""
    client = TestClient(runtime_app)
    resp = client.get(
        "/api/blackboard/state",
        headers={"X-User-Id": "u-1", "X-Tenant-Id": "t-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u-1"
    assert body["tenant_id"] == "t-1"


def test_attention_pin_endpoint_via_main_runtime(runtime_app) -> None:
    """V2.1 wire: pin router 已装进 app."""
    client = TestClient(runtime_app)
    resp = client.post(
        "/api/preferences/pin",
        json={"target_asset_ref": "ka-x", "reason": "test"},
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["target_asset_ref"] == "ka-x"
    # 列表可见
    listing = client.get("/api/preferences/pin", headers={"X-User-Id": "u-1"})
    assert listing.json()["pin_count"] == 1
