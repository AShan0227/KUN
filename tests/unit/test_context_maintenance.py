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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_flags_risky_compiler_assets() -> None:
    store = InMemoryAssetStore()
    asset = LayeredAsset.build(
        "knowledge",
        "tenant-a",
        metadata={
            "compiler_profile": {"name": "kun-v5-lightweight", "limitations": []},
            "risk": {"level": "medium", "flags": ["invalid_json"]},
            "provenance": {"input_sha256": "abc"},
        },
        summary="invalid json source",
    )
    await store.put(asset)

    report = await run_context_maintenance(tenant_id="tenant-a", dry_run=True, store=store)

    assert report.compiler_review == 1
    assert any(item.action == "compiler_review" for item in report.findings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_marks_duplicates_without_deleting() -> None:
    store = InMemoryAssetStore()
    first = LayeredAsset.build("knowledge", "tenant-a", metadata={"title": "A"}, summary="same")
    duplicate = LayeredAsset.build(
        "knowledge",
        "tenant-a",
        metadata={"title": "B"},
        summary="same",
    )
    await store.put(first)
    await store.put(duplicate)

    report = await run_context_maintenance(tenant_id="tenant-a", dry_run=False, store=store)
    after = await store.get(duplicate.asset_id, tenant_id="tenant-a")

    assert report.duplicate_candidates == 1
    assert after is not None
    assert after.l1_metadata["duplicate_candidate"] is True
    assert after.l1_metadata["duplicate_of"] == first.asset_id
    assert "duplicate_candidate" in after.tags
    assert await store.get(first.asset_id, tenant_id="tenant-a") is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_maintenance_marks_compiler_review_assets_when_not_dry_run() -> None:
    store = InMemoryAssetStore()
    asset = LayeredAsset.build(
        "knowledge",
        "tenant-a",
        metadata={
            "compiler_profile": {"name": "kun-v5-lightweight", "limitations": []},
            "risk": {"level": "medium", "flags": ["pdf_text_unavailable"]},
            "provenance": {"input_sha256": "abc"},
        },
        summary="PDF document; text extraction unavailable",
    )
    await store.put(asset)

    report = await run_context_maintenance(tenant_id="tenant-a", dry_run=False, store=store)
    after = await store.get(asset.asset_id, tenant_id="tenant-a")

    assert report.compiler_review == 1
    assert after is not None
    assert after.l1_metadata["compiler_review_required"] is True
    assert "pdf_text_unavailable" in after.l1_metadata["compiler_review_reason"]
    assert "compiler_review_required" in after.tags
