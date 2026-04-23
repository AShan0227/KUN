"""AssetStore in-memory tests."""

import pytest
from kun.context.assets import LayeredAsset
from kun.context.storage import InMemoryAssetStore


def _mk(kind: str = "skill", tenant_id: str = "u-sylvan") -> LayeredAsset:
    return LayeredAsset.build(
        asset_kind=kind,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        metadata={"note": "test"},
        summary="short summary",
        full_ref=None,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_get_list_delete_cycle():
    store = InMemoryAssetStore()
    a = _mk("skill")
    await store.put(a)

    got = await store.get(a.asset_id, tenant_id=a.tenant_id)
    assert got is not None
    assert got.asset_id == a.asset_id
    assert got.access_count == 1  # touch incremented

    listed = await store.list(tenant_id=a.tenant_id, asset_kind="skill")
    assert any(x.asset_id == a.asset_id for x in listed)

    assert await store.delete(a.asset_id, tenant_id=a.tenant_id) is True
    assert await store.get(a.asset_id, tenant_id=a.tenant_id) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_isolation():
    store = InMemoryAssetStore()
    a = _mk("skill", tenant_id="u-1")
    b = _mk("skill", tenant_id="u-2")
    await store.put(a)
    await store.put(b)
    assert await store.get(a.asset_id, tenant_id="u-2") is None  # no cross-tenant access


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_filter_by_kind():
    store = InMemoryAssetStore()
    await store.put(_mk("skill"))
    await store.put(_mk("memory"))
    await store.put(_mk("memory"))
    all_memories = await store.list(tenant_id="u-sylvan", asset_kind="memory")
    assert len(all_memories) == 2
    all_skills = await store.list(tenant_id="u-sylvan", asset_kind="skill")
    assert len(all_skills) == 1
