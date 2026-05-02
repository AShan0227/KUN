from __future__ import annotations

import pytest
from kun.context.assets import LayeredAsset
from kun.context.governance_distill import distill_context_governance_rules
from kun.context.storage import InMemoryAssetStore


def _asset(
    *,
    tenant_id: str = "t-1",
    kind: str = "memory",
    summary: str = "same",
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> LayeredAsset:
    return LayeredAsset.build(
        kind,  # type: ignore[arg-type]
        tenant_id,
        metadata=metadata or {},
        summary=summary,
        tags=tags or [],
    )


@pytest.mark.asyncio
async def test_context_governance_distill_creates_review_only_rule_draft() -> None:
    store = InMemoryAssetStore()
    await store.put(
        _asset(
            metadata={"low_value": True, "source": "task.result"},
            tags=["low_value"],
        )
    )
    await store.put(
        _asset(
            metadata={"low_value": True, "source": "task.result"},
            tags=["low_value"],
        )
    )

    report = await distill_context_governance_rules(
        tenant_id="t-1",
        store=store,
        dry_run=False,
        min_evidence=2,
    )

    assert report.scanned == 2
    assert report.candidates == 1
    assert report.created == 1
    assert report.production_action is False
    assets = await store.list(tenant_id="t-1", asset_kind="methodology")
    assert len(assets) == 1
    rule = assets[0]
    assert rule.l1_metadata["source"] == "context.governance_rule_distill"
    assert rule.l1_metadata["category"] == "low_value_pattern"
    assert rule.l1_metadata["requires_human_review"] is True
    assert rule.l1_metadata["production_action"] is False
    assert "governance_rule_draft" in rule.tags


@pytest.mark.asyncio
async def test_context_governance_distill_dry_run_does_not_write() -> None:
    store = InMemoryAssetStore()
    await store.put(
        _asset(
            metadata={"compiler_recompile_recommended": True, "compiler": "kun.compiler"},
            tags=["compiler_recompile_recommended"],
        )
    )
    await store.put(
        _asset(
            metadata={"compiler_review_required": True, "compiler": "kun.compiler"},
            tags=["compiler_review_required"],
        )
    )

    report = await distill_context_governance_rules(
        tenant_id="t-1",
        store=store,
        dry_run=True,
        min_evidence=2,
    )

    assert report.candidates == 1
    assert report.created == 0
    assert report.drafts[0].category == "compiler_quality_pattern"
    assert await store.list(tenant_id="t-1", asset_kind="methodology") == []


@pytest.mark.asyncio
async def test_context_governance_distill_updates_existing_rule_evidence() -> None:
    store = InMemoryAssetStore()
    for _ in range(2):
        await store.put(
            _asset(
                metadata={"duplicate_candidate": True, "source": "compiler"},
                tags=["duplicate_candidate"],
            )
        )
    first = await distill_context_governance_rules(
        tenant_id="t-1",
        store=store,
        dry_run=False,
        min_evidence=2,
    )
    assert first.created == 1
    await store.put(
        _asset(
            metadata={"duplicate_merge_applied": True, "source": "compiler"},
            tags=["duplicate_merged"],
        )
    )

    second = await distill_context_governance_rules(
        tenant_id="t-1",
        store=store,
        dry_run=False,
        min_evidence=2,
    )

    assert second.created == 0
    assert second.updated == 1
    rules = await store.list(tenant_id="t-1", asset_kind="methodology")
    assert rules[0].l1_metadata["evidence_count"] == 3
