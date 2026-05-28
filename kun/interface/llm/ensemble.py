"""V7 §11.4 multi-LLM ensemble — parallel invoke + consensus + divergence.

KUN V7 核心独特价值: multi-LLM ensemble × Claude Code 工程纪律 × RSI 持续进化.
本 module 是 multi-LLM 并行调用的 primitive, 服务于:

- 启 (Qi) Explorer Pool 3 模式并行候选 (§9.6)
- External Supervisor 跨 family critique (§10.2.3 / §16.6)
- Mission Director 高风险决策 ensemble vote (§9.7)

API contract (V7 §11.4):

    EnsembleResponse = ensemble_invoke(request, providers, ...)

输入:
- request: LLMRequest
- providers: list[LLMProvider] (至少 2, 至少 2 family — cross-family 强 enforce)
- consensus_strategy: 'majority_vote' / 'weighted' / 'pick_best_by_metric'
- divergence_threshold: 偏离阈值, 超过触发 Mission Director 告警

输出 EnsembleResponse:
- responses: list[LLMResponse] (每个 provider 一个)
- consensus: LLMResponse | None (按 consensus_strategy 计算的共识)
- divergence_score: float (0-1, 0=全一致, 1=完全分歧)
- divergence_signals: list[str] (具体分歧维度)
- usage: AggregateUsage (多 provider 总 token / cost)
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from kun.core.logging import get_logger
from kun.interface.llm.base import LLMProvider, LLMRequest, LLMResponse, UsageInfo
from kun.interface.llm.cross_family import (
    CrossFamilyConfigError,
    classify_family,
    is_cross_family,
)

log = get_logger("kun.interface.llm.ensemble")


class ConsensusStrategy(StrEnum):
    """How to derive a consensus response from N ensemble responses."""

    MAJORITY_VOTE = "majority_vote"  # 文本相似性聚类, 选最大类
    WEIGHTED = "weighted"  # 按 provider tier 权重投票 (top > strong > cheap)
    PICK_BEST_BY_METRIC = "pick_best_by_metric"  # 综合评分 (质量 60 + 速度 25 + 成本 15)


@dataclass(frozen=True)
class AggregateUsage:
    """Aggregate usage across all providers in an ensemble call."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_cost_usd_actual: float = 0.0
    total_cost_usd_equivalent: float = 0.0
    per_provider: dict[str, UsageInfo] = field(default_factory=dict)


@dataclass(frozen=True)
class EnsembleResponse:
    """Result of an ensemble_invoke call.

    Per V7 §11.4 API contract.
    """

    responses: list[LLMResponse]  # 每个 provider 一个
    consensus: LLMResponse | None  # 共识结果 (按 strategy 计算)
    divergence_score: float  # 0-1, 0=全一致, 1=完全分歧
    divergence_signals: list[str]  # 具体分歧字段 / 内容描述
    usage: AggregateUsage
    consensus_strategy: ConsensusStrategy


def _normalize_content(text: str) -> str:
    """Normalize text for similarity comparison: lowercase + collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _content_similarity(a: str, b: str) -> float:
    """Quick similarity score between two strings, 0-1.

    Uses token-set Jaccard (simple, robust, no external deps). 1.0 = identical.
    """
    a_norm = _normalize_content(a)
    b_norm = _normalize_content(b)
    if not a_norm and not b_norm:
        return 1.0
    if not a_norm or not b_norm:
        return 0.0
    tokens_a = set(a_norm.split())
    tokens_b = set(b_norm.split())
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union > 0 else 0.0


def _compute_divergence(responses: list[LLMResponse]) -> tuple[float, list[str]]:
    """Compute divergence_score (0-1) and divergence_signals from N responses.

    0 = all responses content-identical
    1 = all responses content-disjoint (or only 1 response)
    Mid = some overlap, some divergence
    """
    if len(responses) <= 1:
        return 0.0, []

    # Pairwise content similarity
    n = len(responses)
    total_sim = 0.0
    pair_count = 0
    signals: list[str] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = _content_similarity(responses[i].content, responses[j].content)
            total_sim += sim
            pair_count += 1
            if sim < 0.3:
                signals.append(
                    f"low similarity ({sim:.2f}) between "
                    f"{responses[i].provider}/{responses[i].model} and "
                    f"{responses[j].provider}/{responses[j].model}"
                )

    avg_sim = total_sim / pair_count if pair_count > 0 else 0.0
    divergence = 1.0 - avg_sim

    # Also check structural divergence — different tool_calls counts
    tool_call_counts = [len(r.tool_calls) for r in responses]
    if len(set(tool_call_counts)) > 1:
        signals.append(f"tool_call count varies: {tool_call_counts}")

    # Finish reason divergence
    finish_reasons = [r.finish_reason for r in responses]
    if len(set(finish_reasons)) > 1:
        signals.append(f"finish_reason varies: {finish_reasons}")

    return min(max(divergence, 0.0), 1.0), signals


def _pick_consensus_majority_vote(responses: list[LLMResponse]) -> LLMResponse | None:
    """Cluster by content similarity, pick the largest cluster's representative."""
    if not responses:
        return None
    if len(responses) == 1:
        return responses[0]

    # Simple clustering: group responses where pairwise similarity >= 0.6
    similarity_threshold = 0.6
    n = len(responses)
    cluster_id = list(range(n))  # initially each in own cluster
    for i in range(n):
        for j in range(i + 1, n):
            if (
                _content_similarity(responses[i].content, responses[j].content)
                >= similarity_threshold
            ):
                # Merge cluster: i and j go to same cluster_id
                target = cluster_id[i]
                source = cluster_id[j]
                if target != source:
                    for k in range(n):
                        if cluster_id[k] == source:
                            cluster_id[k] = target

    # Pick largest cluster
    counter = Counter(cluster_id)
    biggest_cluster_id = counter.most_common(1)[0][0]
    # Return first response in that cluster (deterministic)
    for i in range(n):
        if cluster_id[i] == biggest_cluster_id:
            return responses[i]
    return responses[0]


def _pick_consensus_weighted(responses: list[LLMResponse]) -> LLMResponse | None:
    """Weight by tier (top > strong > cheap > coding > fallback), pick highest weight."""
    if not responses:
        return None
    weights = {"top": 5, "strong": 4, "coding": 3, "cheap": 2, "fallback": 1}
    best = max(
        responses,
        key=lambda r: weights.get(r.tier, 0),
    )
    return best


def _pick_consensus_best_by_metric(
    responses: list[LLMResponse],
    quality_score_fn: Callable[[LLMResponse], float] | None = None,
) -> LLMResponse | None:
    """Pick by 综合评分: 质量 60% + 速度 25% + 成本 15% (V7 §12.4.3).

    quality_score_fn (caller-injected) 给 0-1 quality 分. None → 用 finish_reason
    粗略估 (stop=1.0, length=0.7, tool_use=0.9, error=0.0).
    """
    if not responses:
        return None
    # Speed = 1 / latency (normalized to max latency in the set)
    max_latency = max(r.latency_ms for r in responses) or 1.0
    # Cost = 1 - (cost / max_cost) (normalized)
    max_cost = max(r.cost_usd_equivalent for r in responses) or 1.0

    def _quality(r: LLMResponse) -> float:
        if quality_score_fn is not None:
            return float(quality_score_fn(r))
        return {
            "stop": 1.0,
            "end_turn": 1.0,
            "tool_use": 0.9,
            "length": 0.7,
            "error": 0.0,
        }.get(r.finish_reason, 0.5)

    def _score(r: LLMResponse) -> float:
        q = _quality(r)
        s = 1.0 - (r.latency_ms / max_latency)  # higher = faster
        c = 1.0 - (r.cost_usd_equivalent / max_cost)  # higher = cheaper
        return 0.6 * q + 0.25 * s + 0.15 * c

    return max(responses, key=_score)


def _aggregate_usage(responses: list[LLMResponse]) -> AggregateUsage:
    per_provider: dict[str, UsageInfo] = {}
    total_in = 0
    total_out = 0
    total_cached = 0
    total_actual = 0.0
    total_equiv = 0.0
    for r in responses:
        key = f"{r.provider}/{r.model}"
        per_provider[key] = r.usage
        total_in += r.usage.input_tokens
        total_out += r.usage.output_tokens
        total_cached += r.usage.cached_input_tokens or 0
        total_actual += r.cost_usd_actual
        total_equiv += r.cost_usd_equivalent
    return AggregateUsage(
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cached_tokens=total_cached,
        total_cost_usd_actual=total_actual,
        total_cost_usd_equivalent=total_equiv,
        per_provider=per_provider,
    )


async def ensemble_invoke(
    request: LLMRequest,
    providers: list[LLMProvider],
    *,
    consensus_strategy: ConsensusStrategy = ConsensusStrategy.MAJORITY_VOTE,
    divergence_threshold: float = 0.5,
    require_cross_family: bool = True,
    quality_score_fn: Callable[[LLMResponse], float] | None = None,
    on_divergence: Callable[[float, list[str]], Awaitable[None]] | None = None,
) -> EnsembleResponse:
    """Parallel invoke N providers with the same request, return ensemble result.

    Args:
        request: LLMRequest to send to all providers.
        providers: ≥ 2 LLMProvider instances. ≥ 2 different family if cross_family
            required (V7 §11.1).
        consensus_strategy: how to derive consensus from N responses.
        divergence_threshold: if divergence_score > this, fire on_divergence callback.
        require_cross_family: if True (default), validate ≥ 2 family in providers.
        quality_score_fn: optional, used by PICK_BEST_BY_METRIC strategy.
        on_divergence: optional async callback fired when divergence > threshold.

    Returns:
        EnsembleResponse with all responses + consensus + divergence info.

    Raises:
        ValueError: if providers has < 2 entries.
        CrossFamilyConfigError: if require_cross_family=True and all providers
            from same family.
    """
    if len(providers) < 2:
        raise ValueError(
            f"ensemble_invoke requires ≥ 2 providers, got {len(providers)}"
        )

    # Cross-family validation (V7 §11.2). At least 1 pair must be cross-family.
    # UNKNOWN family is treated as cross (defensive — V7 §11.2 + cross_family.py).
    if require_cross_family:
        any_cross_pair = False
        for i in range(len(providers)):
            for j in range(i + 1, len(providers)):
                if is_cross_family(providers[i].model_id, providers[j].model_id):
                    any_cross_pair = True
                    break
            if any_cross_pair:
                break
        if not any_cross_pair:
            # All providers same known family
            families = {classify_family(p.model_id).value for p in providers}
            family_value = next(iter(families))
            raise CrossFamilyConfigError(
                f"ensemble_invoke require_cross_family=True but all "
                f"{len(providers)} providers are in same family ({family_value}). "
                f"V7 §11.2 cross-family 强制约束."
            )

    # Parallel invoke
    log.info(
        "ensemble.invoke.start",
        n_providers=len(providers),
        providers=[f"{p.name}/{p.model_id}" for p in providers],
        consensus_strategy=consensus_strategy.value,
    )

    # Use return_exceptions so one provider's failure doesn't take down the whole ensemble
    results = await asyncio.gather(
        *(p.invoke(request) for p in providers),
        return_exceptions=True,
    )

    # Separate successful responses from exceptions
    responses: list[LLMResponse] = []
    failures: list[str] = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            failures.append(f"{providers[i].name}/{providers[i].model_id}: {result}")
            log.warning(
                "ensemble.provider_failed",
                provider=providers[i].name,
                model=providers[i].model_id,
                error=str(result),
            )
        else:
            responses.append(result)

    if not responses:
        raise RuntimeError(
            f"ensemble_invoke: all {len(providers)} providers failed: {failures}"
        )

    # Compute divergence
    divergence_score, divergence_signals = _compute_divergence(responses)
    if failures:
        divergence_signals.append(f"{len(failures)} providers failed: {failures}")

    # Pick consensus per strategy
    consensus: LLMResponse | None
    if consensus_strategy == ConsensusStrategy.MAJORITY_VOTE:
        consensus = _pick_consensus_majority_vote(responses)
    elif consensus_strategy == ConsensusStrategy.WEIGHTED:
        consensus = _pick_consensus_weighted(responses)
    elif consensus_strategy == ConsensusStrategy.PICK_BEST_BY_METRIC:
        consensus = _pick_consensus_best_by_metric(responses, quality_score_fn)
    else:
        consensus = responses[0]  # fallback

    log.info(
        "ensemble.invoke.done",
        n_responses=len(responses),
        n_failures=len(failures),
        divergence_score=round(divergence_score, 3),
        consensus_provider=f"{consensus.provider}/{consensus.model}" if consensus else None,
    )

    # Fire divergence callback if threshold crossed
    if on_divergence is not None and divergence_score > divergence_threshold:
        try:
            await on_divergence(divergence_score, divergence_signals)
        except Exception as e:
            log.warning("ensemble.on_divergence_failed", error=str(e))

    return EnsembleResponse(
        responses=responses,
        consensus=consensus,
        divergence_score=divergence_score,
        divergence_signals=divergence_signals,
        usage=_aggregate_usage(responses),
        consensus_strategy=consensus_strategy,
    )


__all__ = [
    "AggregateUsage",
    "ConsensusStrategy",
    "EnsembleResponse",
    "ensemble_invoke",
]


# Verify cross-family check works
def _check_can_run_cross_family(providers: list[LLMProvider]) -> bool:
    """Quick helper for callers wanting to gate before invoke."""
    if len(providers) < 2:
        return False
    for i in range(len(providers)):
        for j in range(i + 1, len(providers)):
            if is_cross_family(providers[i].model_id, providers[j].model_id):
                return True
    return False
