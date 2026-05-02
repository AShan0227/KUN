from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.governance_audit import run_context_governance_audit
from kun.context.storage import InMemoryAssetStore


def _asset(
    *,
    tenant_id: str = "tenant-a",
    kind: str = "memory",
    summary: str = "Useful enough summary text for duplicate detection.",
    metadata: dict | None = None,
    tags: list[str] | None = None,
    layer: AssetLayer = AssetLayer.L1_TASK,
    access_count: int = 0,
    last_accessed_days_ago: int = 0,
) -> LayeredAsset:
    asset = LayeredAsset.build(
        kind,  # type: ignore[arg-type]
        tenant_id,
        metadata=metadata or {},
        summary=summary,
        tags=tags or [],
        layer=layer,
    )
    asset.access_count = access_count
    asset.last_accessed = datetime.now(UTC) - timedelta(days=last_accessed_days_ago)
    return asset


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_governance_audit_reports_review_only_candidates_without_mutating() -> None:
    store = InMemoryAssetStore()
    duplicate_a = _asset(summary="Same reusable lesson that should appear only once.")
    duplicate_b = _asset(summary="Same reusable lesson that should appear only once.")
    stale = _asset(summary="Old note", last_accessed_days_ago=75)
    reusable_without_credit = _asset(
        summary="Reusable result memory without durable credit.",
        metadata={"memory_layer": "task_result", "task_type": "coding.refactor"},
        layer=AssetLayer.L2_PROJECT,
    )
    frequent_a = _asset(
        summary="Successful refactor pattern A.",
        metadata={
            "memory_layer": "task_result",
            "task_type": "coding.refactor",
            "strategy_pack_id": "sp-fast",
            "status": "done",
            "decision_tickets": [{"decision_point": "context_selected"}],
        },
        layer=AssetLayer.L2_PROJECT,
        access_count=3,
    )
    frequent_b = _asset(
        summary="Successful refactor pattern B.",
        metadata={
            "memory_layer": "task_result",
            "task_type": "coding.refactor",
            "strategy_pack_id": "sp-fast",
            "status": "done",
            "decision_tickets": [{"decision_point": "context_selected"}],
        },
        layer=AssetLayer.L2_PROJECT,
        access_count=3,
    )
    for asset in [
        duplicate_a,
        duplicate_b,
        stale,
        reusable_without_credit,
        frequent_a,
        frequent_b,
    ]:
        await store.put(asset)
    before = {
        asset.asset_id: asset.model_dump(mode="json")
        for asset in await store.list(tenant_id="tenant-a", limit=20)
    }

    report = await run_context_governance_audit(tenant_id="tenant-a", store=store)

    after = {
        asset.asset_id: asset.model_dump(mode="json")
        for asset in await store.list(tenant_id="tenant-a", limit=20)
    }
    assert before == after
    assert report.review_only is True
    assert report.production_action is False
    assert report.category_counts["duplicate"] == 1
    assert report.category_counts["low_value"] >= 1
    assert report.category_counts["stale_long_tail"] == 1
    assert report.category_counts["high_frequency_abstractable"] == 1
    assert report.category_counts["missing_credit_attribution"] >= 1
    assert all(finding.review_only is True for finding in report.findings)
    assert all(finding.production_action is False for finding in report.findings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_governance_audit_respects_credit_and_permanent_tier() -> None:
    store = InMemoryAssetStore()
    credited = _asset(
        summary="Promoted memory with durable resource credit.",
        metadata={"memory_layer": "task_result", "task_type": "analysis"},
        layer=AssetLayer.L2_PROJECT,
    )
    permanent_old = _asset(
        summary="Permanent preference memory.",
        metadata={"tier": "permanent", "credit_source": "user_pin"},
        layer=AssetLayer.L2_PROJECT,
        last_accessed_days_ago=120,
    )
    await store.put(credited)
    await store.put(permanent_old)

    report = await run_context_governance_audit(
        tenant_id="tenant-a",
        store=store,
        credited_resource_keys={f"memory:{credited.asset_id}"},
    )

    assert "missing_credit_attribution" not in report.category_counts
    assert not any(permanent_old.asset_id in finding.asset_ids for finding in report.findings)
