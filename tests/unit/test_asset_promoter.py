"""C19 LayeredAsset promotion tests."""

import pytest
from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.task import Owner, TaskLayer3Context, TaskMeta, TaskRef, TaskSpec
from kun.engineering.asset_promoter import AssetPromoter


def _asset(
    *,
    layer: AssetLayer = AssetLayer.L1_TASK,
    metadata: dict[str, object] | None = None,
    summary: str = "reuse postgres migration pattern",
) -> LayeredAsset:
    return LayeredAsset.build(
        "memory",
        "tenant-1",
        metadata=metadata or {},
        summary=summary,
        full_ref="memory://full",
        layer=layer,
        tags=["postgres", "migration"],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suggest_stays_l1_for_single_use_asset() -> None:
    store = InMemoryAssetStore()
    asset = _asset()
    await store.put(asset)

    layer, confidence = await AssetPromoter(tenant_id="tenant-1", store=store).suggest_promote(
        asset.asset_id
    )

    assert layer == AssetLayer.L1_TASK
    assert confidence == 0.35


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suggest_l1_to_l2_after_reuse() -> None:
    store = InMemoryAssetStore()
    asset = _asset(metadata={"reuse_count": 3, "used_by_task_ids": ["a", "b", "c"]})
    await store.put(asset)

    layer, confidence = await AssetPromoter(tenant_id="tenant-1", store=store).suggest_promote(
        asset.asset_id
    )

    assert layer == AssetLayer.L2_PROJECT
    assert confidence > 0.6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_l1_to_l2_without_confirmation() -> None:
    store = InMemoryAssetStore()
    asset = _asset()
    await store.put(asset)

    promoted = await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
        asset.asset_id, AssetLayer.L2_PROJECT
    )

    assert promoted.layer == AssetLayer.L2_PROJECT
    assert promoted.version == asset.version + 1
    assert promoted.l1_metadata["promoted_from"] == AssetLayer.L1_TASK.value


@pytest.mark.unit
@pytest.mark.asyncio
async def test_l2_to_l3_requires_confirmation() -> None:
    store = InMemoryAssetStore()
    asset = _asset(layer=AssetLayer.L2_PROJECT)
    await store.put(asset)

    with pytest.raises(PermissionError):
        await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
            asset.asset_id, AssetLayer.L3_USER
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_l2_to_l3_with_confirmation() -> None:
    store = InMemoryAssetStore()
    asset = _asset(layer=AssetLayer.L2_PROJECT)
    await store.put(asset)

    promoted = await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
        asset.asset_id, AssetLayer.L3_USER, user_confirmed=True
    )

    assert promoted.layer == AssetLayer.L3_USER
    assert promoted.l1_metadata["promotion_confirmed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_l3_to_l4_requires_confirmation() -> None:
    store = InMemoryAssetStore()
    asset = _asset(layer=AssetLayer.L3_USER)
    await store.put(asset)

    with pytest.raises(PermissionError):
        await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
            asset.asset_id, AssetLayer.L4_GLOBAL
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_l3_to_l4_anonymizes_global_asset() -> None:
    store = InMemoryAssetStore()
    asset = _asset(
        layer=AssetLayer.L3_USER,
        metadata={
            "title": "sales playbook",
            "user_id": "u-private",
            "customer_email": "alice@example.com",
            "used_by_project_ids": ["p1", "p2", "p3"],
        },
        summary="Customer alice@example.com called +1 415 555 0101 about pricing.",
    )
    await store.put(asset)

    promoted = await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
        asset.asset_id, AssetLayer.L4_GLOBAL, user_confirmed=True
    )

    assert promoted.layer == AssetLayer.L4_GLOBAL
    assert promoted.tenant_id == "global"
    assert promoted.l1_metadata["anonymized"] is True
    assert "user_id" not in promoted.l1_metadata
    assert "customer_email" not in promoted.l1_metadata
    assert "alice@example.com" not in (promoted.l2_summary or "")
    assert "+1 415 555 0101" not in (promoted.l2_summary or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejects_skipping_layers() -> None:
    store = InMemoryAssetStore()
    asset = _asset(layer=AssetLayer.L1_TASK)
    await store.put(asset)

    with pytest.raises(ValueError, match="cannot skip"):
        await AssetPromoter(tenant_id="tenant-1", store=store).execute_promote(
            asset.asset_id, AssetLayer.L3_USER, user_confirmed=True
        )


@pytest.mark.unit
def test_task_ref_exports_l3_as_layered_asset() -> None:
    owner = Owner(tenant_id="tenant-1", user_id="user-1", project_id="project-1")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("ship report", owner),
        task_type="ops.report",
        owner=owner,
        success_criteria_short="ship weekly report",
    )
    task_ref = TaskRef(
        meta=meta,
        spec=TaskSpec(
            goal_detail="Create a weekly report",
            success_metrics=["sent to stakeholder"],
            required_skills=["markdown_to_pdf"],
        ),
        layer3_context=TaskLayer3Context(
            project_context="This project prefers concise weekly reports.",
            historical_notes=["Last report used the executive template."],
            asset_refs=["mm-history"],
        ),
    )

    asset = task_ref.to_layered_asset(layer=AssetLayer.L2_PROJECT)

    assert asset.asset_kind == "task"
    assert asset.layer == AssetLayer.L2_PROJECT
    assert asset.l3_ref == f"task_l3://{task_ref.meta.task_id}"
    assert "executive template" in (asset.l2_summary or "")
