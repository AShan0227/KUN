"""Context packer — preheat relevant L1/L2 assets for a task."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from kun.context.assets import AssetKind, LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.datamodel.task import TaskRef


class PackedContextItem(BaseModel):
    """One asset selected for the execution context."""

    asset_id: str
    asset_kind: AssetKind
    relevance_score: float = Field(ge=0.0)
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    summary: str = ""


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

        scored: list[tuple[float, LayeredAsset]] = []
        for asset in candidates:
            score = _score_asset(asset, query_terms)
            if score > 0:
                scored.append((score, asset))

        scored.sort(key=lambda item: (-item[0], item[1].asset_id))
        return ContextPack(
            items=[
                PackedContextItem(
                    asset_id=asset.asset_id,
                    asset_kind=asset.asset_kind,
                    relevance_score=score,
                    title=str(
                        asset.l1_metadata.get("title") or asset.l1_metadata.get("name") or ""
                    ),
                    tags=asset.tags,
                    summary=asset.l2_summary or _metadata_summary(asset),
                )
                for score, asset in scored[:limit]
            ]
        )


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
