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
from kun.core.quota_tracker import get_tracker
from kun.interface.llm.anthropic_provider import AnthropicProvider
from kun.interface.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelTier,
    TaskProfile,
)
from kun.interface.llm.claude_code_provider import ClaudeCodeProvider
from kun.interface.llm.codex_cli_provider import CodexCliProvider
from kun.interface.llm.codex_mcp_provider import CodexMcpProvider
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

# Character thresholds for complexity heuristic (ADR-002 amendment 2026-04-24).
# Goal: simple prompts → haiku (fast + low Pro-quota cost), long multi-turn →
# opus. Applies only to the top/strong/cheap tier family (Claude Code CLI).
_COMPLEXITY_SIMPLE_MAX = 400  # <400 chars ≈ <100 tokens → haiku
_COMPLEXITY_COMPLEX_MIN = 3000  # >3000 chars ≈ >750 tokens → opus-worthy


def _complexity_hint(request: LLMRequest) -> Literal["simple", "medium", "complex"]:
    """Estimate task complexity from total prompt length."""
    total = sum(len(m.content or "") for m in request.messages)
    if total < _COMPLEXITY_SIMPLE_MAX:
        return "simple"
    if total < _COMPLEXITY_COMPLEX_MIN:
        return "medium"
    return "complex"


def _apply_complexity(tier: ModelTier, hint: str) -> ModelTier:
    """Adjust tier inside the top/strong/cheap family based on complexity.

    Rules (only within {top, strong, cheap}, coding/fallback untouched):
      - simple  → cheap (downgrade from top/strong)
      - complex → at least strong (upgrade from cheap)
      - medium  → keep purpose-derived tier
    """
    if tier not in {"top", "strong", "cheap"}:
        return tier
    if hint == "simple":
        return "cheap"
    if hint == "complex" and tier == "cheap":
        return "strong"
    return tier


class LLMRouter:
    """Multi-provider router with tier fallback."""

    def __init__(self, providers: dict[ModelTier, LLMProvider]) -> None:
        self.providers = providers

    async def close(self) -> None:
        """Release provider resources (long-lived subprocesses, HTTP pools).

        Called from FastAPI lifespan shutdown. Safe to call multiple times —
        each provider's `close()` is best-effort.
        """
        seen: set[int] = set()
        for provider in self.providers.values():
            # Some providers are shared across tiers; close each instance once
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close_fn = getattr(provider, "close", None)
            if close_fn is None:
                continue
            try:
                await close_fn()
            except Exception as e:
                log.warning(
                    "router.provider_close_failed",
                    provider=provider.name,
                    error=str(e),
                )

    # ---------- Routing decision ----------

    def decide(
        self,
        purpose: TaskPurpose,
        profile: TaskProfile | None = None,
        request: LLMRequest | None = None,
    ) -> RouteDecision:
        """Pick a primary tier, applying four layers in order:

        1. purpose → tier (static table, `_PURPOSE_TO_TIER`)
        2. profile overrides (explicit `risk_level` / `prefer_speed` / `needs_coding`)
        3. complexity hint (prompt-length heuristic; only adjusts inside top/strong/cheap)
        4. quota tracker (5h rolling window; downgrades when saturated)

        Critical-risk profiles pin to `top` *before* quota resolution — if even
        top is saturated the tracker will still downgrade but the ask is recorded.
        """
        rationale_parts: list[str] = [f"purpose={purpose}"]
        primary: ModelTier = _PURPOSE_TO_TIER.get(purpose, "top")
        rationale_parts.append(f"→ {primary}")

        # --- Layer 2: profile overrides ---
        if profile:
            if profile.needs_coding and purpose == "execution":
                primary = "coding"
                rationale_parts.append("coding-profile")
            if profile.prefer_speed and primary == "top":
                primary = "strong"
                rationale_parts.append("prefer-speed→strong")

        # --- Layer 3: complexity hint (only top/strong/cheap family) ---
        if request is not None and primary in {"top", "strong", "cheap"}:
            hint = _complexity_hint(request)
            new_tier = _apply_complexity(primary, hint)
            if new_tier != primary:
                rationale_parts.append(f"complexity={hint}→{new_tier}")
                primary = new_tier

        # --- Critical pinning (after complexity, before quota) ---
        if profile and profile.risk_level == "critical":
            primary = "top"
            rationale_parts.append("critical→top")

        # --- Layer 4: quota-aware downgrade ---
        if primary in {"top", "strong", "cheap", "fallback"}:
            resolved = get_tracker().resolve(primary)  # type: ignore[arg-type]
            if resolved != primary:
                rationale_parts.append(f"quota:{primary}→{resolved}")
                log.info(
                    "router.quota_downgrade",
                    from_tier=primary,
                    to_tier=resolved,
                    purpose=purpose,
                )
                primary = resolved

        return RouteDecision(
            purpose=purpose,
            primary_tier=primary,
            fallback_tier="fallback",
            rationale=" | ".join(rationale_parts),
        )

    # ---------- Execution with fallback ----------

    async def invoke(
        self,
        request: LLMRequest,
        *,
        purpose: TaskPurpose = "execution",
    ) -> LLMResponse:
        """Execute with automatic fallback on failure.

        Records the tier against the quota tracker on success so the next
        `decide()` sees an updated rolling-window usage.
        """
        decision = self.decide(purpose, request.profile, request=request)
        log.debug(
            "router.invoke",
            purpose=purpose,
            primary_tier=decision.primary_tier,
            fallback_tier=decision.fallback_tier,
            rationale=decision.rationale,
        )

        # Try primary tier
        primary = self.providers.get(decision.primary_tier)
        if primary is not None:
            try:
                result = await _invoke_with_retry(primary, request)
                get_tracker().record(decision.primary_tier)  # type: ignore[arg-type]
                return result
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
        result = await _invoke_with_retry(fallback, request)
        get_tracker().record(decision.fallback_tier)  # type: ignore[arg-type]
        return result


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
async def _invoke_with_retry(provider: LLMProvider, request: LLMRequest) -> LLMResponse:
    return await provider.invoke(request)


# =============== Factory ===============

_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """Build (or return cached) router from environment.

    Resolution priority (ADR-002, updated 2026-04-24 — user decision:
    Claude/GPT via CLI OAuth, not API keys):

      top / strong / cheap:
        1. Claude Code CLI (OAuth subscription) ← PREFERRED
        2. Anthropic API (if KUN_OFOX_API_KEY or ANTHROPIC_API_KEY set)
        3. MiniMax substitute (if MINIMAX_API_KEY)
        4. Stub (tests)

      coding:
        1. Codex MCP-server (OAuth ChatGPT subscription, gpt-5.3-codex-spark) ← PREFERRED
        2. Codex exec CLI (OpenAI API accounts only — ChatGPT accounts must use MCP)
        3. OpenAI API (if key)
        4. Claude Code CLI (fallback within OAuth family)
        5. MiniMax substitute
        6. Stub

      fallback:
        1. MiniMax (direct API)
        2. Stub

    Disable CLI probing by setting KUN_DISABLE_CLI_OAUTH=1.
    Disable only codex (keep Claude CLI): KUN_DISABLE_CODEX_CLI=1.
    Override codex model id: KUN_CODEX_MCP_MODEL=gpt-5.3-codex-spark.
    """
    global _router
    if _router is not None:
        return _router

    providers: dict[ModelTier, LLMProvider] = {}

    cli_disabled = os.getenv("KUN_DISABLE_CLI_OAUTH") == "1"
    codex_disabled = os.getenv("KUN_DISABLE_CODEX_CLI") == "1"
    has_claude_cli = ClaudeCodeProvider.available() and not cli_disabled
    # Prefer MCP (works with ChatGPT accounts via gpt-5.3-codex-spark);
    # fall back to exec CLI only when the user has an OpenAI API key account.
    has_codex_mcp = CodexMcpProvider.available() and not cli_disabled and not codex_disabled
    has_codex_cli = CodexCliProvider.available() and not cli_disabled and not codex_disabled
    has_ofox = bool(os.getenv("KUN_OFOX_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_minimax = bool(os.getenv("MINIMAX_API_KEY"))

    # ---- top / strong / cheap ----
    if has_claude_cli:
        log.info("router.claude_code_cli", hint="using logged-in claude CLI OAuth")
        providers["top"] = ClaudeCodeProvider(tier="top")
        providers["strong"] = ClaudeCodeProvider(tier="strong")
        providers["cheap"] = ClaudeCodeProvider(tier="cheap")
    elif has_ofox or has_anthropic:
        providers["top"] = AnthropicProvider(model_id="claude-opus-4-7", tier="top")
        providers["strong"] = AnthropicProvider(model_id="claude-sonnet-4-6", tier="strong")
        providers["cheap"] = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")
    elif has_minimax:
        log.info(
            "router.minimax_substitute",
            hint="MiniMax used for top/strong/cheap (no Anthropic creds/CLI)",
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

    # ---- coding ----
    if has_codex_mcp:
        log.info(
            "router.codex_mcp",
            hint="using codex mcp-server (ChatGPT OAuth, gpt-5.3-codex-spark)",
        )
        providers["coding"] = CodexMcpProvider(tier="coding")
    elif has_codex_cli:
        # Legacy path — only works for OpenAI-API-key accounts, not ChatGPT OAuth
        log.info("router.codex_cli_legacy", hint="using codex exec (API-key path)")
        providers["coding"] = CodexCliProvider(tier="coding", model_id="gpt-5")
    elif has_openai or has_ofox:
        providers["coding"] = OpenAIProvider(model_id="gpt-5", tier="coding")
    elif has_claude_cli:
        # Fallback within the OAuth family — claude-code CLI for coding too
        providers["coding"] = ClaudeCodeProvider(tier="coding")
    elif has_minimax:
        providers["coding"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["coding"].tier = "coding"
    else:
        providers["coding"] = StubProvider(model_id="stub-codex-5.3", tier="coding")

    # ---- fallback ----
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
