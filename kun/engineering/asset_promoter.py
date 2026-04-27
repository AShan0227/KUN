"""LayeredAsset promotion engine (C19).

Promotion turns a narrow task-local asset into a broader reusable asset.
Rules are intentionally conservative:

- L1 -> L2 can be automatic.
- L2 -> L3 needs user confirmation.
- L3 -> L4 needs user confirmation and anonymization.
"""

from __future__ import annotations

from dataclasses import dataclass

from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.storage import AssetStore, get_store

_ORDER = [
    AssetLayer.L1_TASK,
    AssetLayer.L2_PROJECT,
    AssetLayer.L3_USER,
    AssetLayer.L4_GLOBAL,
]


@dataclass(frozen=True)
class PromotionSignals:
    reuse_count: int
    distinct_tasks: int
    distinct_projects: int


class AssetPromoter:
    """Suggest and execute safe asset promotions."""

    def __init__(self, *, tenant_id: str, store: AssetStore | None = None) -> None:
        self._tenant_id = tenant_id
        self._store = store or get_store()

    async def suggest_promote(self, asset_id: str) -> tuple[AssetLayer, float]:
        """Suggest the broadest reasonable next layer plus confidence."""

        asset = await self._must_get(asset_id)
        signals = _signals(asset)
        layer = asset.layer
        if layer == AssetLayer.L4_GLOBAL:
            return (AssetLayer.L4_GLOBAL, 1.0)

        if layer == AssetLayer.L1_TASK:
            if signals.reuse_count >= 2 or signals.distinct_tasks >= 2:
                return (AssetLayer.L2_PROJECT, _confidence(0.55, signals))
            return (AssetLayer.L1_TASK, 0.35)

        if layer == AssetLayer.L2_PROJECT:
            if signals.distinct_projects >= 2 or signals.distinct_tasks >= 5:
                return (AssetLayer.L3_USER, _confidence(0.5, signals))
            return (AssetLayer.L2_PROJECT, 0.45)

        if signals.distinct_projects >= 3 and signals.reuse_count >= 10:
            return (AssetLayer.L4_GLOBAL, _confidence(0.45, signals))
        return (AssetLayer.L3_USER, 0.5)

    async def execute_promote(
        self,
        asset_id: str,
        target_layer: AssetLayer,
        *,
        user_confirmed: bool = False,
    ) -> LayeredAsset:
        """Promote an asset with the right confirmation and anonymization gates."""

        asset = await self._must_get(asset_id)
        _validate_forward_step(asset.layer, target_layer)

        if target_layer == asset.layer:
            return asset
        if target_layer in {AssetLayer.L3_USER, AssetLayer.L4_GLOBAL} and not user_confirmed:
            raise PermissionError(f"{target_layer.value} promotion requires user confirmation")

        if target_layer == AssetLayer.L4_GLOBAL:
            promoted = asset.anonymized_for_global()
            await self._store.put(promoted)
            return promoted

        promoted = asset.clone_for_layer(target_layer)
        promoted.l1_metadata = {
            **promoted.l1_metadata,
            "promoted_from": asset.layer.value,
            "promotion_confirmed": user_confirmed,
        }
        await self._store.put(promoted)
        return promoted

    async def _must_get(self, asset_id: str) -> LayeredAsset:
        asset = await self._store.get(asset_id, tenant_id=self._tenant_id)
        if asset is None:
            raise KeyError(f"unknown asset: {asset_id}")
        return asset


def _validate_forward_step(current: AssetLayer, target: AssetLayer) -> None:
    current_idx = _ORDER.index(current)
    target_idx = _ORDER.index(target)
    if target_idx < current_idx:
        raise ValueError(f"cannot demote asset from {current.value} to {target.value}")
    if target_idx - current_idx > 1:
        raise ValueError(f"cannot skip promotion layers: {current.value} -> {target.value}")


def _signals(asset: LayeredAsset) -> PromotionSignals:
    metadata = asset.l1_metadata
    task_ids = _as_string_set(metadata.get("used_by_task_ids"))
    project_ids = _as_string_set(metadata.get("used_by_project_ids"))
    reuse_count = int(metadata.get("reuse_count") or asset.access_count or len(task_ids))
    return PromotionSignals(
        reuse_count=max(reuse_count, len(task_ids)),
        distinct_tasks=len(task_ids),
        distinct_projects=len(project_ids),
    )


def _as_string_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if str(item)}
    return set()


def _confidence(base: float, signals: PromotionSignals) -> float:
    raw = (
        base
        + min(signals.reuse_count, 10) * 0.035
        + min(signals.distinct_tasks, 8) * 0.025
        + min(signals.distinct_projects, 4) * 0.04
    )
    return round(min(raw, 0.95), 3)


__all__ = ["AssetPromoter", "PromotionSignals"]
