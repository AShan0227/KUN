from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.context.assets import LayeredAsset
from kun.context.maintenance import run_context_maintenance
from kun.context.storage import InMemoryAssetStore


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_dry_run_reports_without_mutating() -> None:
    store = InMemoryAssetStore()
    old = LayeredAsset.build(
        "memory",
        "tenant-a",
        metadata={"title": "old note"},
        summary="old",
    )
    old.last_accessed = datetime.now(UTC) - timedelta(days=45)
    await store.put(old)

    report = await run_context_maintenance(tenant_id="tenant-a", dry_run=True, store=store)
    after = await store.get(old.asset_id, tenant_id="tenant-a")

    assert report.soft_forgotten == 1
    assert after is not None
    assert after.l1_metadata.get("soft_forgotten") is not True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_can_compress_summary() -> None:
    store = InMemoryAssetStore()
    asset = LayeredAsset.build(
        "memory",
        "tenant-a",
        metadata={"title": "long note"},
        summary="x " * 1000,
    )
    await store.put(asset)

    report = await run_context_maintenance(
        tenant_id="tenant-a",
        dry_run=False,
        compress_summary_over_chars=100,
        store=store,
    )
    after = await store.get(asset.asset_id, tenant_id="tenant-a")

    assert report.compressed == 1
    assert after is not None
    assert after.l1_metadata["compressed_from_chars"] > 100
    assert "[compressed]" in (after.l2_summary or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_hard_deletes_old_unused_asset() -> None:
    store = InMemoryAssetStore()
    old = LayeredAsset.build("memory", "tenant-a", metadata={"title": "stale"}, summary="stale")
    old.last_accessed = datetime.now(UTC) - timedelta(days=100)
    await store.put(old)

    report = await run_context_maintenance(tenant_id="tenant-a", dry_run=False, store=store)

    assert report.hard_deleted == 1
    assert await store.get(old.asset_id, tenant_id="tenant-a") is None
