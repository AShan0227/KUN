"""L3.1 — 第 2 条 RSI 实例: context 压缩 策略 单测.

Supervisor 检测 context_oversized_spike + Strategist 提 3 个 Explorer 候选.
"""

from __future__ import annotations

import pytest
from kun.agents.strategist.service import StrategistService
from kun.agents.supervisor.service import SupervisorService

# ---- Supervisor context_oversized 检测 ----


@pytest.mark.asyncio
async def test_context_oversized_below_token_threshold_no_trigger() -> None:
    svc = SupervisorService()
    triggered = await svc.observe(
        "llm.invoke.completed",
        {"tenant_id": "u-test", "input_tokens": 10_000},  # 远低于 80k
    )
    assert triggered == []


@pytest.mark.asyncio
async def test_context_oversized_single_oversized_event_no_trigger() -> None:
    """单次超阈不触发 — 需要窗口内 ≥ 3 次."""
    svc = SupervisorService()
    triggered = await svc.observe(
        "llm.invoke.completed",
        {"tenant_id": "u-test", "input_tokens": 100_000},
    )
    assert triggered == []


@pytest.mark.asyncio
async def test_context_oversized_three_in_window_triggers() -> None:
    svc = SupervisorService()
    payload = {"tenant_id": "u-test", "input_tokens": 100_000, "provider": "anthropic"}
    for _ in range(2):
        await svc.observe("llm.invoke.completed", payload)
    triggered = await svc.observe("llm.invoke.completed", payload)
    assert len(triggered) == 1
    req = triggered[0]
    assert req["anomaly_kind"] == "context_oversized_spike"
    assert req["target_module"] == "llm.context"
    assert req["evidence"][0]["oversized_count"] == 3
    assert req["evidence"][0]["latest_input_tokens"] == 100_000
    assert req["priority"] == "medium"


@pytest.mark.asyncio
async def test_context_oversized_dedup_within_ttl() -> None:
    """同 tenant + 同 target_module + 同 kind 在 dedup_ttl 内不重复触发."""
    svc = SupervisorService()
    payload = {"tenant_id": "u-test", "input_tokens": 100_000}
    for _ in range(3):
        await svc.observe("llm.invoke.completed", payload)
    # 第 4 次仍 oversized 但已 dedup
    triggered = await svc.observe("llm.invoke.completed", payload)
    assert triggered == []


@pytest.mark.asyncio
async def test_context_oversized_custom_threshold() -> None:
    """单 tenant state 可定制 threshold."""
    svc = SupervisorService()
    state = svc.state_for("u-test")
    state.context_oversized_input_tokens = 50_000
    state.context_oversized_threshold = 2

    payload = {"tenant_id": "u-test", "input_tokens": 60_000}
    await svc.observe("llm.invoke.completed", payload)
    triggered = await svc.observe("llm.invoke.completed", payload)
    assert len(triggered) == 1


@pytest.mark.asyncio
async def test_other_event_types_dont_affect_context_count() -> None:
    """非 llm.invoke.completed 事件不计入 oversized_count."""
    svc = SupervisorService()
    # 跑一些 task.failed 触发其他逻辑, 不应混入 context 检测
    await svc.observe("task.failed", {"tenant_id": "u-test"})
    payload = {"tenant_id": "u-test", "input_tokens": 100_000}
    # 单次 oversized 不应触发
    triggered = await svc.observe("llm.invoke.completed", payload)
    assert triggered == []


# ---- Strategist context_oversized_spike 候选 ----


@pytest.mark.asyncio
async def test_strategist_yields_three_compression_candidates() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "context_oversized_spike",
            "target_module": "llm.context",
            "evidence": [
                {
                    "type": "input_tokens_spike",
                    "oversized_count": 5,
                    "threshold_tokens": 80_000,
                    "latest_input_tokens": 120_000,
                }
            ],
        }
    )
    assert len(candidates) == 3
    modes = {c.explorer_mode for c in candidates}
    assert modes == {"conservative", "aggressive", "performance"}

    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    assert conservative.change_spec["kind"] == "context_summary_compression"
    assert conservative.rollout_mode == "canary"

    aggressive = next(c for c in candidates if c.explorer_mode == "aggressive")
    assert aggressive.change_spec["kind"] == "context_hard_truncation"
    assert aggressive.change_spec["preserve_top_pins"] is True
    # 必须保 anchor pinning (ADR-022 Layer 2)

    performance = next(c for c in candidates if c.explorer_mode == "performance")
    assert performance.change_spec["kind"] == "context_rag_retrieval"
    assert performance.rollout_mode == "shadow"


@pytest.mark.asyncio
async def test_strategist_uses_evidence_threshold_to_size_acceptance() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "context_oversized_spike",
            "target_module": "llm.context",
            "evidence": [{"threshold_tokens": 50_000, "latest_input_tokens": 70_000}],
        }
    )
    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    # acceptance 应基于 evidence 的 threshold 而非默认
    assert conservative.acceptance_threshold == 30_000.0  # 50_000 * 0.6


@pytest.mark.asyncio
async def test_strategist_handles_missing_evidence() -> None:
    """evidence 为空 — fallback 到默认 80k."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "context_oversized_spike",
            "target_module": "llm.context",
            "evidence": [],
        }
    )
    assert len(candidates) == 3
    conservative = next(c for c in candidates if c.explorer_mode == "conservative")
    # 默认 80_000 * 0.6 = 48_000
    assert conservative.acceptance_threshold == 48_000.0


@pytest.mark.asyncio
async def test_strategist_context_candidates_have_rollback_triggers() -> None:
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "context_oversized_spike",
            "target_module": "llm.context",
            "evidence": [{"threshold_tokens": 80_000}],
        }
    )
    for c in candidates:
        assert c.rollback_on  # 每个候选必带 rollback 触发器
        # Conservative 必带 task_success_rate 保护
        if c.explorer_mode == "conservative":
            metrics = {r["metric"] for r in c.rollback_on}
            assert "task_success_rate" in metrics
