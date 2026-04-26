"""Tests for strategy_router_bridge (V2.1 wire M3.3)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from kun.core.strategy_matcher import reset_matcher
from kun.interface.llm.router import RouteDecision
from kun.interface.llm.strategy_router_bridge import (
    TIER_COST_ESTIMATE,
    TIER_LATENCY_ESTIMATE,
    TIER_OUTCOME_ESTIMATE,
    build_signal_bundle,
    ensure_registered,
    is_enabled,
    maybe_override_with_strategy,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_matcher()
    yield
    reset_matcher()


def test_disabled_by_default() -> None:
    """V2.1 wire 默认 off, 不破坏现有行为."""
    with patch.dict(os.environ, {}, clear=True):
        assert is_enabled() is False


def test_enabled_via_env() -> None:
    with patch.dict(os.environ, {"KUN_STRATEGY_MATCHER_ENABLED": "1"}):
        assert is_enabled() is True


def test_disabled_returns_base_unchanged() -> None:
    """禁用时, maybe_override 直接返 base."""
    base = RouteDecision(
        purpose="execution",
        primary_tier="strong",
        fallback_tier="fallback",
        rationale="test",
    )
    import asyncio

    with patch.dict(os.environ, {}, clear=True):
        result = asyncio.run(maybe_override_with_strategy(base, "execution", None))
    assert result.primary_tier == "strong"
    assert result.rationale == "test"


@pytest.mark.asyncio
async def test_enabled_strategy_matcher_low_risk_picks_cheap() -> None:
    """启用 + low risk + 短 prompt → strategy_score 倾向 cheap (γ=0.40)."""
    base = RouteDecision(
        purpose="execution",
        primary_tier="top",
        fallback_tier="fallback",
        rationale="default",
    )

    from kun.interface.llm.base import LLMMessage, LLMRequest

    req = LLMRequest(messages=[LLMMessage(role="user", content="x")])

    with patch.dict(os.environ, {"KUN_STRATEGY_MATCHER_ENABLED": "1"}):
        result = await maybe_override_with_strategy(
            base,
            "execution",
            req,
            profile=type(
                "P",
                (),
                {
                    "risk_level": "low",
                    "needs_coding": False,
                    "prefer_speed": False,
                    "force_fallback": False,
                },
            )(),
        )
    # 不应该选 top (low risk α 低 + top latency 8s 太大)
    assert result.primary_tier != "top"
    assert "strategy_matcher" in result.rationale


@pytest.mark.asyncio
async def test_enabled_critical_keeps_top() -> None:
    """启用 + critical → α=0.7, top 应该胜出 (高 outcome)."""
    base = RouteDecision(
        purpose="execution",
        primary_tier="top",
        fallback_tier="fallback",
        rationale="default",
    )

    from kun.interface.llm.base import LLMMessage, LLMRequest

    req = LLMRequest(messages=[LLMMessage(role="user", content="x")])

    with patch.dict(os.environ, {"KUN_STRATEGY_MATCHER_ENABLED": "1"}):
        result = await maybe_override_with_strategy(
            base,
            "execution",
            req,
            profile=type(
                "P",
                (),
                {
                    "risk_level": "critical",
                    "needs_coding": False,
                    "prefer_speed": False,
                    "force_fallback": False,
                },
            )(),
        )
    # critical: α=0.7, top (outcome=0.92) 应胜
    assert result.primary_tier == "top"


def test_build_signal_bundle_extracts_complexity() -> None:
    from kun.interface.llm.base import LLMMessage, LLMRequest

    long_msg = LLMMessage(role="user", content="x" * 4000)
    req = LLMRequest(messages=[long_msg])
    sb = build_signal_bundle("execution", req, profile=None)
    assert sb.task["complexity_score"] == 0.7  # > 3000 chars


def test_build_signal_bundle_with_profile_overrides() -> None:
    profile = type(
        "P",
        (),
        {"risk_level": "high", "needs_coding": True, "prefer_speed": True, "force_fallback": False},
    )()
    sb = build_signal_bundle("execution", None, profile)
    assert sb.task["risk_level"] == "high"
    assert sb.task["task_type"] == "coding"
    assert sb.user["speed_sensitivity"] == "high"


def test_ensure_registered_idempotent() -> None:
    ensure_registered()
    ensure_registered()
    ensure_registered()
    from kun.core.strategy_matcher import get_matcher

    assert "model_select" in get_matcher()._enumerators


def test_tier_estimate_tables_complete() -> None:
    """5 个 tier 都有估算."""
    expected_tiers = {"top", "strong", "cheap", "coding", "fallback"}
    assert set(TIER_COST_ESTIMATE.keys()) == expected_tiers
    assert set(TIER_LATENCY_ESTIMATE.keys()) == expected_tiers
    assert set(TIER_OUTCOME_ESTIMATE.keys()) == expected_tiers


def test_tier_outcomes_top_highest() -> None:
    """top tier outcome 应最高."""
    assert TIER_OUTCOME_ESTIMATE["top"] == max(TIER_OUTCOME_ESTIMATE.values())


def test_tier_costs_coding_zero() -> None:
    """coding tier (codex MCP) 走订阅, $0."""
    assert TIER_COST_ESTIMATE["coding"] == 0.0


def test_tier_latencies_cheap_lowest() -> None:
    """cheap tier (Haiku) 应最快."""
    assert TIER_LATENCY_ESTIMATE["cheap"] == min(TIER_LATENCY_ESTIMATE.values())
