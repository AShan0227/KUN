"""LLMRouterEnsembleAdapter 单测 (Wire 20)."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from kun.interface.llm.base import LLMRequest, LLMResponse, ModelTier, UsageInfo
from kun.interface.llm.router import LLMRouter
from kun.interface.llm.stub_provider import StubProvider
from kun.lab import (
    EnsembleConfig,
    EnsembleExecutor,
    LLMRouterEnsembleAdapter,
)
from kun.lab.ensemble_executor import PathConfig


def _build_router(*, tiers: list[ModelTier] | None = None) -> LLMRouter:
    """5 tier StubProvider router (top/strong/cheap/coding/fallback)."""
    tier_set: list[ModelTier] = tiers or ["top", "strong", "cheap", "coding", "fallback"]
    providers: dict[ModelTier, Any] = {
        t: StubProvider(model_id=f"stub-{t}", tier=t, latency_ms=2.0) for t in tier_set
    }
    return LLMRouter(providers)


@pytest.mark.asyncio
async def test_adapter_basic_call_returns_text_cost_latency() -> None:
    """adapter(prompt, path) → (text, cost_usd, latency_sec) 三元组."""
    router = _build_router()
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1)
    text, cost, latency = await adapter("hello world", path)

    assert isinstance(text, str)
    assert "hello world" in text  # stub echoes
    assert cost >= 0.0
    assert latency >= 0.0


@pytest.mark.asyncio
async def test_adapter_uses_path_temperature_and_tier() -> None:
    """temperature + tier 都正确传给 LLMRequest / provider."""
    captured: list[LLMRequest] = []

    def capturing_builder(request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(
            content="ok",
            usage=UsageInfo(input_tokens=5, output_tokens=2),
            tier="cheap",
        )

    cheap = StubProvider(model_id="capt-cheap", tier="cheap", builder=capturing_builder)
    router = LLMRouter({"cheap": cheap})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_cheap_high_temp", tier="cheap", temperature=0.7)
    await adapter("question", path)

    assert len(captured) == 1
    assert captured[0].temperature == pytest.approx(0.7)
    # user message present
    user_msgs = [m for m in captured[0].messages if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "question"


@pytest.mark.asyncio
async def test_adapter_injects_system_prompt_override() -> None:
    """path.system_prompt_override → 第一条 system message."""
    captured: list[LLMRequest] = []

    def capturing_builder(request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(content="ok", usage=UsageInfo())

    top = StubProvider(model_id="capt-top", tier="top", builder=capturing_builder)
    router = LLMRouter({"top": top})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(
        strategy="diverse_perspective",
        tier="top",
        temperature=0.0,
        system_prompt_override="Take a contrarian view.",
    )
    await adapter("Q4 plan", path)

    msgs = captured[0].messages
    assert msgs[0].role == "system"
    assert msgs[0].content == "Take a contrarian view."
    assert msgs[1].role == "user"
    assert msgs[1].content == "Q4 plan"


@pytest.mark.asyncio
async def test_adapter_no_system_prompt_when_omitted() -> None:
    """system_prompt_override=None → messages 只有 user."""
    captured: list[LLMRequest] = []

    def capturing_builder(request: LLMRequest) -> LLMResponse:
        captured.append(request)
        return LLMResponse(content="ok", usage=UsageInfo())

    top = StubProvider(model_id="capt-top", tier="top", builder=capturing_builder)
    router = LLMRouter({"top": top})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1)
    await adapter("just user", path)

    msgs = captured[0].messages
    assert len(msgs) == 1
    assert msgs[0].role == "user"


@pytest.mark.asyncio
async def test_adapter_unknown_tier_raises() -> None:
    """tier 不在 router.providers → RuntimeError 含可用 tier 列表."""
    router = LLMRouter({"cheap": StubProvider(model_id="x", tier="cheap")})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1)
    with pytest.raises(RuntimeError, match="no provider for tier='top'"):
        await adapter("q", path)


@pytest.mark.asyncio
async def test_adapter_propagates_provider_exception() -> None:
    """provider.invoke 抛异常 → adapter 让它向上传 (EnsembleExecutor 收)."""

    def failing_builder(request: LLMRequest) -> LLMResponse:
        raise RuntimeError("simulated provider failure")

    top = StubProvider(model_id="fail", tier="top", builder=failing_builder)
    router = LLMRouter({"top": top})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1)
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await adapter("q", path)


@pytest.mark.asyncio
async def test_adapter_cost_falls_back_to_actual_when_no_equivalent() -> None:
    """cost_usd_equivalent=0 → fallback 到 cost_usd_actual."""

    def cost_builder(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="x",
            usage=UsageInfo(input_tokens=10, output_tokens=10),
            cost_usd_actual=0.05,
            cost_usd_equivalent=0.0,
        )

    top = StubProvider(model_id="cost", tier="top", builder=cost_builder)
    router = LLMRouter({"top": top})
    adapter = LLMRouterEnsembleAdapter(router)

    path = PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1)
    _, cost, _ = await adapter("q", path)
    assert cost == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_adapter_end_to_end_with_executor() -> None:
    """LLMRouterEnsembleAdapter 装进 EnsembleExecutor 真跑全 5 路径."""
    router = _build_router()
    adapter = LLMRouterEnsembleAdapter(router)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        executor = EnsembleExecutor(adapter)
        result = await executor.run(
            "Analyze quarterly revenue",
            config=EnsembleConfig(n_paths=5, selection_method="best_score"),
        )

    assert len(result.path_results) == 5
    # 每条都成功
    assert all(not pr.error for pr in result.path_results)
    # winner 选了一条
    assert 0 <= result.winning_path_idx < 5
    assert result.winning_output != ""
    # tier 都不一样 (5 strategies → 5 tier mix)
    tiers = {pr.config["tier"] for pr in result.path_results}
    assert tiers == {"top", "strong", "cheap"}  # DEFAULT_PATHS 是 top/strong/cheap/top/top


@pytest.mark.asyncio
async def test_adapter_lab_disabled_blocks_executor_run() -> None:
    """KUN_LAB_MODE=0 时 EnsembleExecutor.run() 仍 raise (adapter 本身不强校验)."""
    router = _build_router()
    adapter = LLMRouterEnsembleAdapter(router)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_LAB_MODE", None)
        executor = EnsembleExecutor(adapter)
        with pytest.raises(RuntimeError, match="KUN-Lab disabled"):
            await executor.run("q", config=EnsembleConfig(n_paths=2))


def test_make_default_adapter_uses_get_router() -> None:
    """make_default_adapter() 走 get_router() 单例 — 不真初始化 (mock)."""
    from kun.lab.llm_router_adapter import make_default_adapter

    fake_router = LLMRouter({"top": StubProvider(model_id="x", tier="top")})

    with patch("kun.interface.llm.router.get_router", return_value=fake_router):
        adapter = make_default_adapter(task_type="test_factory")
        assert adapter._router is fake_router
        assert adapter._task_type == "test_factory"
