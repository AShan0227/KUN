"""StrategyMatcher → router model_select 决策点桥 (V2.1 wire M3.3).

把 V1 router.decide() 的产出 (4 层决策) 包装成 §17 StrategyMatcher 决策点
candidates, 让 §17.3 strategy_score 公式有机会 override.

opt-in 模式 (默认 off):
- KUN_STRATEGY_MATCHER_ENABLED=1 启用
- 启用后: router.decide() 拿现有 primary tier 作为 baseline candidate, 加
  其他 tier 作为 alt candidate, 走 strategy_score 排序.
- 禁用: 完全走 V1 4 层决策.

为什么 opt-in:
- 现有 ~500 单测都基于老 4 层行为, 强制切换会全炸.
- M3.3 阶段验证, M4 默认开.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.strategy_matcher import (
    DecisionKind,
    SignalBundle,
    StrategyCandidate,
    StrategyDecision,
    StrategyMatcher,
    get_matcher,
)
from kun.interface.llm.base import LLMRequest
from kun.interface.llm.router import RouteDecision, TaskPurpose

logger = logging.getLogger(__name__)


# tier 估算成本表 (USD per call, 粗估; 实际从 capability_card 拉)
TIER_COST_ESTIMATE: dict[str, float] = {
    "top": 0.05,  # Opus
    "strong": 0.012,  # Sonnet
    "cheap": 0.001,  # Haiku
    "coding": 0.0,  # codex MCP, $0 via subscription
    "fallback": 0.005,  # MiniMax
}

# tier 估算延迟 (sec)
TIER_LATENCY_ESTIMATE: dict[str, float] = {
    "top": 8.0,
    "strong": 3.0,
    "cheap": 1.2,
    "coding": 5.5,
    "fallback": 2.5,
}

# tier 估算成果 (0-1, 默认; 实际从 capability_card)
TIER_OUTCOME_ESTIMATE: dict[str, float] = {
    "top": 0.92,
    "strong": 0.82,
    "cheap": 0.65,
    "coding": 0.85,
    "fallback": 0.70,
}


def is_enabled() -> bool:
    """检查 StrategyMatcher 是否启用 (默认 off)."""
    return os.getenv("KUN_STRATEGY_MATCHER_ENABLED", "0") == "1"


def build_signal_bundle(
    purpose: TaskPurpose,
    request: LLMRequest | None,
    profile: Any = None,
    user_id: str | None = None,
) -> SignalBundle:
    """从 router 调用上下文构造 SignalBundle.

    M3.3: 加 user_id 参数, 启用 SoulFile 时从灵魂档案拉用户偏好.
    M4: 接 capability_card 历史数据.
    """
    task: dict[str, Any] = {"task_type": f"router.{purpose}"}
    user: dict[str, Any] = {}

    if profile is not None:
        risk = getattr(profile, "risk_level", None)
        if risk:
            task["risk_level"] = risk
        if getattr(profile, "needs_coding", False):
            task["task_type"] = "coding"
        if getattr(profile, "prefer_speed", False):
            user["speed_sensitivity"] = "high"

    if request is not None:
        msgs = getattr(request, "messages", []) or []
        text_len = sum(len(getattr(m, "content", "") or "") for m in msgs)
        task["estimated_tokens"] = max(1, text_len // 4)  # 粗估
        if text_len > 3000:
            task["complexity_score"] = 0.7
        elif text_len > 1500:
            task["complexity_score"] = 0.5
        else:
            task["complexity_score"] = 0.3

    # V2.1 wire M3.3: SoulFile 接入
    if user_id:
        try:
            from kun.datamodel.soul_file_provider import (
                get_soul_file,
                soul_file_to_signal_user_dict,
            )
            from kun.datamodel.soul_file_provider import (
                is_enabled as _soul_enabled,
            )

            if _soul_enabled():
                soul = get_soul_file(user_id)
                # SoulFile.user 字段覆盖到 SignalBundle.user (灵魂档案优先)
                soul_user = soul_file_to_signal_user_dict(soul)
                soul_user.update(user)  # profile 显式 override 仍然生效
                user = soul_user
        except Exception:
            logger.exception("soul_file injection failed (non-fatal)")

    return SignalBundle(task=task, user=user)


async def _enumerate_model_candidates(
    signals: SignalBundle,
    previous_decisions: dict[DecisionKind, StrategyDecision],
) -> list[StrategyCandidate]:
    """model_select 的候选枚举器: 4 个 tier 都列出."""
    candidates = []
    for tier in ("top", "strong", "cheap", "coding", "fallback"):
        candidates.append(
            StrategyCandidate(
                candidate_id=f"tier:{tier}",
                description=f"router tier = {tier}",
                expected_outcome=TIER_OUTCOME_ESTIMATE.get(tier, 0.7),
                expected_cost_usd=TIER_COST_ESTIMATE.get(tier, 0.01),
                expected_latency_sec=TIER_LATENCY_ESTIMATE.get(tier, 3.0),
                risk_penalty=0.10 if tier == "fallback" else 0.0,
                metadata={"tier": tier},
            )
        )
    return candidates


async def enumerate_model_candidates_anchor_then_expand(
    signals: SignalBundle,
    previous_decisions: dict[DecisionKind, StrategyDecision] | None = None,
    *,
    max_rounds: int = 3,
) -> AsyncIterator[StrategyCandidate]:
    """model_select 候选的 anchor-expand 新接口.

    保留 ``_enumerate_model_candidates`` 给 StrategyMatcher 老路径使用; 这个接口给
    Claude 后续 wire 时按需拉 tier 候选, 避免每次都把 5 个 tier 全塞进下游.

    # TODO: wire by Claude in V2.2
    """
    candidates = await _enumerate_model_candidates(signals, previous_decisions or {})
    if not candidates:
        return

    matcher = StrategyMatcher()
    weights = matcher.compute_weights(signals)
    ranked = sorted(
        candidates,
        key=lambda c: matcher.score(c, weights).score,
        reverse=True,
    )

    async def anchor_fn() -> StrategyCandidate:
        return ranked[0]

    async def expand_fn(
        _anchor: StrategyCandidate,
        prior: list[StrategyCandidate],
    ) -> StrategyCandidate | None:
        seen = {item.candidate_id for item in prior}
        return next((item for item in ranked if item.candidate_id not in seen), None)

    async for item in AnchorExpandIterator(
        anchor_fn,
        expand_fn,
        max_rounds=max_rounds,
    ):
        yield item


def ensure_registered(matcher: StrategyMatcher | None = None) -> None:
    """注册 model_select enumerator. 应在 lifespan startup 调用一次."""
    m = matcher or get_matcher()
    # check if already registered
    if "model_select" in getattr(m, "_enumerators", {}):
        return
    m.register("model_select", _enumerate_model_candidates)


async def maybe_override_with_strategy(
    base_decision: RouteDecision,
    purpose: TaskPurpose,
    request: LLMRequest | None,
    profile: Any = None,
    user_id: str | None = None,
) -> RouteDecision:
    """如果启用了 StrategyMatcher, 用 §17.3 公式重排 tier 选择.

    保留原 RouteDecision 结构, 只换 primary_tier (如果 strategy 推荐不同).
    rationale 加 strategy_score 来源.
    """
    if not is_enabled():
        return base_decision

    matcher = get_matcher()
    ensure_registered(matcher)
    signals = build_signal_bundle(purpose, request, profile, user_id=user_id)

    try:
        decision = await matcher.decide("model_select", signals)
    except Exception:
        logger.exception("strategy_matcher decide failed, falling back to V1 router")
        return base_decision

    chosen_tier = decision.chosen.metadata.get("tier", base_decision.primary_tier)
    if chosen_tier == base_decision.primary_tier:
        # 一致, 不动
        return base_decision

    return RouteDecision(
        purpose=base_decision.purpose,
        primary_tier=chosen_tier,
        fallback_tier=base_decision.fallback_tier,
        rationale=(
            base_decision.rationale
            + f" | strategy_matcher: {base_decision.primary_tier}→{chosen_tier} "
            + f"(score breakdown: {decision.score_breakdown})"
        ),
    )


__all__ = [
    "TIER_COST_ESTIMATE",
    "TIER_LATENCY_ESTIMATE",
    "TIER_OUTCOME_ESTIMATE",
    "build_signal_bundle",
    "ensure_registered",
    "enumerate_model_candidates_anchor_then_expand",
    "is_enabled",
    "maybe_override_with_strategy",
]
