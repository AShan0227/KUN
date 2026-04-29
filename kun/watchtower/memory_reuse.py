"""Memory reuse advisor for V3 sparse-MoE strategy routing.

This is the first honest closed loop between memory writeback and routing:
meta-decision / result memories written by previous tasks are read before a new
task starts, then converted into concrete strategy hints consumed by
WatchtowerDecisionPlane and ContextPacker.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.datamodel.task import TaskRef

log = logging.getLogger(__name__)


class StrategyReuseHint(BaseModel):
    """Reusable path evidence mined from prior memories."""

    recommended_strategy_pack_id: str | None = None
    skill_hints: list[str] = Field(default_factory=list)
    reuse_asset_ids: list[str] = Field(default_factory=list)
    matched_task_types: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


@dataclass
class MemoryReuseAdvisor:
    """Find prior task memories that should influence the next route."""

    store: AssetStore | None = None
    min_score: float = 0.45

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = get_store()

    async def suggest(self, task_ref: TaskRef, *, tenant_id: str) -> StrategyReuseHint:
        assert self.store is not None
        candidates: list[LayeredAsset] = []
        for kind in ("methodology", "memory"):
            try:
                candidates.extend(
                    await self.store.list(tenant_id=tenant_id, asset_kind=kind, limit=200)
                )
            except Exception as exc:
                log.debug("memory_reuse.store_list_failed kind=%s error=%s", kind, exc)
                continue

        scored = [
            (score, asset)
            for asset in candidates
            if (score := _score_memory(asset, task_ref)) >= self.min_score
        ]
        scored.sort(key=lambda item: (-item[0], item[1].asset_id))
        if not scored:
            return StrategyReuseHint(reason="没有找到足够相似的历史策略记忆")

        top = scored[:6]
        pack_scores: dict[str, float] = {}
        skill_scores: dict[str, float] = {}
        task_types: list[str] = []
        asset_ids: list[str] = []
        for score, asset in top:
            asset_ids.append(asset.asset_id)
            metadata = asset.l1_metadata
            task_type = _str(metadata.get("task_type"))
            if task_type and task_type not in task_types:
                task_types.append(task_type)
            strategy_pack_id = _str(metadata.get("strategy_pack_id"))
            if strategy_pack_id:
                pack_scores[strategy_pack_id] = pack_scores.get(strategy_pack_id, 0.0) + score
            for skill in _string_list(metadata.get("skill_hints")):
                skill_scores[skill] = skill_scores.get(skill, 0.0) + score

        recommended = None
        if pack_scores:
            recommended = sorted(pack_scores.items(), key=lambda item: (-item[1], item[0]))[0][0]
        skills = [
            skill
            for skill, _score in sorted(skill_scores.items(), key=lambda item: (-item[1], item[0]))
        ][:8]
        confidence = min(0.95, 0.35 + min(sum(score for score, _ in top), 2.4) * 0.20)
        reason = (
            f"命中 {len(top)} 条历史经验; "
            f"recommended_strategy={recommended or 'none'}; confidence={confidence:.2f}"
        )
        return StrategyReuseHint(
            recommended_strategy_pack_id=recommended,
            skill_hints=skills,
            reuse_asset_ids=asset_ids,
            matched_task_types=task_types,
            confidence=confidence,
            reason=reason,
        )


def _score_memory(asset: LayeredAsset, task_ref: TaskRef) -> float:
    metadata = asset.l1_metadata
    memory_layer = _str(metadata.get("memory_layer"))
    if memory_layer not in {"meta_decision", "task_result", "execution_process"}:
        return 0.0

    score = 0.0
    prior_task_type = _str(metadata.get("task_type"))
    current_task_type = task_ref.meta.task_type
    if prior_task_type == current_task_type:
        score += 1.15
    elif prior_task_type and (
        prior_task_type.startswith(current_task_type)
        or current_task_type.startswith(prior_task_type)
    ):
        score += 0.65

    query_terms = _task_terms(task_ref)
    asset_text = " ".join(
        [
            asset.l2_summary or "",
            " ".join(asset.tags),
            " ".join(_str(value) for value in metadata.values()),
        ]
    ).lower()
    if query_terms:
        overlap = sum(1 for term in query_terms if term in asset_text)
        score += min(0.75, overlap * 0.12)

    if memory_layer == "meta_decision":
        score += 0.35
    elif memory_layer == "task_result":
        if _str(metadata.get("status")) == "done":
            score += 0.20
        outcome = _str(metadata.get("validation_outcome"))
        if outcome == "pass":
            score += 0.25
        elif outcome == "partial":
            score += 0.10
        score_overall = _float_or_none(metadata.get("score_overall"))
        if score_overall is not None:
            score += min(0.30, max(0.0, score_overall) * 0.30)
    return score


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
    return {
        term
        for term in re.split(r"[\s,.;:!?，。；：！？/\\|()\[\]{}<>\"']+", " ".join(parts).lower())
        if len(term) >= 2
    }


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["MemoryReuseAdvisor", "StrategyReuseHint"]
