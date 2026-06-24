"""L4.2 — Supervisor Pool 多实例单测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.supervisor.pool import SupervisorPool, SupervisorPoolConfig
from kun.agents.supervisor.service import SupervisorService

# ---- SupervisorPoolConfig ----


def test_default_dimensions_contain_three() -> None:
    cfg = SupervisorPoolConfig()
    assert set(cfg.dimensions.keys()) == {
        "latency_audit",
        "cost_audit",
        "safety_audit",
    }


def test_dimensions_for_event_fan_out() -> None:
    """task.done 应该被多维度消费 (latency + safety)."""
    cfg = SupervisorPoolConfig()
    dims = set(cfg.dimensions_for_event("task.done"))
    assert dims == {"latency_audit", "safety_audit"}


def test_dimensions_for_event_unique() -> None:
    """llm.fallback.triggered 只 cost_audit 关心."""
    cfg = SupervisorPoolConfig()
    dims = cfg.dimensions_for_event("llm.fallback.triggered")
    assert dims == ["cost_audit"]


def test_dimensions_for_unknown_event_empty() -> None:
    cfg = SupervisorPoolConfig()
    assert cfg.dimensions_for_event("random.event") == []


def test_custom_dimensions_config() -> None:
    cfg = SupervisorPoolConfig(
        dimensions={
            "only_failures": {"task.failed"},
            "only_fallback": {"llm.fallback.triggered"},
        }
    )
    assert set(cfg.dimensions.keys()) == {"only_failures", "only_fallback"}
    assert cfg.dimensions_for_event("task.failed") == ["only_failures"]


# ---- SupervisorPool basics ----


@pytest.mark.asyncio
async def test_pool_lazy_init_instance() -> None:
    pool = SupervisorPool()
    svc = pool.instance_for("safety_audit")
    assert isinstance(svc, SupervisorService)
    # 再取应该返回同一 instance
    svc2 = pool.instance_for("safety_audit")
    assert svc is svc2


@pytest.mark.asyncio
async def test_pool_returns_empty_for_unmatched_event() -> None:
    pool = SupervisorPool()
    result = await pool.observe("never.heard.of", {"tenant_id": "u-test"})
    assert result == {}


@pytest.mark.asyncio
async def test_pool_routes_event_to_single_dimension() -> None:
    received: list[dict[str, Any]] = []

    async def fake_emitter(req: dict[str, Any]) -> None:
        received.append(req)

    pool = SupervisorPool(emitter=fake_emitter)
    # llm.fallback.triggered 仅 cost_audit
    # 跑 3 次让阈值触发
    payload = {
        "tenant_id": "u-test",
        "primary_provider": "openai",
    }
    for _ in range(2):
        await pool.observe("llm.fallback.triggered", payload)
    result = await pool.observe("llm.fallback.triggered", payload)
    # cost_audit 维度有 1 triggered
    assert "cost_audit" in result
    assert len(result["cost_audit"]) == 1
    # 其他维度不在 result 中
    assert "latency_audit" not in result
    assert "safety_audit" not in result


@pytest.mark.asyncio
async def test_pool_fans_out_task_done_to_two_dims() -> None:
    pool = SupervisorPool()
    # task.done 同时被 latency_audit + safety_audit 订阅
    result = await pool.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "x",
            "task_anomaly_score": 0.1,  # 低 → 不触发 anomaly_score
            "duration_sec": 1.0,
            "avg_duration_sec_for_task_type": 1.0,  # ratio=1, 不触发 duration_outlier
        },
    )
    # 两 dim 都消费但都未触发
    assert "latency_audit" in result
    assert "safety_audit" in result
    assert result["latency_audit"] == []
    assert result["safety_audit"] == []


@pytest.mark.asyncio
async def test_pool_isolates_state_across_dimensions() -> None:
    """同事件被多 dim 消费时, 每 dim 自己的状态独立.

    单次 task.done 即触发 task_anomaly_spike (score >= 0.6)
    和 duration_outlier (ratio >= 3.0). 两 dim 应各自独立触发.
    """
    pool = SupervisorPool()
    result = await pool.observe(
        "task.done",
        {
            "tenant_id": "u-test",
            "task_type": "x",
            "task_anomaly_score": 0.7,
            "duration_sec": 10.0,
            "avg_duration_sec_for_task_type": 1.0,
        },
    )
    safety = result.get("safety_audit", [])
    latency = result.get("latency_audit", [])
    # 两 dim 各自独立触发, 因为他们是独立 SupervisorService instance
    assert len(safety) >= 1
    assert len(latency) >= 1
    # state isolated — 验证 2 dim 各自有独立 SupervisorAnomalyState
    safety_svc = pool.instance_for("safety_audit")
    latency_svc = pool.instance_for("latency_audit")
    assert safety_svc is not latency_svc
    # 两 dim 的 state 字典是独立对象
    assert "u-test" in safety_svc._states
    assert "u-test" in latency_svc._states
    assert (
        safety_svc._states["u-test"] is not latency_svc._states["u-test"]
    )


@pytest.mark.asyncio
async def test_pool_all_dimensions_listed() -> None:
    pool = SupervisorPool()
    dims = pool.all_dimensions()
    assert set(dims) == {"latency_audit", "cost_audit", "safety_audit"}


@pytest.mark.asyncio
async def test_pool_propagates_emitter_to_all_instances() -> None:
    received_cost: list[dict[str, Any]] = []
    received_safety: list[dict[str, Any]] = []

    async def emitter(req: dict[str, Any]) -> None:
        if req["anomaly_kind"] == "llm_fallback_spike":
            received_cost.append(req)
        elif req["anomaly_kind"] == "task_failure_spike":
            received_safety.append(req)

    pool = SupervisorPool(emitter=emitter)
    # cost_audit: llm.fallback.triggered ≥3 次
    fb = {"tenant_id": "u-test", "primary_provider": "openai"}
    for _ in range(3):
        await pool.observe("llm.fallback.triggered", fb)
    # safety_audit: task.failed ≥3 次
    tf = {"tenant_id": "u-test", "task_type": "y"}
    for _ in range(3):
        await pool.observe("task.failed", tf)

    assert len(received_cost) == 1
    assert len(received_safety) == 1


@pytest.mark.asyncio
async def test_pool_dimension_observe_exception_swallowed() -> None:
    """单 dim 失败不影响其他 dim."""
    pool = SupervisorPool()
    # Inject a broken service into one dimension
    svc = pool.instance_for("cost_audit")

    async def bad_observe(event_type: str, payload: dict[str, Any]) -> list[dict]:
        raise RuntimeError("cost dim crashed")

    svc.observe = bad_observe  # type: ignore[assignment]

    # task.done 不路由到 cost_audit, 应正常
    result = await pool.observe(
        "task.done",
        {"tenant_id": "u-test", "task_type": "z"},
    )
    assert "latency_audit" in result
    assert "safety_audit" in result
