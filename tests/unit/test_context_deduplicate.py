from __future__ import annotations

import pytest
from kun.context.assets import LayeredAsset
from kun.context.deduplicate import DuplicateAssetMerger
from kun.context.storage import InMemoryAssetStore


def _asset(summary: str, *, tenant_id: str = "tenant-dedupe") -> LayeredAsset:
    return LayeredAsset.build("memory", tenant_id, summary=summary, tags=["pytest"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_merger_dry_run_plans_without_mutation() -> None:
    store = InMemoryAssetStore()
    canonical = _asset("same")
    duplicate = _asset("same")
    duplicate.l1_metadata["duplicate_candidate"] = True
    duplicate.l1_metadata["duplicate_of"] = canonical.asset_id
    duplicate.tags = ["duplicate_candidate"]
    await store.put(canonical)
    await store.put(duplicate)

    report = await DuplicateAssetMerger(store=store).merge_duplicates(
        tenant_id="tenant-dedupe",
        dry_run=True,
    )

    assert report.candidates == 1
    assert report.planned == 1
    assert report.results[0].canonical_asset_id == canonical.asset_id
    after_duplicate = await store.get(duplicate.asset_id, tenant_id="tenant-dedupe")
    assert after_duplicate is not None
    assert after_duplicate.l1_metadata.get("duplicate_merge_applied") is not True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_merger_apply_soft_forgets_duplicate_and_records_canonical() -> None:
    store = InMemoryAssetStore()
    canonical = _asset("same")
    duplicate = _asset("same")
    duplicate.l1_metadata["duplicate_candidate"] = True
    duplicate.l1_metadata["duplicate_of"] = canonical.asset_id
    duplicate.tags = ["duplicate_candidate"]
    await store.put(canonical)
    await store.put(duplicate)

    report = await DuplicateAssetMerger(store=store).merge_duplicates(
        tenant_id="tenant-dedupe",
        dry_run=False,
    )

    assert report.merged == 1
    result = report.results[0]
    assert result.status == "merged"
    after_duplicate = await store.get(duplicate.asset_id, tenant_id="tenant-dedupe")
    after_canonical = await store.get(canonical.asset_id, tenant_id="tenant-dedupe")
    assert after_duplicate is not None
    assert after_duplicate.l1_metadata["duplicate_merge_applied"] is True
    assert after_duplicate.l1_metadata["duplicate_candidate"] is False
    assert after_duplicate.l1_metadata["duplicate_merged_into_asset_id"] == canonical.asset_id
    assert after_duplicate.l1_metadata["soft_forgotten"] is True
    assert "duplicate_candidate" not in after_duplicate.tags
    assert "duplicate_merged" in after_duplicate.tags
    assert after_canonical is not None
    assert after_canonical.l1_metadata["merged_duplicate_asset_ids"] == [duplicate.asset_id]
    assert after_canonical.l1_metadata["merged_duplicate_count"] == 1
    assert "duplicate_canonical" in after_canonical.tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_merger_skips_missing_canonical() -> None:
    store = InMemoryAssetStore()
    duplicate = _asset("same")
    duplicate.l1_metadata["duplicate_candidate"] = True
    duplicate.l1_metadata["duplicate_of"] = "missing"
    duplicate.tags = ["duplicate_candidate"]
    await store.put(duplicate)

    report = await DuplicateAssetMerger(store=store).merge_duplicates(
        tenant_id="tenant-dedupe",
        dry_run=False,
    )

    assert report.skipped == 1
    assert report.results[0].reason == "canonical_asset_missing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_merger_is_idempotent_after_apply() -> None:
    store = InMemoryAssetStore()
    canonical = _asset("same")
    duplicate = _asset("same")
    duplicate.l1_metadata["duplicate_candidate"] = True
    duplicate.l1_metadata["duplicate_of"] = canonical.asset_id
    duplicate.tags = ["duplicate_candidate"]
    await store.put(canonical)
    await store.put(duplicate)

    first = await DuplicateAssetMerger(store=store).merge_duplicates(
        tenant_id="tenant-dedupe",
        dry_run=False,
    )
    second = await DuplicateAssetMerger(store=store).merge_duplicates(
        tenant_id="tenant-dedupe",
        dry_run=False,
    )

    assert first.merged == 1
    assert second.candidates == 0
