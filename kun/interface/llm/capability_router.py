"""Capability-aware model selection — bridge capability_card data into routing.

The LLMRouter picks a *tier* (top / strong / cheap / coding / fallback). When
multiple providers register against the same tier, this helper picks the best
one for a given (task_type, tenant) by consulting the capability_card data
populated by ``capability_writeback``.

Today every tier has exactly one provider, so this acts as a passthrough that
just annotates the rationale with the model's measured reliability. As we add
more candidates per tier — A/B experiments comparing GPT-5.5 vs Opus on
"execution" / "judge" — this helper actually starts ranking them.

Design notes:
  - Async helper so it can hit Postgres; caller is the orchestrator path
    that's already async.
  - In-memory cache with a 5 min TTL so a single task's steps don't hit the
    DB N times. ``invalidate()`` for tests.
  - Score combines reliability (success_rate weighted by sample size) +
    a freshness bonus (recently-exercised tier_default models score higher).
  - Falls back gracefully — DB error / no data → all candidates get the same
    neutral score 0.5; routing decision is unchanged.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import CapabilityCardRow
from kun.core.tenancy import MissingTenantContextError, current_tenant

log = get_logger("kun.llm.capability_router")

_CACHE_TTL_SEC = 300


@dataclass(frozen=True)
class CapabilityScore:
    """One model's measured score for a specific task type."""

    model_id: str
    task_type: str
    reliability: float  # 0..1, blended success rate
    sample_size: int  # how many calls fed this score
    score: float  # final score for sort: reliability with cold-start damping
    is_cold_start: bool


class CapabilityRouter:
    """Score candidates by their capability_card numbers."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], tuple[float, CapabilityScore]] = {}
        # key = (tenant_id, model_id, task_type), value = (expires_at, score)

    def invalidate(self) -> None:
        self._cache.clear()

    async def score_for(
        self,
        *,
        tenant_id: str,
        model_id: str,
        task_type: str,
    ) -> CapabilityScore:
        """Return the score for one model on one task_type. Cached for 5 min."""
        key = (tenant_id, model_id, task_type)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        score = await self._fetch_score(tenant_id=tenant_id, model_id=model_id, task_type=task_type)
        self._cache[key] = (now + _CACHE_TTL_SEC, score)
        return score

    async def _fetch_score(
        self,
        *,
        tenant_id: str,
        model_id: str,
        task_type: str,
    ) -> CapabilityScore:
        try:
            async with session_scope(tenant_id=tenant_id) as s:
                result = await s.execute(
                    select(CapabilityCardRow).where(
                        CapabilityCardRow.tenant_id == tenant_id,
                        CapabilityCardRow.entity_type == "model",
                        CapabilityCardRow.entity_id == model_id,
                    )
                )
                row = result.scalar_one_or_none()
        except Exception as e:
            log.debug("capability_router.fetch_failed", model_id=model_id, error=str(e))
            return _neutral_score(model_id, task_type)

        if row is None:
            return _neutral_score(model_id, task_type)

        return _project_score(row, model_id=model_id, task_type=task_type)

    async def rank_candidates(
        self,
        *,
        tenant_id: str,
        model_ids: list[str],
        task_type: str,
    ) -> list[CapabilityScore]:
        """Return the candidates sorted by score, highest first."""
        scores = []
        for mid in model_ids:
            scores.append(
                await self.score_for(tenant_id=tenant_id, model_id=mid, task_type=task_type)
            )
        scores.sort(key=lambda s: s.score, reverse=True)
        return scores

    async def score_model(self, task_type: str, model_id: str) -> float:
        """Compatibility hook consumed by ``LLMRouteGovernor``.

        The governor sits inside the router hot path and only knows the
        candidate ids it is asked to choose from.  This adapter lets it consult
        the same capability-card data without learning about database rows.
        When no tenant context exists, return neutral so routing stays stable.
        """

        tenant_id = _current_tenant_id_or_none()
        if not tenant_id:
            return 0.5
        return (
            await self.score_for(
                tenant_id=tenant_id,
                model_id=model_id,
                task_type=task_type,
            )
        ).score

    async def model_scores(self, task_type: str, candidate_models: list[str]) -> dict[str, float]:
        """Batch compatibility hook for ``LLMRouteGovernor``."""

        tenant_id = _current_tenant_id_or_none()
        if not tenant_id:
            return dict.fromkeys(candidate_models, 0.5)
        ranked = await self.rank_candidates(
            tenant_id=tenant_id,
            model_ids=candidate_models,
            task_type=task_type,
        )
        return {item.model_id: item.score for item in ranked}

    async def rank_candidates_anchor_then_expand(
        self,
        *,
        tenant_id: str,
        model_ids: list[str],
        task_type: str,
        max_rounds: int = 3,
    ) -> AsyncIterator[CapabilityScore]:
        """按能力画像分数流式返回模型候选.

        第一轮只返回最高分模型; 调用方觉得不够再继续展开后续模型.
        老的 ``rank_candidates`` 保持不变, 方便现有路由继续一次性排序.

        # TODO: wire by Claude in V2.2
        """
        ranked = await self.rank_candidates(
            tenant_id=tenant_id,
            model_ids=model_ids,
            task_type=task_type,
        )
        if not ranked:
            return

        async def anchor_fn() -> CapabilityScore:
            return ranked[0]

        async def expand_fn(
            _anchor: CapabilityScore,
            prior: list[CapabilityScore],
        ) -> CapabilityScore | None:
            seen = {item.model_id for item in prior}
            return next((item for item in ranked if item.model_id not in seen), None)

        async for item in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield item


def _neutral_score(model_id: str, task_type: str) -> CapabilityScore:
    return CapabilityScore(
        model_id=model_id,
        task_type=task_type,
        reliability=0.5,
        sample_size=0,
        score=0.5,
        is_cold_start=True,
    )


def _project_score(
    row: Any,
    *,
    model_id: str,
    task_type: str,
) -> CapabilityScore:
    """Pick the per-task-type stats out of the JSON card and fold into one score.

    Cold start damping: a model that's only been called 5 times shouldn't
    automatically beat one with 200 calls and 0.7 reliability. We blend the
    measured reliability toward 0.5 by sample size.
    """
    card = row.card_json or {}
    capabilities = card.get("capabilities") or []

    matching: dict[str, Any] | None = None
    for cap in capabilities:
        if isinstance(cap, dict) and cap.get("task_type") == task_type:
            matching = cap
            break

    if matching is None:
        # No matching task_type yet — use overall_reliability as the prior
        overall = float(getattr(row, "overall_reliability", 0.0) or 0.5)
        return CapabilityScore(
            model_id=model_id,
            task_type=task_type,
            reliability=overall,
            sample_size=0,
            score=0.5 + (overall - 0.5) * 0.2,  # mild pull toward 0.5
            is_cold_start=True,
        )

    stats = matching.get("stats") or {}
    success_rate = float(stats.get("success_rate") or 0.0)
    n = int(stats.get("total_invocations") or 0)

    # Damp by sample size: small n → score closer to 0.5 (neutral).
    weight = min(1.0, n / 30.0)
    score = 0.5 + (success_rate - 0.5) * weight

    return CapabilityScore(
        model_id=model_id,
        task_type=task_type,
        reliability=success_rate,
        sample_size=n,
        score=score,
        is_cold_start=n < 5,
    )


# ---- module-level singleton (cheap to keep, in-memory cache) ----

_router_singleton: CapabilityRouter | None = None


def get_capability_router() -> CapabilityRouter:
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = CapabilityRouter()
    return _router_singleton


def reset_capability_router() -> None:
    global _router_singleton
    _router_singleton = None


def _current_tenant_id_or_none() -> str | None:
    try:
        return current_tenant().tenant_id
    except MissingTenantContextError:
        return None


__all__ = [
    "CapabilityRouter",
    "CapabilityScore",
    "get_capability_router",
    "reset_capability_router",
]
