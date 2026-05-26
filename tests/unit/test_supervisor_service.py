"""L2.1 · SupervisorService 工程化阈值检测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.supervisor import SupervisorService


def _make_emitter() -> tuple[list[dict[str, Any]], Any]:
    """Return (sink_list, async_emitter) — emitter appends to sink."""
    sink: list[dict[str, Any]] = []

    async def emit(payload: dict[str, Any]) -> None:
        sink.append(payload)

    return sink, emit


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_spike_below_threshold_does_not_trigger():
    """1-2 次 fallback (低于阈值 3) → 不触发 search request."""
    sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {"tenant_id": "u-test", "primary_provider": "claude-cli"}
    triggered1 = await svc.observe("llm.fallback.triggered", payload)
    triggered2 = await svc.observe("llm.fallback.triggered", payload)

    assert triggered1 == []
    assert triggered2 == []
    assert sink == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_spike_at_threshold_triggers_search_request():
    """连续 3 次 fallback → 触发 strategy_search_request."""
    sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {
        "tenant_id": "u-test",
        "primary_provider": "claude-cli",
        "primary_model": "claude-code-haiku",
        "reason": "RetryError",
    }
    for _ in range(3):
        triggered = await svc.observe("llm.fallback.triggered", payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert req["triggered_by"] == "anomaly_threshold"
    assert req["target_module"] == "claude-cli"
    assert req["anomaly_kind"] == "llm_fallback_spike"
    assert req["priority"] == "medium"
    assert req["status"] == "open"
    assert req["tenant_id"] == "u-test"
    assert req["evidence"][0]["count"] == 3
    # emitter 也收到了
    assert len(sink) == 1
    assert sink[0]["request_id"].startswith("ss-")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_spike_triggers_high_priority():
    """连续 3 次 task.failed → 触发 high priority search request."""
    _sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {"tenant_id": "u-test", "task_type": "coding.refactor"}
    for _ in range(3):
        triggered = await svc.observe("task.failed", payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert req["priority"] == "high"
    assert req["target_module"] == "executor.coding.refactor"
    assert req["anomaly_kind"] == "task_failure_spike"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anomaly_score_above_threshold_triggers():
    """task_anomaly_score >= 0.6 → 触发 search request."""
    _sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {
        "tenant_id": "u-test",
        "task_type": "writing.report",
        "task_id": "tk-test123",
        "task_anomaly_score": 0.75,
    }
    triggered = await svc.observe("task.done", payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert req["anomaly_kind"] == "task_anomaly_spike"
    assert req["evidence"][0]["score"] == 0.75


@pytest.mark.unit
@pytest.mark.asyncio
async def test_anomaly_score_below_threshold_does_not_trigger():
    """task_anomaly_score < 0.6 → 不触发."""
    _sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {
        "tenant_id": "u-test",
        "task_type": "writing.report",
        "task_anomaly_score": 0.3,
    }
    triggered = await svc.observe("task.done", payload)
    assert triggered == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duration_outlier_triggers_low_priority():
    """duration > 3x 平均 → 触发 low priority outlier."""
    _sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    payload = {
        "tenant_id": "u-test",
        "task_type": "writing.report",
        "duration_sec": 120.0,
        "avg_duration_sec_for_task_type": 30.0,
    }
    triggered = await svc.observe("task.done", payload)
    # duration=120, avg=30 → ratio=4.0 > 3.0 阈值
    duration_reqs = [r for r in triggered if r["anomaly_kind"] == "duration_outlier"]
    assert len(duration_reqs) == 1
    req = duration_reqs[0]
    assert req["priority"] == "low"
    assert req["evidence"][0]["ratio"] == 4.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_prevents_duplicate_within_ttl():
    """同 tenant + 同 target_module + 同 anomaly_kind 在 dedup_ttl 内只触发一次."""
    sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter, dedup_ttl_sec=3600)

    payload = {
        "tenant_id": "u-test",
        "primary_provider": "claude-cli",
        "primary_model": "claude-code-haiku",
    }
    # 第一波: 3 次 fallback → 触发 1 次
    for _ in range(3):
        await svc.observe("llm.fallback.triggered", payload)
    # 第二波: 又 3 次 → dedup 跳过, 不触发
    for _ in range(3):
        await svc.observe("llm.fallback.triggered", payload)

    # 累计只有 1 个 search request
    assert len(sink) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_isolation():
    """不同 tenant 各自累积, 互不影响."""
    sink, emitter = _make_emitter()
    svc = SupervisorService(emitter=emitter)

    for _ in range(3):
        await svc.observe("task.failed", {"tenant_id": "u-a", "task_type": "x"})
    for _ in range(2):
        await svc.observe("task.failed", {"tenant_id": "u-b", "task_type": "x"})

    # u-a 触发 (3 次 >= 阈值), u-b 不触发 (2 次)
    assert len(sink) == 1
    assert sink[0]["tenant_id"] == "u-a"
