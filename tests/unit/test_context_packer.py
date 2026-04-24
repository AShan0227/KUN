"""Context packer tests."""

from __future__ import annotations

import pytest
from kun.context.assets import LayeredAsset
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec


def _task() -> TaskRef:
    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("pytest report", owner),
        task_type="coding.python.pytest",
        owner=owner,
        success_criteria_short="生成 pytest 修复报告",
    )
    spec = TaskSpec(
        goal_detail="修复 pytest 失败并输出报告",
        success_metrics=["pytest 全部通过"],
        required_skills=["coding-pytest"],
    )
    return TaskRef(meta=meta, spec=spec)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_selects_relevant_assets() -> None:
    store = InMemoryAssetStore()
    relevant = LayeredAsset.build(
        "methodology",
        "u-sylvan",
        metadata={"title": "pytest 修复方法论"},
        summary="遇到 pytest 失败时，先复现，再最小修复，再回归测试。",
        tags=["pytest", "coding"],
    )
    irrelevant = LayeredAsset.build(
        "knowledge",
        "u-sylvan",
        metadata={"title": "销售话术"},
        summary="和销售线索跟进有关。",
        tags=["sales"],
    )
    await store.put(relevant)
    await store.put(irrelevant)

    pack = await ContextPacker(store).pack(_task(), tenant_id="u-sylvan")

    assert [item.asset_id for item in pack.items] == [relevant.asset_id]
    summary = pack.summary()
    assert "pytest 修复方法论" in summary
    assert "先复现" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_keeps_tenant_boundary() -> None:
    store = InMemoryAssetStore()
    await store.put(
        LayeredAsset.build(
            "memory",
            "u-other",
            metadata={"title": "pytest secret"},
            summary="other tenant data",
            tags=["pytest"],
        )
    )

    pack = await ContextPacker(store).pack(_task(), tenant_id="u-sylvan")

    assert pack.items == []
