"""LLM Router (§7.2 + ADR-002).

调用顺序 (硬规则):
  1. 默认走主力 Opus 4.7 (tier="top")
  2. 识别为"代码密集" → 走 Codex 5.3 (tier="coding")
  3. 识别为"轻量决策 / 分类 / 简单判官" → 走便宜档 (tier="cheap")
  4. 任一路径不可用 → 自动降级 MiniMax fallback + 推送通知
  5. MiniMax 不可用 → 熔断, 排队, 问用户

自我进化: 每次路由结果入 events (type=llm.call.completed),
idle-batch 按聚类 / 关联规则挖掘涌现新路由规则.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from tenacity import retry, stop_after_attempt, wait_exponential

from kun.core.logging import get_logger
from kun.core.metrics import llm_fallback_total
from kun.interface.llm.anthropic_provider import AnthropicProvider
from kun.interface.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    TaskProfile,
)
from kun.interface.llm.minimax_provider import MiniMaxProvider
from kun.interface.llm.openai_provider import OpenAIProvider
from kun.interface.llm.stub_provider import StubProvider

log = get_logger("kun.llm.router")

TaskPurpose = Literal[
    "intent",  # 意图理解 → top
    "planning",  # 任务拆解 → top / strong
    "routing_decision",  # 路由判断 → cheap
    "execution",  # 常规执行 → top (ADR-002 主力)
    "coding",  # 编程 → coding
    "judge",  # 判官 / 评估 → cheap / strong
    "classification",  # 分类 → cheap
    "compression",  # 压缩小模型 → cheap (local)
]


@dataclass(frozen=True)
class RouteDecision:
    purpose: TaskPurpose
    primary_tier: ModelTier
    fallback_tier: ModelTier = "fallback"
    rationale: str = ""


# 目的 → 主档位映射 (ADR-002)
_PURPOSE_TO_TIER: dict[TaskPurpose, ModelTier] = {
    "intent": "top",
    "planning": "top",
    "routing_decision": "cheap",
    "execution": "top",  # ADR-002: Opus 4.7 主力
    "coding": "coding",
    "judge": "cheap",
    "classification": "cheap",
    "compression": "cheap",
}


class LLMRouter:
    """Multi-provider router with tier fallback."""

    def __init__(self, providers: dict[ModelTier, LLMProvider]) -> None:
        self.providers = providers

    # ---------- Routing decision ----------

    def decide(
        self,
        purpose: TaskPurpose,
        profile: TaskProfile | None = None,
    ) -> RouteDecision:
        """Pick a primary tier. Can be overridden by profile hints."""
        primary = _PURPOSE_TO_TIER.get(purpose, "top")

        # Profile-driven overrides
        if profile:
            if profile.needs_coding and purpose == "execution":
                primary = "coding"
            if profile.prefer_speed and primary == "top":
                primary = "strong"
            if profile.risk_level == "critical":
                # Critical → always top regardless
                primary = "top"

        return RouteDecision(
            purpose=purpose,
            primary_tier=primary,
            fallback_tier="fallback",
            rationale=f"purpose={purpose} → {primary}",
        )

    # ---------- Execution with fallback ----------

    async def invoke(
        self,
        request: LLMRequest,
        *,
        purpose: TaskPurpose = "execution",
    ) -> LLMResponse:
        """Execute with automatic fallback on failure."""
        decision = self.decide(purpose, request.profile)
        log.debug(
            "router.invoke",
            purpose=purpose,
            primary_tier=decision.primary_tier,
            fallback_tier=decision.fallback_tier,
        )

        # Try primary tier
        primary = self.providers.get(decision.primary_tier)
        if primary is not None:
            try:
                return await _invoke_with_retry(primary, request)
            except Exception as e:
                log.warning(
                    "router.primary_failed",
                    provider=primary.name,
                    model=primary.model_id,
                    error=str(e),
                )
                llm_fallback_total.labels(
                    from_provider=primary.name,
                    to_provider=self.providers.get(decision.fallback_tier, primary).name,
                    reason=type(e).__name__,
                ).inc()

        # Fallback tier
        fallback = self.providers.get(decision.fallback_tier)
        if fallback is None:
            raise RuntimeError(
                f"No provider for primary={decision.primary_tier} or fallback={decision.fallback_tier}"
            )
        log.info(
            "router.fallback_engaged",
            purpose=purpose,
            fallback=fallback.name,
        )
        return await _invoke_with_retry(fallback, request)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
async def _invoke_with_retry(provider: LLMProvider, request: LLMRequest) -> LLMResponse:
    return await provider.invoke(request)


# =============== Factory ===============

_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """Build (or return cached) router from environment.

    Resolution priority (per ADR-002 + dev-time reality):

      top / strong / cheap:
        1. Anthropic via ofox proxy (KUN_OFOX_API_KEY) or direct (ANTHROPIC_API_KEY)
        2. MiniMax as substitute (if MINIMAX_API_KEY set and no Anthropic)
        3. Stub (deterministic, for tests)

      coding:
        1. OpenAI via ofox or direct
        2. MiniMax substitute
        3. Stub

      fallback: MiniMax if creds, else stub.
    """
    global _router
    if _router is not None:
        return _router

    providers: dict[ModelTier, LLMProvider] = {}

    has_ofox = bool(os.getenv("KUN_OFOX_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_minimax = bool(os.getenv("MINIMAX_API_KEY"))

    if has_ofox or has_anthropic:
        providers["top"] = AnthropicProvider(model_id="claude-opus-4-7", tier="top")
        providers["strong"] = AnthropicProvider(model_id="claude-sonnet-4-6", tier="strong")
        providers["cheap"] = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")
    elif has_minimax:
        log.info(
            "router.minimax_substitute",
            hint="MiniMax M2.7 used for top/strong/cheap (Anthropic creds missing)",
        )
        providers["top"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["top"].tier = "top"
        providers["strong"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["strong"].tier = "strong"
        providers["cheap"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["cheap"].tier = "cheap"
    else:
        log.warning("router.no_creds", hint="falling back to stub for top/strong/cheap")
        providers["top"] = StubProvider(model_id="stub-opus-4.7", tier="top")
        providers["strong"] = StubProvider(model_id="stub-sonnet-4.6", tier="strong")
        providers["cheap"] = StubProvider(model_id="stub-haiku-4.5", tier="cheap")

    if has_openai or has_ofox:
        providers["coding"] = OpenAIProvider(model_id="codex-5.3", tier="coding")
    elif has_minimax:
        providers["coding"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["coding"].tier = "coding"
    else:
        providers["coding"] = StubProvider(model_id="stub-codex-5.3", tier="coding")

    if has_minimax:
        providers["fallback"] = MiniMaxProvider(model_id="MiniMax-M2.7")
    else:
        log.warning("router.no_minimax_creds", hint="falling back to stub for fallback")
        providers["fallback"] = StubProvider(model_id="stub-minimax-m2.7", tier="fallback")

    _router = LLMRouter(providers)
    return _router


def set_router(router: LLMRouter) -> None:
    """Override the cached router (for tests)."""
    global _router
    _router = router


def reset_router() -> None:
    """Clear cached router."""
    global _router
    _router = None
