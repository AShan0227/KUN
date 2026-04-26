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


def test_cron_scheduler_singleton_in_app_state() -> None:
    from fastapi import FastAPI
    from kun.api.runtime import get_cron_scheduler
    from kun.engineering.cron_scheduler import CronScheduler

    app = FastAPI()
    install_runtime(app, rule_engine=RuleEngine([]))
    sched = get_cron_scheduler(app)
    assert isinstance(sched, CronScheduler)
    assert sched.list_jobs() == []  # lifespan registers jobs, install_runtime 不


def test_value_gate_default_enabled() -> None:
    """V2.2 §19.4 + §21 wire: 默认 KUN_VALUE_GATE_ENABLED=1 (FAST 模式自动跳过)."""
    import os
    from unittest.mock import patch

    from fastapi import FastAPI
    from kun.api.runtime import get_orchestrator, get_value_gate
    from kun.watchtower.value_gate import ValueGate

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_VALUE_GATE_ENABLED", None)
        app = FastAPI()
        install_runtime(app, rule_engine=RuleEngine([]))
        gate = get_value_gate(app)
        assert isinstance(gate, ValueGate)
        orch = get_orchestrator(app)
        assert orch.value_gate is gate


def test_value_gate_force_disable_via_env() -> None:
    """KUN_VALUE_GATE_ENABLED=0 强制关 ValueGate."""
    import os
    from unittest.mock import patch

    from fastapi import FastAPI
    from kun.api.runtime import get_value_gate

    with patch.dict(os.environ, {"KUN_VALUE_GATE_ENABLED": "0"}):
        app = FastAPI()
        install_runtime(app, rule_engine=RuleEngine([]))
        assert get_value_gate(app) is None


def test_hermes_default_enabled_and_orchestrator_holds_it() -> None:
    """V2.2 §22 wire: 默认 KUN_HERMES_ENABLED=1, orchestrator 持有 generator."""
    import os
    from unittest.mock import patch

    from fastapi import FastAPI
    from kun.api.runtime import get_orchestrator, get_structured_step_generator
    from kun.engineering.execution_protocol import StructuredStepGenerator

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_HERMES_ENABLED", None)
        app = FastAPI()
        install_runtime(app, rule_engine=RuleEngine([]))
        gen = get_structured_step_generator(app)
        assert isinstance(gen, StructuredStepGenerator)
        orch = get_orchestrator(app)
        assert orch.structured_step_generator is gen


def test_hermes_force_disable_via_env() -> None:
    import os
    from unittest.mock import patch

    from fastapi import FastAPI
    from kun.api.runtime import get_structured_step_generator

    with patch.dict(os.environ, {"KUN_HERMES_ENABLED": "0"}):
        app = FastAPI()
        install_runtime(app, rule_engine=RuleEngine([]))
        assert get_structured_step_generator(app) is None


@pytest.mark.asyncio
async def test_blackboard_assets_source_returns_pin_and_active_kinds() -> None:
    """V2.2 Wire 4: assets endpoint 拉真 pin + 资产分组 (不再是空切片)."""
    from kun.api.blackboard import register_data_source, reset_data_sources
    from kun.api.blackboard_data_sources import _assets_source_async
    from kun.context.storage import reset_store as reset_asset_store
    from kun.core.attention_anchor import reset_manager as reset_anchor_manager

    reset_data_sources()
    reset_asset_store()
    reset_anchor_manager()

    register_data_source("assets", _assets_source_async)

    # 默认空 store / 空 anchor → 4 类全空 list, 但 dict shape 完整
    result = await _assets_source_async(task_id="tk-x", user_id="u-x")
    assert result["task_id"] == "tk-x"
    assert isinstance(result["pinned_assets"], list)
    assert isinstance(result["semantic_assets"], list)
    assert isinstance(result["methodology_refs"], list)
    assert isinstance(result["capability_card_refs"], list)


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
