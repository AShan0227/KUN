"""L4.5 — Resource Quota 单测."""

from __future__ import annotations

import asyncio

import pytest
from kun.agents.strategist.service import StrategistService
from kun.governance.resource_quota import (
    DEFAULT_EXPERIMENT_BUDGET_PER_HOUR,
    DEFAULT_TOKEN_BUDGET_PER_HOUR,
    QuotaCheckResult,
    ResourceQuota,
)

# ---- ResourceQuota basics ----


def test_defaults_reasonable() -> None:
    rq = ResourceQuota()
    assert rq._token_budget == DEFAULT_TOKEN_BUDGET_PER_HOUR
    assert rq._experiment_budget == DEFAULT_EXPERIMENT_BUDGET_PER_HOUR


def test_invalid_budgets_rejected() -> None:
    with pytest.raises(ValueError):
        ResourceQuota(token_budget_per_window=-1)
    with pytest.raises(ValueError):
        ResourceQuota(experiment_budget_per_window=-1)
    with pytest.raises(ValueError):
        ResourceQuota(window_seconds=0)


@pytest.mark.asyncio
async def test_first_experiment_allowed() -> None:
    rq = ResourceQuota(experiment_budget_per_window=10)
    result = await rq.check_and_record_experiment("u-test", "er-1")
    assert result.allowed
    assert result.current_experiment_count == 1


@pytest.mark.asyncio
async def test_experiment_budget_exceeded() -> None:
    rq = ResourceQuota(experiment_budget_per_window=2)
    r1 = await rq.check_and_record_experiment("u-test", "er-1")
    r2 = await rq.check_and_record_experiment("u-test", "er-2")
    r3 = await rq.check_and_record_experiment("u-test", "er-3")
    assert r1.allowed
    assert r2.allowed
    assert r3.allowed is False
    assert "experiment_budget_exceeded" in r3.reason


@pytest.mark.asyncio
async def test_token_budget_exceeded() -> None:
    rq = ResourceQuota(
        token_budget_per_window=1000, experiment_budget_per_window=100
    )
    r1 = await rq.check_and_record_experiment(
        "u-test", "er-1", estimated_tokens=500
    )
    r2 = await rq.check_and_record_experiment(
        "u-test", "er-2", estimated_tokens=400
    )
    r3 = await rq.check_and_record_experiment(
        "u-test", "er-3", estimated_tokens=200
    )
    assert r1.allowed
    assert r2.allowed
    assert r3.allowed is False
    assert "token_budget_exceeded" in r3.reason


@pytest.mark.asyncio
async def test_record_token_usage_post_call() -> None:
    rq = ResourceQuota(token_budget_per_window=500)
    await rq.record_token_usage("u-test", 200)
    await rq.record_token_usage("u-test", 200)
    snap = await rq.snapshot("u-test")
    assert snap["current_token_usage"] == 400
    # 下次 experiment 仅剩 100 token 预算
    r = await rq.check_and_record_experiment(
        "u-test", "er-1", estimated_tokens=200
    )
    assert r.allowed is False


@pytest.mark.asyncio
async def test_tenant_isolation() -> None:
    rq = ResourceQuota(experiment_budget_per_window=1)
    r1 = await rq.check_and_record_experiment("u-a", "er-1")
    r2 = await rq.check_and_record_experiment("u-b", "er-2")
    assert r1.allowed
    assert r2.allowed  # 不同 tenant 各自独立 budget
    r3 = await rq.check_and_record_experiment("u-a", "er-3")
    assert r3.allowed is False


@pytest.mark.asyncio
async def test_record_zero_or_negative_token_ignored() -> None:
    rq = ResourceQuota()
    await rq.record_token_usage("u-test", 0)
    await rq.record_token_usage("u-test", -100)
    snap = await rq.snapshot("u-test")
    assert snap["current_token_usage"] == 0


@pytest.mark.asyncio
async def test_window_purge_on_check() -> None:
    """超过窗口的记录应被自动清理."""
    rq = ResourceQuota(
        experiment_budget_per_window=2, window_seconds=1
    )
    await rq.check_and_record_experiment("u-test", "er-1")
    await rq.check_and_record_experiment("u-test", "er-2")
    # 满, 第 3 个拒
    r = await rq.check_and_record_experiment("u-test", "er-3")
    assert r.allowed is False
    # 等过窗口
    await asyncio.sleep(1.1)
    # 新 check 应清旧记录 → 重新可用
    r = await rq.check_and_record_experiment("u-test", "er-4")
    assert r.allowed


@pytest.mark.asyncio
async def test_quota_check_result_carries_budgets() -> None:
    rq = ResourceQuota(
        token_budget_per_window=1000, experiment_budget_per_window=5
    )
    r = await rq.check_and_record_experiment(
        "u-test", "er-1", estimated_tokens=100
    )
    assert isinstance(r, QuotaCheckResult)
    assert r.token_budget == 1000
    assert r.experiment_budget == 5
    assert r.current_token_usage == 100
    assert r.current_experiment_count == 1


# ---- StrategistService 集成 ----


@pytest.mark.asyncio
async def test_strategist_uses_quota_to_filter_candidates() -> None:
    """quota 注入后, experiment budget=0 时所有候选被拒."""
    rq = ResourceQuota(experiment_budget_per_window=0)
    svc = StrategistService(resource_quota=rq)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_strategist_passes_some_when_budget_partial() -> None:
    """budget=1 时只有 1 个 candidate 通过."""
    rq = ResourceQuota(experiment_budget_per_window=1)
    svc = StrategistService(resource_quota=rq)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    # 仅 1 个通过 quota
    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_strategist_no_quota_unchanged_behavior() -> None:
    """无 quota 注入时与之前行为一致."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 3
