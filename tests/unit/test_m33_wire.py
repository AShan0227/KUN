"""Tests for M3.3 wire: PanoramaBuilder + SoulFile orchestrator/router 接入."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from kun.core.strategy_matcher import reset_matcher
from kun.datamodel.soul_file import SoulFile
from kun.datamodel.soul_file_provider import (
    get_soul_file,
    reset_store,
    soul_file_to_router_overrides,
    soul_file_to_signal_user_dict,
)
from kun.datamodel.soul_file_provider import (
    is_enabled as soul_is_enabled,
)
from kun.engineering.panorama_orchestrator_bridge import (
    build_panorama_for_task,
    panorama_to_event_data,
    reset_builder,
)
from kun.engineering.panorama_orchestrator_bridge import (
    is_enabled as panorama_is_enabled,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    reset_matcher()
    reset_builder()
    yield
    reset_store()
    reset_matcher()
    reset_builder()


# ---- PanoramaBuilder bridge ----


def test_panorama_disabled_default() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert panorama_is_enabled() is False


def test_panorama_enabled_via_env() -> None:
    with patch.dict(os.environ, {"KUN_PANORAMA_BUILDER_ENABLED": "1"}):
        assert panorama_is_enabled() is True


@pytest.mark.asyncio
async def test_build_panorama_disabled_returns_none() -> None:
    """禁用时直接返 None."""
    fake_task_ref = type(
        "TR",
        (),
        {
            "meta": type(
                "M",
                (),
                {
                    "task_id": "tk-1",
                    "task_type": "x",
                    "risk_level": "low",
                    "complexity_score": 0.2,
                    "estimated_cost_usd": 0.01,
                    "success_criteria_short": "x",
                },
            )()
        },
    )()
    with patch.dict(os.environ, {}, clear=True):
        result = await build_panorama_for_task(fake_task_ref, "test")
    assert result is None


@pytest.mark.asyncio
async def test_build_panorama_enabled_returns_panorama() -> None:
    fake_task_ref = type(
        "TR",
        (),
        {
            "meta": type(
                "M",
                (),
                {
                    "task_id": "tk-1",
                    "task_type": "x",
                    "risk_level": "low",
                    "complexity_score": 0.2,
                    "estimated_cost_usd": 0.01,
                    "success_criteria_short": "测试任务",
                },
            )()
        },
    )()
    with patch.dict(os.environ, {"KUN_PANORAMA_BUILDER_ENABLED": "1"}):
        result = await build_panorama_for_task(fake_task_ref, "test message")
    assert result is not None
    assert result.task_ref == "tk-1"
    assert result.intent_one_sentence == "测试任务"
    assert result.tier in ("minimal", "light", "medium", "heavy", "full")


# V2.2 §19.3 + C25 wire: build_anchored


@pytest.mark.asyncio
async def test_build_panorama_anchored_disabled_returns_empty() -> None:
    fake_task_ref = type(
        "TR",
        (),
        {
            "meta": type(
                "M",
                (),
                {
                    "task_id": "tk-1",
                    "task_type": "x",
                    "risk_level": "low",
                    "complexity_score": 0.2,
                    "estimated_cost_usd": 0.01,
                    "success_criteria_short": "测试",
                    "execution_mode": "FAST",
                },
            )()
        },
    )()
    from kun.engineering.panorama_orchestrator_bridge import (
        build_panorama_anchored_for_task,
    )

    with patch.dict(os.environ, {}, clear=True):
        result = await build_panorama_anchored_for_task(fake_task_ref, "test")
    assert result == []


@pytest.mark.asyncio
async def test_build_panorama_anchored_enabled_yields_modules() -> None:
    """V2.2 wire: enabled + FAST mode → 1 round (1-2 module)."""
    fake_task_ref = type(
        "TR",
        (),
        {
            "meta": type(
                "M",
                (),
                {
                    "task_id": "tk-fast",
                    "task_type": "x",
                    "risk_level": "low",
                    "complexity_score": 0.2,
                    "estimated_cost_usd": 0.01,
                    "success_criteria_short": "fast 任务",
                    "execution_mode": "FAST",
                },
            )()
        },
    )()
    from kun.engineering.panorama_orchestrator_bridge import (
        build_panorama_anchored_for_task,
    )

    with patch.dict(os.environ, {"KUN_PANORAMA_BUILDER_ENABLED": "1"}):
        modules = await build_panorama_anchored_for_task(fake_task_ref, "test")
    # FAST → 1 round = at least intent_one_sentence + risk_summary 2 个
    assert len(modules) >= 1


@pytest.mark.asyncio
async def test_build_panorama_anchored_max_yields_more_modules_than_fast() -> None:
    """V2.2 wire: MAX 模式应跑更多 round → 更多 module."""
    from kun.engineering.panorama_orchestrator_bridge import (
        build_panorama_anchored_for_task,
    )

    def _make_ref(mode: str):
        return type(
            "TR",
            (),
            {
                "meta": type(
                    "M",
                    (),
                    {
                        "task_id": f"tk-{mode}",
                        "task_type": "x",
                        "risk_level": "high",
                        "complexity_score": 0.8,
                        "estimated_cost_usd": 0.5,
                        "success_criteria_short": f"{mode} 任务",
                        "execution_mode": mode,
                    },
                )()
            },
        )()

    with patch.dict(os.environ, {"KUN_PANORAMA_BUILDER_ENABLED": "1"}):
        fast_modules = await build_panorama_anchored_for_task(_make_ref("FAST"), "x")
        max_modules = await build_panorama_anchored_for_task(_make_ref("MAX"), "x")

    # MAX 跑 3 round, FAST 跑 1 round → MAX 模块数 ≥ FAST
    assert len(max_modules) >= len(fast_modules)
    assert len(max_modules) > 1


def test_panorama_to_event_data() -> None:
    from kun.core.task_panorama import TaskPanorama

    p = TaskPanorama(
        task_ref="tk-1",
        tier="medium",
        intent_one_sentence="x",
        modules_run=["risk_assessment"],
        modules_skipped=["multi_judge_review"],
    )
    data = panorama_to_event_data(p)
    assert data["stage"] == "panorama_built"
    assert data["tier"] == "medium"
    assert data["modules_run"] == ["risk_assessment"]


# ---- SoulFile provider ----


def test_soul_disabled_default() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert soul_is_enabled() is False


def test_soul_enabled_via_env() -> None:
    with patch.dict(os.environ, {"KUN_SOUL_FILE_ENABLED": "1"}):
        assert soul_is_enabled() is True


def test_get_soul_file_creates_default() -> None:
    soul = get_soul_file("u-1")
    assert soul.user_id == "u-1"
    assert soul.audience == "developer"  # 默认
    assert soul.approval_threshold_money == 10.0


def test_get_soul_file_returns_existing() -> None:
    soul1 = get_soul_file("u-1")
    soul1.audience = "expert"
    soul2 = get_soul_file("u-1")
    assert soul2.audience == "expert"  # 同一实例


def test_soul_to_router_overrides() -> None:
    soul = SoulFile(
        user_id="u-1",
        audience="expert",
        approval_threshold_money=50.0,
        risk_tolerance="low",
    )
    overrides = soul_file_to_router_overrides(soul)
    assert overrides["audience"] == "expert"
    assert overrides["approval_threshold_money"] == 50.0
    assert overrides["risk_tolerance"] == "low"


def test_soul_to_signal_user_dict() -> None:
    soul = SoulFile(
        user_id="u-007",
        speed_sensitivity="high",
        cost_sensitivity="low",
        professional_role="后端工程师",
    )
    user_dict = soul_file_to_signal_user_dict(soul)
    assert user_dict["user_id"] == "u-007"
    assert user_dict["speed_sensitivity"] == "high"
    assert user_dict["cost_sensitivity"] == "low"
    assert user_dict["user_role"] == "后端工程师"


# ---- 完整链路: SoulFile → SignalBundle → StrategyMatcher → router ----


@pytest.mark.asyncio
async def test_soul_file_affects_router_decision() -> None:
    """启用 SoulFile + StrategyMatcher → user.cost_sensitivity=high 让 β 升 → 倾向便宜."""
    from kun.interface.llm.base import LLMMessage, LLMRequest
    from kun.interface.llm.router import RouteDecision
    from kun.interface.llm.strategy_router_bridge import (
        maybe_override_with_strategy,
    )

    # 设置该 user 极端成本敏感
    soul = get_soul_file("u-cost-sensitive")
    soul.cost_sensitivity = "high"
    soul.speed_sensitivity = "low"

    base = RouteDecision(
        purpose="execution",
        primary_tier="top",
        fallback_tier="fallback",
        rationale="default",
    )
    req = LLMRequest(messages=[LLMMessage(role="user", content="x")])

    with patch.dict(
        os.environ,
        {
            "KUN_STRATEGY_MATCHER_ENABLED": "1",
            "KUN_SOUL_FILE_ENABLED": "1",
        },
    ):
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
            user_id="u-cost-sensitive",
        )
    # cost_sensitivity=high → β 升 → 不应选 top (cost 高)
    assert result.primary_tier != "top"


@pytest.mark.asyncio
async def test_soul_disabled_does_not_affect() -> None:
    """SoulFile 禁用时, user_id 传了也不影响."""
    from kun.interface.llm.base import LLMMessage, LLMRequest
    from kun.interface.llm.strategy_router_bridge import build_signal_bundle

    soul = get_soul_file("u-1")
    soul.cost_sensitivity = "high"

    req = LLMRequest(messages=[LLMMessage(role="user", content="x")])

    with patch.dict(os.environ, {}, clear=True):
        sb = build_signal_bundle("execution", req, profile=None, user_id="u-1")
    # SoulFile 禁用 → user 字段不该有 cost_sensitivity
    assert "cost_sensitivity" not in sb.user
