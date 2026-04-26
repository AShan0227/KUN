"""Tests for StrategyMatcher (V2.1 §17 / ADR-019)."""

from __future__ import annotations

import pytest
from kun.core.strategy_matcher import (
    WEIGHT_TABLE,
    SignalBundle,
    StrategyCandidate,
    StrategyMatcher,
    Weights,
    get_matcher,
    reset_matcher,
)


def test_weights_normalize() -> None:
    w = Weights(alpha=2.0, beta=2.0, gamma=2.0, delta=2.0).normalize()
    assert abs(w.alpha + w.beta + w.gamma + w.delta - 1.0) < 1e-6
    assert w.alpha == w.beta == w.gamma == w.delta == 0.25


def test_signal_bundle_get() -> None:
    sb = SignalBundle(
        task={"task_type": "coding.py", "risk_level": "high"},
        user={"audience": "developer"},
    )
    assert sb.get("task_type") == "coding.py"
    assert sb.get("audience") == "developer"
    assert sb.get("missing", default="x") == "x"
    assert sb.get_risk_level() == "high"


def test_compute_weights_critical_locks_alpha() -> None:
    sb = SignalBundle(task={"risk_level": "critical"})
    m = StrategyMatcher()
    w = m.compute_weights(sb)
    # critical 锚定 α=0.7
    assert w.alpha >= 0.5  # critical 不能被压到 < 0.5


def test_compute_weights_user_cost_sensitivity_high() -> None:
    sb = SignalBundle(
        task={"risk_level": "low"},
        user={"cost_sensitivity": "high"},
    )
    m = StrategyMatcher()
    w = m.compute_weights(sb)
    base = WEIGHT_TABLE["low"]
    # cost_sensitivity=high 应该让 β 比基线高
    assert w.beta > base.beta


def test_score_basic() -> None:
    m = StrategyMatcher()
    candidate = StrategyCandidate(
        candidate_id="c1",
        description="test",
        expected_outcome=0.8,
        expected_cost_usd=0.0,  # 订阅 free
        expected_latency_sec=2.0,
        risk_penalty=0.0,
    )
    weights = Weights(alpha=0.5, beta=0.2, gamma=0.2, delta=0.1)
    scored = m.score(candidate, weights)
    # outcome=0.5*0.8=0.4 / cost=0 / latency=0.2*(2/60)≈0.0067 / risk=0
    assert scored.score > 0.3
    assert "outcome_term" in scored.score_breakdown


@pytest.mark.asyncio
async def test_decide_picks_highest_score() -> None:
    m = StrategyMatcher()

    async def enum(_sb, _prev):
        return [
            StrategyCandidate(
                candidate_id="cheap",
                description="cheap fast",
                expected_outcome=0.5,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            ),
            StrategyCandidate(
                candidate_id="expensive",
                description="expensive slow",
                expected_outcome=0.95,
                expected_cost_usd=0.5,
                expected_latency_sec=10.0,
            ),
        ]

    m.register("model_select", enum)
    sb = SignalBundle(task={"risk_level": "low"})
    decision = await m.decide("model_select", sb)
    # low risk: γ=0.40 高, expensive 因为 latency 大被压;
    # cheap 应该胜出
    assert decision.chosen.candidate_id == "cheap"
    assert len(decision.runners_up) == 1


@pytest.mark.asyncio
async def test_decide_critical_picks_high_outcome() -> None:
    m = StrategyMatcher()

    async def enum(_sb, _prev):
        return [
            StrategyCandidate(
                candidate_id="cheap_low_outcome",
                description="便宜但效果差",
                expected_outcome=0.5,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            ),
            StrategyCandidate(
                candidate_id="expensive_high_outcome",
                description="贵但效果好",
                expected_outcome=0.95,
                expected_cost_usd=0.5,
                expected_latency_sec=10.0,
            ),
        ]

    m.register("model_select", enum)
    sb = SignalBundle(task={"risk_level": "critical"})
    decision = await m.decide("model_select", sb)
    # critical: α=0.7, expensive_high_outcome 应胜
    assert decision.chosen.candidate_id == "expensive_high_outcome"


@pytest.mark.asyncio
async def test_decide_empty_candidates_raises() -> None:
    m = StrategyMatcher()

    async def enum(_sb, _prev):
        return []

    m.register("model_select", enum)
    sb = SignalBundle(task={"risk_level": "low"})
    with pytest.raises(RuntimeError, match="empty"):
        await m.decide("model_select", sb)


@pytest.mark.asyncio
async def test_decide_unregistered_kind_raises() -> None:
    m = StrategyMatcher()
    sb = SignalBundle()
    with pytest.raises(ValueError, match="No enumerator"):
        await m.decide("model_select", sb)


@pytest.mark.asyncio
async def test_writeback_hook() -> None:
    m = StrategyMatcher()

    async def enum(_sb, _prev):
        return [
            StrategyCandidate(
                candidate_id="x",
                description="x",
                expected_outcome=0.5,
                expected_cost_usd=0.0,
                expected_latency_sec=1.0,
            )
        ]

    m.register("model_select", enum)

    captured = []

    async def hook(d, info):
        captured.append((d.decision_id, info))

    m.register_writeback(hook)
    sb = SignalBundle(task={"risk_level": "low"})
    decision = await m.decide("model_select", sb)
    await m.writeback(decision, actual_outcome=0.6, actual_cost_usd=0.0)
    assert len(captured) == 1
    assert captured[0][1]["actual_outcome"] == 0.6


def test_singleton_matcher() -> None:
    reset_matcher()
    m1 = get_matcher()
    m2 = get_matcher()
    assert m1 is m2
    reset_matcher()
    m3 = get_matcher()
    assert m3 is not m1
