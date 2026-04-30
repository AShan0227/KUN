"""Context packer — preheat relevant L1/L2 assets for a task."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from kun.context.assets import AssetKind, LayeredAsset
from kun.context.importance import ImportanceScore, ImportanceScorer
from kun.context.storage import AssetStore, get_store
from kun.datamodel.task import TaskRef

log = logging.getLogger(__name__)


class PackedContextItem(BaseModel):
    """One asset selected for the execution context."""

    asset_id: str
    asset_kind: AssetKind
    relevance_score: float = Field(ge=0.0)
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    score_rationale: str = ""


class ContextPack(BaseModel):
    """A small, prompt-ready slice of the context system."""

    items: list[PackedContextItem] = Field(default_factory=list)

    def summary(self, *, max_chars: int = 1800) -> str:
        if not self.items:
            return ""
        lines = ["相关上下文资产 (L1/L2 摘要):"]
        for item in self.items:
            title = f"{item.asset_kind}:{item.asset_id}"
            if item.title:
                title = f"{title} — {item.title}"
            lines.append(f"- {title}")
            if item.tags:
                lines.append(f"  tags: {', '.join(item.tags[:6])}")
            if item.summary:
                lines.append(f"  summary: {item.summary}")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20].rstrip() + "\n...<truncated>"


class ContextPacker:
    """Select relevant context assets without calling an LLM."""

    def __init__(self, store: AssetStore | None = None) -> None:
        self._store = store or get_store()
        self._scorer = ImportanceScorer()

    async def pack_query(
        self,
        query: str,
        *,
        tenant_id: str,
        kinds: Iterable[AssetKind] | None = None,
        limit: int = 3,
    ) -> ContextPack:
        """Wire 33: 按 query 字符串拉相关 context (不依赖 task_ref).

        给 hermes ExecutionStep.action_type="use_memory" 用 — LLM 提供
        payload.query, 我们拉 memory/knowledge 加塞进 step prompt.

        异常 / 空 query → 返空 ContextPack (不爆 step 主流程).
        """
        query_terms = _terms(query)
        if not query_terms:
            return ContextPack()

        import logging

        logger = logging.getLogger(__name__)
        candidates: list[LayeredAsset] = []
        for kind in kinds or ("memory", "knowledge", "methodology"):
            try:
                candidates.extend(
                    await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100)
                )
            except Exception as exc:
                logger.debug("packer.pack_query store_list_failed kind=%s err=%s", kind, exc)
                continue

        scored = await self._rank_assets(candidates, query=query)
        selected = scored[:limit]
        await self._touch_selected(selected)
        return ContextPack(items=[_to_packed(score, asset) for asset, score in selected])

    async def pack(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
        kinds: Iterable[AssetKind] | None = None,
        limit: int = 5,
    ) -> ContextPack:
        query_terms = _task_terms(task_ref)
        if not query_terms:
            return ContextPack()

        candidates: list[LayeredAsset] = []
        for kind in kinds or ("memory", "knowledge", "methodology", "role_template", "skill"):
            candidates.extend(
                await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100)
            )

        query = _task_query(task_ref)
        scored = await self._rank_assets(candidates, query=query)
        selected = scored[:limit]
        await self._touch_selected(selected)
        return ContextPack(items=[_to_packed(score, asset) for asset, score in selected])

    def pack_anchor_then_expand(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
        kinds: Iterable[AssetKind] | None = None,
        max_rounds: int = 3,
        use_marginal_stop: bool = True,
    ) -> Any:
        """V2.2 §19.3 + §20.3: 按需扩展式 context 装载.

        不一次拉满 limit 条, 而是流式 yield PackedContextItem:
        - 第 1 轮: 评分最高的 anchor 资产
        - 后续: 沿 score 排序的次优, ≤ max_rounds
        - marginal_stop: 下一条 score 跌幅大就停 (避免拉一堆低相关性)

        用法:
            async for item in packer.pack_anchor_then_expand(task_ref, tenant_id=tid):
                add_to_prompt(item)
                if context_token_budget_low: break

        Returns:
            AsyncIterator[PackedContextItem]
        """
        from kun.core.anchor_expand import AnchorExpandIterator
        from kun.engineering.marginal_roi import (
            MarginalROIStopCriterion,
            ValueEstimator,
        )

        async def _build_scored() -> list[tuple[LayeredAsset, ImportanceScore]]:
            query_terms = _task_terms(task_ref)
            if not query_terms:
                return []
            candidates: list[LayeredAsset] = []
            for kind in kinds or ("memory", "knowledge", "methodology", "role_template", "skill"):
                candidates.extend(
                    await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100)
                )
            return await self._rank_assets(candidates, query=_task_query(task_ref))

        scored_cache: list[list[tuple[LayeredAsset, ImportanceScore]]] = []  # 避免多次 fetch

        async def anchor_fn() -> PackedContextItem:
            if not scored_cache:
                scored_cache.append(await _build_scored())
            scored = scored_cache[0]
            if not scored:
                raise StopAsyncIteration
            asset, score = scored[0]
            await self._touch_selected([(asset, score)])
            return _to_packed(score, asset)

        async def expand_fn(
            anchor: PackedContextItem, prior: list[PackedContextItem]
        ) -> PackedContextItem | None:
            scored = scored_cache[0]
            idx = len(prior)
            if idx >= len(scored):
                return None
            asset, score = scored[idx]
            await self._touch_selected([(asset, score)])
            return _to_packed(score, asset)

        criterion: MarginalROIStopCriterion | None = None
        estimator: ValueEstimator | None = None
        if use_marginal_stop:
            # 下一条 relevance_score 跌幅 > 0.3 → 停 (说明已经到弱相关层)
            criterion = MarginalROIStopCriterion(
                delta_threshold=-0.3,
                window_k=1,
                min_steps=2,
            )
            estimator = ValueEstimator(
                custom_fn=lambda item, prior: float(item.relevance_score),
            )

        return AnchorExpandIterator(
            anchor_fn=anchor_fn,
            expand_fn=expand_fn,
            max_rounds=max_rounds,
            stop_criterion=criterion,
            value_estimator=estimator,
        )

    async def _rank_assets(
        self,
        candidates: list[LayeredAsset],
        *,
        query: str,
    ) -> list[tuple[LayeredAsset, ImportanceScore]]:
        """统一用 ImportanceScorer 排序，并接入历史贡献度。

        这里是 V4 里的关键补线：过去 contribution / credit 已经会累计，
        但 ContextPacker 没吃到，资产选择仍像关键词搜索。现在相关资产之间
        会按 semantic/frequency/recency/contribution 综合排序。
        """
        if not candidates:
            return []

        from kun.engineering.credit_assignment import get_contribution_tracker

        tracker = get_contribution_tracker()
        kind_by_asset_id = {asset.asset_id: asset.asset_kind for asset in candidates}
        scored = await self._scorer.score_with_contribution_boost(
            candidates,
            query=query,
            contribution_lookup=lambda asset_id: tracker.contribution_score(
                asset_id, str(kind_by_asset_id.get(asset_id, "memory"))
            ),
        )
        scored = [(asset, _quality_adjusted_score(asset, score)) for asset, score in scored]
        filtered = [
            (asset, score)
            for asset, score in scored
            if score.semantic > 0 or score.dependency > 0 or score.pin > 0
        ]
        filtered.sort(key=lambda item: (-item[1].overall, item[0].asset_id))
        return filtered

    async def _touch_selected(
        self,
        selected: list[tuple[LayeredAsset, ImportanceScore]],
    ) -> None:
        for asset, _score in selected:
            try:
                asset.touch()
                await self._store.put(asset)
            except Exception:
                log.debug("context_packer.touch_selected_failed", exc_info=True)
                continue


def _task_terms(task_ref: TaskRef) -> set[str]:
    parts = [
        task_ref.meta.task_type,
        task_ref.meta.success_criteria_short,
    ]
    if task_ref.spec is not None:
        parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.success_metrics),
                " ".join(task_ref.spec.required_skills),
                " ".join(task_ref.spec.required_tools),
            ]
        )
    return _terms(" ".join(parts))


def _task_query(task_ref: TaskRef) -> str:
    parts = [
        task_ref.meta.task_type,
        task_ref.meta.success_criteria_short,
    ]
    if task_ref.spec is not None:
        parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.success_metrics),
                " ".join(task_ref.spec.required_skills),
                " ".join(task_ref.spec.required_tools),
            ]
        )
    return " ".join(part for part in parts if part)


def _to_packed(score: ImportanceScore, asset: LayeredAsset) -> PackedContextItem:
    return PackedContextItem(
        asset_id=asset.asset_id,
        asset_kind=asset.asset_kind,
        relevance_score=score.overall,
        title=str(asset.l1_metadata.get("title") or asset.l1_metadata.get("name") or ""),
        tags=asset.tags,
        summary=asset.l2_summary or _metadata_summary(asset),
        score_rationale=score.rationale,
    )


def _quality_adjusted_score(asset: LayeredAsset, score: ImportanceScore) -> ImportanceScore:
    """Adjust context ranking by outcome quality, not just text match.

    This keeps failed or low-confidence memories from dominating future context
    just because they share keywords.
    """

    meta = asset.l1_metadata or {}
    quality_delta = 0.0
    validation_outcome = str(meta.get("validation_outcome") or meta.get("outcome") or "").lower()
    if validation_outcome in {"fail", "failed"}:
        quality_delta -= 0.20
    elif validation_outcome == "partial":
        quality_delta -= 0.05
    elif validation_outcome in {"pass", "passed", "done"}:
        quality_delta += 0.08

    for key in ("score_overall", "validation_score", "rubric_score"):
        raw = meta.get(key)
        if isinstance(raw, int | float):
            value = float(raw)
            if key == "rubric_score" and value > 1.0:
                value = value / 5.0
            quality_delta += max(-0.10, min(0.15, (value - 0.5) * 0.20))
            break

    surprise = meta.get("surprise_score")
    if isinstance(surprise, int | float) and float(surprise) >= 0.75:
        quality_delta += 0.05

    if quality_delta == 0:
        return score
    adjusted = max(0.0, min(1.0, score.overall + quality_delta))
    return ImportanceScore(
        overall=adjusted,
        semantic=score.semantic,
        frequency=score.frequency,
        recency=score.recency,
        dependency=score.dependency,
        pin=score.pin,
        contribution=score.contribution,
        rationale=f"{score.rationale}; quality_delta={quality_delta:+.2f}",
    )


def _score_asset(asset: LayeredAsset, query_terms: set[str]) -> float:
    asset_terms = _terms(
        " ".join(
            [
                asset.asset_kind,
                " ".join(asset.tags),
                asset.l2_summary or "",
                " ".join(str(v) for v in asset.l1_metadata.values()),
            ]
        )
    )
    overlap = len(query_terms & asset_terms)
    if overlap == 0:
        return 0.0
    tag_bonus = len(query_terms & {tag.lower() for tag in asset.tags})
    access_bonus = min(asset.access_count, 10) / 20.0
    return float(overlap + tag_bonus + access_bonus)


def _metadata_summary(asset: LayeredAsset) -> str:
    if not asset.l1_metadata:
        return ""
    items = list(asset.l1_metadata.items())[:4]
    return "; ".join(f"{key}={value}" for key, value in items)


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[\w.-]+", text) if len(part) >= 2}


__all__ = ["ContextPack", "ContextPacker", "PackedContextItem"]
