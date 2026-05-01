"""Conservative duplicate asset merge executor for NUO context maintenance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store

DuplicateMergeStatus = Literal["planned", "merged", "skipped", "error"]


class DuplicateMergeResult(BaseModel):
    """Result for one duplicate asset candidate."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    status: DuplicateMergeStatus
    reason: str
    dry_run: bool
    canonical_asset_id: str | None = None
    soft_forgotten: bool = False


class DuplicateMergeReport(BaseModel):
    """Aggregate result for one duplicate merge pass."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    dry_run: bool
    scanned: int = 0
    candidates: int = 0
    planned: int = 0
    merged: int = 0
    skipped: int = 0
    errors: int = 0
    results: list[DuplicateMergeResult] = Field(default_factory=list)


class DuplicateAssetMerger:
    """Merge duplicate findings without deleting original audit history.

    Context maintenance marks duplicate candidates.  This executor turns that
    diagnosis into a reversible action: the duplicate is soft-forgotten and the
    canonical asset records which duplicates were merged into it.
    """

    def __init__(self, *, store: AssetStore | None = None) -> None:
        self.store = store or get_store()

    async def merge_duplicates(
        self,
        *,
        tenant_id: str,
        dry_run: bool = True,
        max_assets: int = 500,
        mark_duplicate_soft_forgotten: bool = True,
    ) -> DuplicateMergeReport:
        report = DuplicateMergeReport(tenant_id=tenant_id, dry_run=dry_run)
        assets = await self.store.list(tenant_id=tenant_id, limit=max_assets)
        for asset in assets:
            report.scanned += 1
            canonical_id = _candidate_canonical_id(asset)
            if canonical_id is None:
                continue
            report.candidates += 1
            try:
                result = await self._merge_one(
                    asset,
                    tenant_id=tenant_id,
                    canonical_id=canonical_id,
                    dry_run=dry_run,
                    mark_duplicate_soft_forgotten=mark_duplicate_soft_forgotten,
                )
            except Exception as exc:  # pragma: no cover - defensive ops guard
                result = DuplicateMergeResult(
                    asset_id=asset.asset_id,
                    status="error",
                    reason=f"{type(exc).__name__}: {exc}",
                    dry_run=dry_run,
                    canonical_asset_id=canonical_id,
                )
            report.results.append(result)
            if result.status == "planned":
                report.planned += 1
            elif result.status == "merged":
                report.merged += 1
            elif result.status == "skipped":
                report.skipped += 1
            else:
                report.errors += 1
        return report

    async def _merge_one(
        self,
        duplicate: LayeredAsset,
        *,
        tenant_id: str,
        canonical_id: str,
        dry_run: bool,
        mark_duplicate_soft_forgotten: bool,
    ) -> DuplicateMergeResult:
        if duplicate.asset_id == canonical_id:
            return _skipped(duplicate, "duplicate_points_to_itself", dry_run, canonical_id)
        canonical = await self.store.get(canonical_id, tenant_id=tenant_id)
        if canonical is None:
            return _skipped(duplicate, "canonical_asset_missing", dry_run, canonical_id)
        if duplicate.l1_metadata.get("duplicate_merge_applied") is True:
            return _skipped(duplicate, "duplicate_already_merged", dry_run, canonical_id)
        if dry_run:
            return DuplicateMergeResult(
                asset_id=duplicate.asset_id,
                status="planned",
                reason="would_soft_forget_duplicate_and_record_on_canonical",
                dry_run=True,
                canonical_asset_id=canonical_id,
                soft_forgotten=mark_duplicate_soft_forgotten,
            )

        _mark_duplicate_merged(
            duplicate,
            canonical_id=canonical_id,
            mark_soft_forgotten=mark_duplicate_soft_forgotten,
        )
        _mark_canonical_with_duplicate(canonical, duplicate.asset_id)
        await self.store.put(canonical)
        await self.store.put(duplicate)
        return DuplicateMergeResult(
            asset_id=duplicate.asset_id,
            status="merged",
            reason="duplicate_soft_forgotten_and_recorded_on_canonical",
            dry_run=False,
            canonical_asset_id=canonical_id,
            soft_forgotten=mark_duplicate_soft_forgotten,
        )


def _candidate_canonical_id(asset: LayeredAsset) -> str | None:
    meta = asset.l1_metadata or {}
    if meta.get("duplicate_merge_applied") is True:
        return None
    duplicate_flag = meta.get("duplicate_candidate") is True or "duplicate_candidate" in set(
        asset.tags
    )
    if not duplicate_flag:
        return None
    duplicate_of = meta.get("duplicate_of")
    if not isinstance(duplicate_of, str) or not duplicate_of.strip():
        return None
    return duplicate_of.strip()


def _mark_duplicate_merged(
    asset: LayeredAsset,
    *,
    canonical_id: str,
    mark_soft_forgotten: bool,
) -> None:
    asset.version += 1
    asset.l1_metadata["duplicate_merge_applied"] = True
    asset.l1_metadata["duplicate_merged_into_asset_id"] = canonical_id
    asset.l1_metadata["duplicate_merged_at"] = datetime.now(UTC).isoformat()
    asset.l1_metadata["duplicate_candidate"] = False
    asset.tags = sorted(tag for tag in set(asset.tags) if tag != "duplicate_candidate")
    asset.tags = sorted({*asset.tags, "duplicate_merged"})
    if mark_soft_forgotten:
        asset.l1_metadata["soft_forgotten"] = True
        asset.tags = sorted({*asset.tags, "soft_forgotten"})


def _mark_canonical_with_duplicate(canonical: LayeredAsset, duplicate_id: str) -> None:
    canonical.version += 1
    existing = canonical.l1_metadata.get("merged_duplicate_asset_ids")
    merged_ids = (
        [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    )
    if duplicate_id not in merged_ids:
        merged_ids.append(duplicate_id)
    canonical.l1_metadata["merged_duplicate_asset_ids"] = merged_ids
    canonical.l1_metadata["merged_duplicate_count"] = len(merged_ids)
    canonical.l1_metadata["last_duplicate_merged_at"] = datetime.now(UTC).isoformat()
    canonical.tags = sorted({*canonical.tags, "duplicate_canonical"})


def _skipped(
    asset: LayeredAsset,
    reason: str,
    dry_run: bool,
    canonical_id: str | None,
) -> DuplicateMergeResult:
    return DuplicateMergeResult(
        asset_id=asset.asset_id,
        status="skipped",
        reason=reason,
        dry_run=dry_run,
        canonical_asset_id=canonical_id,
    )


__all__ = [
    "DuplicateAssetMerger",
    "DuplicateMergeReport",
    "DuplicateMergeResult",
    "DuplicateMergeStatus",
]
