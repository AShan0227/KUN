"""LLM Router (§7.2 + ADR-002).

调用顺序 (硬规则):
  1. 默认走 Codex MCP / GPT-5.5 (ChatGPT 订阅链路)
  2. 显式指定 Claude / Anthropic 时才走 Claude 家族
  3. 识别为"轻量决策 / 分类 / 简单判官" → 走便宜档 (tier="cheap")
  4. 任一路径不可用 → 自动降级 MiniMax fallback + 推送通知
  5. MiniMax 不可用 → 熔断, 排队, 问用户

自我进化: 每次路由结果入 events (type=llm.call.completed),
idle-batch 按聚类 / 关联规则挖掘涌现新路由规则.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

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
    UsageInfo,
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

    def __init__(
        self,
        providers: dict[ModelTier, LLMProvider],
        *,
        ab_alternates: dict[ModelTier, LLMProvider] | None = None,
        ab_ratio: float = 0.0,
    ) -> None:
        """providers: 每 tier 1 个 primary 模型 (现在的默认行为).

        ab_alternates: 每 tier 可选的"挑战者"模型. invoke 时按 ab_ratio 概率切流;
            走挑战者 → OTel span 标 kun.ab_branch="challenger" + 记 cost. 这样
            Grafana 能直接对比同 tier 两模型的 success / latency / cost. 不并行
            调用 (避免成本翻倍), 是真 A/B 不是 shadow.
        ab_ratio: 0.0-1.0. 默认 0 关闭 A/B; 0.1 = 10% 流量进 challenger.
        """
        self.providers = providers
        self.ab_alternates: dict[ModelTier, LLMProvider] = ab_alternates or {}
        self.ab_ratio = max(0.0, min(1.0, ab_ratio))

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

        # --- Budget kill switch ---
        # Orchestrator sets profile.force_fallback when daily budget is over
        # the hard cap. Skip every other layer and pin to fallback so we stop
        # burning subscription quota for the rest of the day.
        if profile and profile.force_fallback:
            rationale_parts.append("budget→fallback")
            return RouteDecision(
                purpose=purpose,
                primary_tier="fallback",
                fallback_tier="fallback",
                rationale=" | ".join(rationale_parts),
            )

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
        # OTel: span around router so Grafana can show
        # purpose × primary_tier 路由分布 + fallback 触发率.
        from opentelemetry import trace

        tracer = trace.get_tracer("kun.interface.llm.router")
        with tracer.start_as_current_span("kun.router.invoke") as span:
            decision = self.decide(purpose, request.profile, request=request)
            route_debug: dict[str, object] = {
                "purpose": purpose,
                "initial_tier": decision.primary_tier,
                "initial_rationale": decision.rationale,
                "fallback_tier": decision.fallback_tier,
                "complexity_hint": _complexity_hint(request),
                "strategy_override": False,
                "credit_override": False,
                "ab_branch": "primary",
                "fallback_engaged": False,
            }

            # V2.1 §17 wire (M3.3): opt-in StrategyMatcher 第 5 层覆盖
            # KUN_STRATEGY_MATCHER_ENABLED=1 启用; 默认 off, 不破坏现有行为.
            from kun.interface.llm.strategy_router_bridge import (
                is_enabled as _sm_enabled,
            )
            from kun.interface.llm.strategy_router_bridge import (
                maybe_override_with_strategy,
            )

            if _sm_enabled():
                before_strategy = decision
                decision = await maybe_override_with_strategy(
                    decision,
                    purpose,
                    request,
                    request.profile,
                )
                span.set_attribute("kun.strategy_matcher_engaged", True)
                if decision.primary_tier != before_strategy.primary_tier:
                    route_debug["strategy_override"] = True
                    route_debug["strategy_from_tier"] = before_strategy.primary_tier
                    route_debug["strategy_to_tier"] = decision.primary_tier

            before_credit = decision
            decision = await self._maybe_override_with_credit(decision, request)
            if decision.primary_tier != before_credit.primary_tier:
                route_debug["credit_override"] = True
                route_debug["credit_from_tier"] = before_credit.primary_tier
                route_debug["credit_to_tier"] = decision.primary_tier

            before_governance = decision
            decision = await self._maybe_govern_route(decision, request, purpose)
            if decision.primary_tier != before_governance.primary_tier:
                route_debug["governance_override"] = True
                route_debug["governance_from_tier"] = before_governance.primary_tier
                route_debug["governance_to_tier"] = decision.primary_tier
            route_debug["final_planned_tier"] = decision.primary_tier
            route_debug["final_rationale"] = decision.rationale

            span.set_attribute("kun.purpose", str(purpose))
            span.set_attribute("kun.primary_tier", str(decision.primary_tier))
            span.set_attribute("kun.fallback_tier", str(decision.fallback_tier))
            log.debug(
                "router.invoke",
                purpose=purpose,
                primary_tier=decision.primary_tier,
                fallback_tier=decision.fallback_tier,
                rationale=decision.rationale,
            )

            # A/B 切流: 同 tier 配了挑战者 + 命中 ab_ratio → 用挑战者代替 primary.
            # 失败时挑战者也走同样的 fallback 路径. 不并行调用 (不是 shadow).
            primary = self.providers.get(decision.primary_tier)
            challenger = self.ab_alternates.get(decision.primary_tier)
            ab_branch = "primary"
            if challenger is not None and self.ab_ratio > 0.0 and _ab_roll() < self.ab_ratio:
                primary = challenger
                ab_branch = "challenger"
            span.set_attribute("kun.ab_branch", ab_branch)
            route_debug["ab_branch"] = ab_branch
            route_debug["primary_provider"] = _provider_snapshot(primary)
            route_debug["challenger_provider"] = _provider_snapshot(challenger)

            # Try primary tier
            if primary is not None:
                try:
                    result = await _invoke_with_retry(primary, request)
                    get_tracker().record(decision.primary_tier)  # type: ignore[arg-type]
                    span.set_attribute("kun.fallback_engaged", False)
                    span.set_attribute("kun.final_provider", primary.name)
                    span.set_attribute("kun.cost_usd_equivalent", result.cost_usd_equivalent)
                    result.route_debug = {
                        **route_debug,
                        "fallback_engaged": False,
                        "final_provider": primary.name,
                        "final_model": primary.model_id,
                        "final_tier": decision.primary_tier,
                    }
                    return result
                except Exception as e:
                    route_debug["primary_error"] = type(e).__name__
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
                    # Emit a watchtower-observable event so the
                    # llm_fallback_spike rule can fire (R-A1).
                    await _emit_fallback_event(
                        primary_provider=primary.name,
                        primary_model=primary.model_id,
                        primary_tier=decision.primary_tier,
                        fallback_tier=decision.fallback_tier,
                        error=e,
                    )

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
            span.set_attribute("kun.fallback_engaged", True)
            span.set_attribute("kun.final_provider", fallback.name)
            span.set_attribute("kun.cost_usd_equivalent", result.cost_usd_equivalent)
            result.route_debug = {
                **route_debug,
                "fallback_engaged": True,
                "final_provider": fallback.name,
                "final_model": fallback.model_id,
                "final_tier": decision.fallback_tier,
            }
            return result

    async def _maybe_override_with_credit(
        self,
        decision: RouteDecision,
        request: LLMRequest,
    ) -> RouteDecision:
        """Use proven historical credits to adjust the chosen tier.

        这层是 MoE / 最佳路径学习的热路径接入点: Orchestrator 已经把
        ``model`` / ``model_tier`` / ``llm_route`` 写进 resource_credit_stats,
        router 这里把这些经验读回来。为了避免过度工程化拖慢简单任务, 只有
        历史差距足够明显时才覆盖原 4 层路由结论。
        """

        if os.getenv("KUN_LLM_CREDIT_ROUTING_ENABLED", "1") != "1":
            return decision
        if decision.primary_tier in {"fallback", "coding"}:
            return decision

        profile = request.profile
        if profile and (profile.force_fallback or profile.needs_coding):
            return decision
        if profile and profile.risk_level == "critical":
            return decision

        candidate_tiers = [
            cast_tier(tier)
            for tier in ("top", "strong", "cheap")
            if cast_tier(tier) in self.providers
        ]
        if decision.primary_tier not in candidate_tiers or len(candidate_tiers) < 2:
            return decision

        scored = await _score_tier_credit_candidates(self.providers, candidate_tiers)
        if not scored:
            return decision

        baseline = scored.get(decision.primary_tier, 0.0)
        best_tier, best_score = max(
            scored.items(),
            key=lambda item: (item[1], -_tier_strength_rank(item[0])),
        )
        if best_tier == decision.primary_tier:
            return decision

        min_score = _credit_routing_min_score()
        min_delta = _credit_routing_min_delta()
        if best_score < min_score or (best_score - baseline) < min_delta:
            return decision

        # 高风险任务允许经验把模型升档, 不允许因为历史便宜路线不错就降档。
        if (
            profile
            and profile.risk_level == "high"
            and _tier_strength_rank(best_tier) < _tier_strength_rank(decision.primary_tier)
        ):
            return decision

        return RouteDecision(
            purpose=decision.purpose,
            primary_tier=best_tier,
            fallback_tier=decision.fallback_tier,
            rationale=(
                decision.rationale
                + " | credit-routing:"
                + f" {decision.primary_tier}→{best_tier}"
                + f" score={best_score:.2f} baseline={baseline:.2f}"
            ),
        )

    async def _maybe_govern_route(
        self,
        decision: RouteDecision,
        request: LLMRequest,
        purpose: TaskPurpose,
    ) -> RouteDecision:
        """Run the selected tier through Watchtower route governance.

        This is the missing hot-path wire for ``LLMRouteGovernor``.  It keeps
        the router's normal tier heuristics and credit override, then asks the
        governance layer to enforce cost/trust/privacy rules before a provider
        is invoked.  If the governor has no historical scores it simply keeps
        the current primary tier; if trust policy blocks that tier it can choose
        the next allowed one.
        """

        if os.getenv("KUN_LLM_ROUTE_GOVERNANCE_ENABLED", "1") != "1":
            return decision
        governor = get_route_governor()
        if governor is None:
            return decision
        candidates = _governance_candidates(decision.primary_tier, self.providers)
        if not candidates:
            return decision
        provider = self.providers.get(decision.primary_tier)
        task_meta = _governance_task_meta(
            request=request,
            purpose=purpose,
            planned_decision=decision,
            planned_provider=provider,
        )
        selected = await governor.consult_for_model_select(task_meta, candidates)
        if selected == decision.primary_tier or selected not in self.providers:
            return decision
        selected_tier = cast_tier(selected)
        return RouteDecision(
            purpose=decision.purpose,
            primary_tier=selected_tier,
            fallback_tier=decision.fallback_tier,
            rationale=(
                decision.rationale + " | governance:" + f" {decision.primary_tier}→{selected_tier}"
            ),
        )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=8))
async def _invoke_with_retry(provider: LLMProvider, request: LLMRequest) -> LLMResponse:
    return await provider.invoke(request)


def _ab_roll() -> float:
    """A/B 抛骰. 单独抽一个函数, 让测试可以 monkeypatch 决定走哪条路."""
    import random

    return random.random()


def _provider_snapshot(provider: LLMProvider | None) -> dict[str, str] | None:
    if provider is None:
        return None
    return {
        "name": provider.name,
        "model_id": provider.model_id,
        "tier": provider.tier,
    }


async def _score_tier_credit_candidates(
    providers: dict[ModelTier, LLMProvider],
    candidate_tiers: list[ModelTier],
) -> dict[ModelTier, float]:
    """Score model tiers from hot + durable resource credits."""

    from kun.engineering.credit_assignment import get_contribution_tracker

    tier_keys: dict[ModelTier, list[str]] = {}
    for tier in candidate_tiers:
        provider = providers.get(tier)
        if provider is None:
            continue
        keys = [
            f"model:{provider.model_id}",
            f"model_tier:{tier}",
            f"llm_route:{provider.name}:{provider.model_id}:{tier}",
        ]
        tier_keys[tier] = keys

    all_keys = [key for keys in tier_keys.values() for key in keys]
    durable_scores = await _load_route_credit_scores(all_keys)
    tracker = get_contribution_tracker()
    tenant_id = _current_tenant_id_for_credit()

    scored: dict[ModelTier, float] = {}
    for tier, keys in tier_keys.items():
        hot = max(
            (tracker.contribution_score(key, tenant_id=tenant_id) for key in keys),
            default=0.0,
        )
        durable = max((durable_scores.get(key, 0.0) for key in keys), default=0.0)
        scored[tier] = max(hot, durable)
    return scored


async def _load_route_credit_scores(resource_keys: list[str]) -> dict[str, float]:
    """Load durable route credit scores without making router startup DB-bound."""

    if not resource_keys:
        return {}
    try:
        from kun.core.db import session_scope
        from kun.core.tenancy import current_tenant
        from kun.engineering.credit_assignment import load_resource_credit_scores

        tenant = current_tenant()
        async with session_scope(tenant_id=tenant.tenant_id) as session:
            return await load_resource_credit_scores(
                session,
                tenant_id=tenant.tenant_id,
                resource_keys=resource_keys,
            )
    except Exception as exc:
        log.debug("router.credit_scores_skipped", error=str(exc))
        return {}


def _current_tenant_id_for_credit() -> str | None:
    try:
        from kun.core.tenancy import current_tenant

        return current_tenant().tenant_id
    except Exception:
        return None


def _tier_strength_rank(tier: ModelTier) -> int:
    """Higher means stronger/more expensive reasoning tier."""

    return {"cheap": 1, "strong": 2, "top": 3, "coding": 3, "fallback": 0}.get(tier, 0)


def _credit_routing_min_score() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("KUN_LLM_CREDIT_ROUTING_MIN_SCORE", "0.75"))))
    except ValueError:
        return 0.75


def _credit_routing_min_delta() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("KUN_LLM_CREDIT_ROUTING_MIN_DELTA", "0.25"))))
    except ValueError:
        return 0.25


async def _emit_fallback_event(
    *,
    primary_provider: str,
    primary_model: str,
    primary_tier: ModelTier,
    fallback_tier: ModelTier,
    error: BaseException,
) -> None:
    """Emit ``llm.fallback.triggered`` so watchtower rules can react.

    Best-effort: we never let observability raise into the LLM hot path.
    Lazy-imports core/db to avoid a circular import (router is loaded by
    config/orchestrator before db engine is set up in some test paths).
    """
    try:
        from kun.core.db import session_scope
        from kun.core.events import emit
        from kun.core.tenancy import current_tenant
        from kun.datamodel.events import Event

        tenant = current_tenant()
    except Exception as e:
        log.debug("router.fallback_event_skipped_no_tenant", error=str(e))
        return

    try:
        async with session_scope() as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="llm.fallback.triggered",
                    payload={
                        "primary_provider": primary_provider,
                        "primary_model": primary_model,
                        "primary_tier": primary_tier,
                        "fallback_tier": fallback_tier,
                        "reason": type(error).__name__,
                    },
                ),
            )
    except Exception as e:
        log.debug("router.fallback_event_emit_failed", error=str(e))


# =============== Factory ===============

_router: LLMRouter | None = None
_route_governor: Any | None = None


def get_route_governor() -> Any | None:
    return _route_governor


def set_route_governor(governor: Any | None) -> None:
    """Install the process-local LLM route governor.

    Runtime startup wires this to Watchtower's loaded ``RuleEngine``.  Tests can
    set a fake governor without rebuilding the router.
    """

    global _route_governor
    _route_governor = governor


def get_router() -> LLMRouter:
    """Build (or return cached) router from environment.

    Resolution priority (ADR-002, updated 2026-04-29 — user decision:
    Claude account is unavailable; default main chain is Codex MCP / GPT-5.5):

      top / strong / cheap:
        1. Codex MCP / Codex CLI (default; or KUN_LLM_PRIMARY=codex)
        2. Claude Code CLI (only if KUN_LLM_PRIMARY=claude/anthropic or Codex unavailable)
        3. Anthropic API (if KUN_OFOX_API_KEY or ANTHROPIC_API_KEY set)
        4. MiniMax substitute (if MINIMAX_API_KEY)
        5. Stub (tests)

      coding:
        1. Codex MCP-server (OAuth ChatGPT subscription, GPT-5.5) ← PREFERRED
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
    Disable only Claude CLI (keep Codex): KUN_DISABLE_CLAUDE_CLI=1.
    Force one primary family with KUN_LLM_PRIMARY=auto|codex|claude|anthropic|minimax|stub.
    When KUN_LLM_PRIMARY=codex, Claude fallback is disabled by default; set
    KUN_ALLOW_CLAUDE_FALLBACK=1 if you intentionally want Claude as a backup.
    Override codex model id: KUN_CODEX_MCP_MODEL=gpt-5.5.
    """
    global _router
    if _router is not None:
        return _router

    providers: dict[ModelTier, LLMProvider] = {}

    primary_family = (os.getenv("KUN_LLM_PRIMARY") or "codex").strip().lower()
    valid_primary_families = {"auto", "codex", "claude", "anthropic", "minimax", "stub"}
    if primary_family not in valid_primary_families:
        log.warning("router.invalid_primary_family", value=primary_family, fallback="codex")
        primary_family = "codex"

    cli_disabled = os.getenv("KUN_DISABLE_CLI_OAUTH") == "1"
    claude_disabled = os.getenv("KUN_DISABLE_CLAUDE_CLI") == "1"
    codex_disabled = os.getenv("KUN_DISABLE_CODEX_CLI") == "1"
    allow_claude_fallback = os.getenv("KUN_ALLOW_CLAUDE_FALLBACK") == "1"
    has_claude_cli = ClaudeCodeProvider.available() and not cli_disabled and not claude_disabled
    # Prefer MCP (works with ChatGPT accounts; default model is GPT-5.5);
    # fall back to exec CLI only when the user has an OpenAI API key account.
    has_codex_mcp = CodexMcpProvider.available() and not cli_disabled and not codex_disabled
    has_codex_cli = CodexCliProvider.available() and not cli_disabled and not codex_disabled
    has_ofox = bool(os.getenv("KUN_OFOX_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_minimax = bool(os.getenv("MINIMAX_API_KEY"))

    def install_codex_family_for_main_tiers() -> bool:
        if has_codex_mcp:
            log.info(
                "router.codex_mcp_primary",
                hint="KUN_LLM_PRIMARY=codex: using codex mcp-server for top/strong/cheap",
            )
            providers["top"] = CodexMcpProvider(tier="top")
            providers["strong"] = CodexMcpProvider(tier="strong")
            providers["cheap"] = CodexMcpProvider(tier="cheap")
            return True
        if has_codex_cli:
            log.info(
                "router.codex_cli_primary",
                hint="KUN_LLM_PRIMARY=codex: using codex exec for top/strong/cheap",
            )
            providers["top"] = CodexCliProvider(tier="top")
            providers["strong"] = CodexCliProvider(tier="strong")
            providers["cheap"] = CodexCliProvider(tier="cheap")
            return True
        log.warning(
            "router.codex_primary_unavailable",
            hint="KUN_LLM_PRIMARY=codex set but codex CLI/MCP is unavailable; falling through",
        )
        return False

    def install_claude_family_for_main_tiers() -> bool:
        if has_claude_cli:
            log.info("router.claude_code_cli", hint="using logged-in claude CLI OAuth")
            providers["top"] = ClaudeCodeProvider(tier="top")
            providers["strong"] = ClaudeCodeProvider(tier="strong")
            providers["cheap"] = ClaudeCodeProvider(tier="cheap")
            return True
        if has_ofox or has_anthropic:
            providers["top"] = AnthropicProvider(model_id="claude-opus-4-7", tier="top")
            providers["strong"] = AnthropicProvider(model_id="claude-sonnet-4-6", tier="strong")
            providers["cheap"] = AnthropicProvider(
                model_id="claude-haiku-4-5-20251001",
                tier="cheap",
            )
            return True
        log.warning(
            "router.claude_primary_unavailable",
            hint="Claude primary requested but no Claude CLI/API credential is available",
        )
        return False

    def install_minimax_family_for_main_tiers() -> bool:
        if not has_minimax:
            return False
        log.info(
            "router.minimax_substitute",
            hint="MiniMax used for top/strong/cheap",
        )
        providers["top"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["top"].tier = "top"
        providers["strong"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["strong"].tier = "strong"
        providers["cheap"] = MiniMaxProvider(model_id="MiniMax-M2.7")
        providers["cheap"].tier = "cheap"
        return True

    def install_stub_family_for_main_tiers() -> None:
        log.warning("router.no_creds", hint="falling back to stub for top/strong/cheap")
        providers["top"] = StubProvider(model_id="stub-opus-4.7", tier="top")
        providers["strong"] = StubProvider(model_id="stub-sonnet-4.6", tier="strong")
        providers["cheap"] = StubProvider(model_id="stub-haiku-4.5", tier="cheap")

    # ---- top / strong / cheap ----
    installed_main = False
    if primary_family == "codex":
        installed_main = install_codex_family_for_main_tiers()
    elif primary_family in {"claude", "anthropic"}:
        installed_main = install_claude_family_for_main_tiers()
    elif primary_family == "minimax":
        installed_main = install_minimax_family_for_main_tiers()
    elif primary_family == "stub":
        install_stub_family_for_main_tiers()
        installed_main = True

    def should_consider_claude_fallback() -> bool:
        return primary_family != "codex" or allow_claude_fallback

    if installed_main:
        pass
    elif should_consider_claude_fallback() and has_claude_cli:
        log.info("router.claude_code_cli", hint="using logged-in claude CLI OAuth")
        providers["top"] = ClaudeCodeProvider(tier="top")
        providers["strong"] = ClaudeCodeProvider(tier="strong")
        providers["cheap"] = ClaudeCodeProvider(tier="cheap")
    elif should_consider_claude_fallback() and (has_ofox or has_anthropic):
        providers["top"] = AnthropicProvider(model_id="claude-opus-4-7", tier="top")
        providers["strong"] = AnthropicProvider(model_id="claude-sonnet-4-6", tier="strong")
        providers["cheap"] = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")
    elif has_minimax:
        install_minimax_family_for_main_tiers()
    else:
        install_stub_family_for_main_tiers()

    # ---- coding ----
    if has_codex_mcp:
        log.info(
            "router.codex_mcp",
            hint="using codex mcp-server (ChatGPT OAuth, GPT-5.5)",
        )
        providers["coding"] = CodexMcpProvider(tier="coding")
    elif has_codex_cli:
        # Legacy path — only works for OpenAI-API-key accounts, not ChatGPT OAuth
        log.info("router.codex_cli_legacy", hint="using codex exec (API-key path)")
        providers["coding"] = CodexCliProvider(tier="coding", model_id="gpt-5")
    elif has_openai or has_ofox:
        providers["coding"] = OpenAIProvider(model_id="gpt-5", tier="coding")
    elif should_consider_claude_fallback() and has_claude_cli:
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

    # ---- A/B alternates (optional) ----
    # KUN_AB_RATIO=0.1 + KUN_AB_TOP_CHALLENGER_TIER=strong → 10% 的 top 流量
    # 走 strong tier 的 provider, 让两档模型在同 purpose 下做对照实验.
    # 不指定就是 0% (默认关闭, 等价于现在的行为).
    ab_alternates: dict[ModelTier, LLMProvider] = {}
    for tier_name in ("top", "strong", "cheap", "coding"):
        env_key = f"KUN_AB_{tier_name.upper()}_CHALLENGER_TIER"
        challenger_tier = os.getenv(env_key)
        if not challenger_tier:
            continue
        if challenger_tier not in providers or tier_name not in providers:
            log.warning(
                "router.ab_alt_missing",
                tier=tier_name,
                challenger=challenger_tier,
            )
            continue
        ab_alternates[cast_tier(tier_name)] = providers[cast_tier(challenger_tier)]

    try:
        ab_ratio = float(os.getenv("KUN_AB_RATIO", "0") or "0")
    except ValueError:
        ab_ratio = 0.0
    if ab_alternates and ab_ratio > 0.0:
        log.info(
            "router.ab_enabled",
            ratio=ab_ratio,
            tiers=list(ab_alternates.keys()),
        )

    _router = LLMRouter(providers, ab_alternates=ab_alternates, ab_ratio=ab_ratio)
    return _router


def cast_tier(name: str) -> ModelTier:
    """Helper — annotation cast for the tier strings we already validated."""
    return name  # type: ignore[return-value]


def set_router(router: LLMRouter) -> None:
    """Override the cached router (for tests)."""
    global _router
    _router = router


def reset_router() -> None:
    """Clear cached router."""
    global _router, _route_governor
    _router = None
    _route_governor = None


def _governance_candidates(
    primary_tier: ModelTier,
    providers: dict[ModelTier, LLMProvider],
) -> list[str]:
    ordered: list[str] = []
    for tier in [primary_tier, "top", "strong", "cheap", "coding"]:
        if tier == "fallback":
            continue
        if tier in providers and tier not in ordered:
            ordered.append(tier)
    return ordered


def _governance_task_meta(
    *,
    request: LLMRequest,
    purpose: TaskPurpose,
    planned_decision: RouteDecision,
    planned_provider: LLMProvider | None,
) -> dict[str, Any]:
    profile = request.profile
    estimated = _estimate_request_cost(request, planned_provider)
    meta: dict[str, Any] = {
        "task_type": profile.task_type if profile and profile.task_type else f"llm.{purpose}",
        "purpose": purpose,
        "risk_level": profile.risk_level if profile else "low",
        "planned_tier": planned_decision.primary_tier,
        "planned_provider": planned_provider.name if planned_provider else "unknown",
        "planned_model": planned_provider.model_id
        if planned_provider
        else planned_decision.primary_tier,
        "estimated_cost_usd": estimated,
        "prompt_chars": sum(len(message.content or "") for message in request.messages),
        "max_tokens": request.max_tokens,
    }
    if profile and profile.max_cost_usd is not None:
        meta["cost_ceiling_usd"] = profile.max_cost_usd
    if profile and profile.needs_coding:
        meta["needs_coding"] = True
    if profile and profile.needs_reasoning:
        meta["needs_reasoning"] = True
    if profile and profile.force_fallback:
        meta["force_fallback"] = True
    # Keep a short redaction test surface for the governor; it will redact
    # before RuleEngine events.  Do not put full prompts into telemetry by
    # default.
    meta["prompt_preview"] = "\n".join(message.content for message in request.messages[-2:])[:800]
    return meta


def _estimate_request_cost(request: LLMRequest, provider: LLMProvider | None) -> float | None:
    if provider is None:
        return None
    input_tokens = max(1, sum(len(message.content or "") for message in request.messages) // 4)
    output_tokens = max(1, request.max_tokens)
    try:
        return provider.compute_cost(
            UsageInfo(input_tokens=input_tokens, output_tokens=output_tokens),
            equivalent=True,
        )
    except Exception:
        return None
