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


class PackedProcessExperience(BaseModel):
    """One recalled execution-process hint for prompt context."""

    asset_id: str
    summary: str
    similarity_score: float = Field(ge=0.0)


class ContextPack(BaseModel):
    """A small, prompt-ready slice of the context system."""

    items: list[PackedContextItem] = Field(default_factory=list)
    process_experiences: list[PackedProcessExperience] = Field(default_factory=list)

    def summary(self, *, max_chars: int = 1800) -> str:
        if not self.items and not self.process_experiences:
            return ""
        lines: list[str] = []
        if self.items:
            lines.append("相关上下文资产 (L1/L2 摘要):")
            for item in self.items:
                title = f"{item.asset_kind}:{item.asset_id}"
                if item.title:
                    title = f"{title} — {item.title}"
                lines.append(f"- {title}")
                if item.tags:
                    lines.append(f"  tags: {', '.join(item.tags[:6])}")
                if item.summary:
                    lines.append(f"  summary: {item.summary}")
        if self.process_experiences:
            if lines:
                lines.append("")
            lines.append("相关执行过程经验:")
            for experience in self.process_experiences:
                lines.append(f"- {experience.summary}")
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
        memory_layers: Iterable[str] | None = None,
        avoid_memory_layers: Iterable[str] | None = None,
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
        requested_layers = _normalize_memory_layers(memory_layers)
        avoided_layers = _normalize_memory_layers(avoid_memory_layers)
        for kind in kinds or ("memory", "knowledge", "methodology"):
            try:
                candidates.extend(
                    _filter_by_memory_policy(
                        await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100),
                        requested_layers=requested_layers,
                        avoided_layers=avoided_layers,
                    )
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
        memory_layers: Iterable[str] | None = None,
        avoid_memory_layers: Iterable[str] | None = None,
    ) -> ContextPack:
        query_terms = _task_terms(task_ref)
        if not query_terms:
            return ContextPack()

        candidates: list[LayeredAsset] = []
        requested_layers = _normalize_memory_layers(memory_layers)
        avoided_layers = _normalize_memory_layers(avoid_memory_layers)
        for kind in kinds or ("memory", "knowledge", "methodology", "role_template", "skill"):
            candidates.extend(
                _filter_by_memory_policy(
                    await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100),
                    requested_layers=requested_layers,
                    avoided_layers=avoided_layers,
                )
            )

        query = _task_query(task_ref)
        scored = await self._rank_assets(candidates, query=query)
        selected = scored[:limit]
        await self._touch_selected(selected)
        process_experiences = (
            await self._recall_process_experiences(
                task_ref,
                tenant_id=tenant_id,
            )
            if _allows_process_experiences(
                requested_layers=requested_layers,
                avoided_layers=avoided_layers,
            )
            else []
        )
        return ContextPack(
            items=[_to_packed(score, asset) for asset, score in selected],
            process_experiences=process_experiences,
        )

    def pack_anchor_then_expand(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
        kinds: Iterable[AssetKind] | None = None,
        max_rounds: int = 3,
        use_marginal_stop: bool = True,
        memory_layers: Iterable[str] | None = None,
        avoid_memory_layers: Iterable[str] | None = None,
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
            requested_layers = _normalize_memory_layers(memory_layers)
            avoided_layers = _normalize_memory_layers(avoid_memory_layers)
            for kind in kinds or ("memory", "knowledge", "methodology", "role_template", "skill"):
                candidates.extend(
                    _filter_by_memory_policy(
                        await self._store.list(tenant_id=tenant_id, asset_kind=kind, limit=100),
                        requested_layers=requested_layers,
                        avoided_layers=avoided_layers,
                    )
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

    async def recall_process_experiences(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
    ) -> list[PackedProcessExperience]:
        """Recall process memories for paths that do anchor-expand loading.

        SMART mode gets process memories through `pack()`. MAX / ENSEMBLE use
        anchor-expand for assets, so they need an explicit hook to avoid losing
        the most useful "how did similar tasks actually run" hints.
        """

        return await self._recall_process_experiences(task_ref, tenant_id=tenant_id)

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

        from kun.core.db import session_scope
        from kun.engineering.credit_assignment import (
            get_contribution_tracker,
            load_resource_credit_scores,
            make_resource_key,
        )

        tracker = get_contribution_tracker()
        kind_by_asset_id = {asset.asset_id: asset.asset_kind for asset in candidates}
        resource_keys = [
            make_resource_key(str(kind_by_asset_id.get(asset.asset_id, "memory")), asset.asset_id)
            for asset in candidates
        ]
        durable_scores: dict[str, float] = {}
        try:
            tenant_id = str(candidates[0].tenant_id)
            async with session_scope(tenant_id=tenant_id) as s:
                durable_scores = await load_resource_credit_scores(
                    s,
                    tenant_id=tenant_id,
                    resource_keys=resource_keys,
                )
        except Exception:
            log.debug("context_packer.load_resource_credit_scores_failed", exc_info=True)

        def _contribution_lookup(asset_id: str) -> float:
            kind = str(kind_by_asset_id.get(asset_id, "memory"))
            key = make_resource_key(kind, asset_id)
            return max(
                tracker.contribution_score(asset_id, kind, tenant_id=str(candidates[0].tenant_id)),
                durable_scores.get(key, 0.0),
            )

        scored = await self._scorer.score_with_contribution_boost(
            candidates,
            query=query,
            contribution_lookup=_contribution_lookup,
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

    async def _recall_process_experiences(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
    ) -> list[PackedProcessExperience]:
        try:
            from kun.memory.similar_task_recall import (
                recall_similar_task_experiences,
                summarize_execution_process_experiences,
            )

            experiences = await recall_similar_task_experiences(
                tenant_id=tenant_id,
                task_ref=task_ref,
                store=self._store,
                limit=12,
            )
            process_by_asset_id = {
                experience.asset_id: experience
                for experience in experiences
                if experience.memory_layer == "execution_process"
            }
            ordered_process_ids = [
                experience.asset_id
                for experience in sorted(
                    process_by_asset_id.values(),
                    key=lambda item: (
                        -item.similarity_score,
                        item.step_id if item.step_id is not None else 9999,
                        item.asset_id,
                    ),
                )
            ]
            summaries = summarize_execution_process_experiences(experiences)
            return [
                PackedProcessExperience(
                    asset_id=asset_id,
                    summary=summary,
                    similarity_score=process_by_asset_id[asset_id].similarity_score,
                )
                for asset_id, summary in zip(ordered_process_ids, summaries, strict=False)
            ]
        except Exception:
            log.debug("context_packer.process_experience_recall_failed", exc_info=True)
            return []


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


def _normalize_memory_layers(layers: Iterable[str] | None) -> set[str]:
    return {str(layer).strip() for layer in layers or () if str(layer).strip()}


def _filter_by_memory_policy(
    assets: list[LayeredAsset],
    *,
    requested_layers: set[str],
    avoided_layers: set[str],
) -> list[LayeredAsset]:
    if not requested_layers and not avoided_layers:
        return assets
    out: list[LayeredAsset] = []
    for asset in assets:
        logical_layer = _logical_memory_layer(asset)
        if logical_layer in avoided_layers:
            continue
        if (
            requested_layers
            and _is_memory_like_asset(asset)
            and logical_layer not in requested_layers
        ):
            continue
        out.append(asset)
    return out


def _allows_process_experiences(
    *,
    requested_layers: set[str],
    avoided_layers: set[str],
) -> bool:
    if "execution_process" in avoided_layers:
        return False
    return not requested_layers or "execution_process" in requested_layers


def _is_memory_like_asset(asset: LayeredAsset) -> bool:
    return asset.asset_kind in {"memory", "methodology"}


def _logical_memory_layer(asset: LayeredAsset) -> str:
    raw = asset.l1_metadata.get("memory_layer")
    if isinstance(raw, str) and raw:
        return raw
    if asset.asset_kind == "methodology":
        return "methodology"
    if asset.asset_kind == "memory":
        return "task_result"
    if asset.asset_kind == "skill":
        return "behavior"
    return str(asset.asset_kind)


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


__all__ = [
    "ContextPack",
    "ContextPacker",
    "PackedContextItem",
    "PackedProcessExperience",
]
