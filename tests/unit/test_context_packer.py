"""Context packer tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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


# ---- Wire 33: pack_query (hermes use_memory) ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pack_query_returns_relevant_assets() -> None:
    """Wire 33: pack_query 用 query string 拉相关 memory."""
    store = InMemoryAssetStore()
    relevant = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "auth_service 架构"},
        summary="JWT 认证服务的设计与实现",
        tags=["auth", "jwt"],
    )
    irrelevant = LayeredAsset.build(
        "knowledge",
        "u-sylvan",
        metadata={"title": "销售话术"},
        summary="销售跟进套路",
        tags=["sales"],
    )
    await store.put(relevant)
    await store.put(irrelevant)

    pack = await ContextPacker(store).pack_query("JWT 认证 实现", tenant_id="u-sylvan")

    assert any(it.asset_id == relevant.asset_id for it in pack.items)
    assert all(it.asset_id != irrelevant.asset_id for it in pack.items)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pack_query_empty_query_returns_empty() -> None:
    store = InMemoryAssetStore()
    pack = await ContextPacker(store).pack_query("", tenant_id="u-sylvan")
    assert pack.items == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pack_query_respects_tenant() -> None:
    store = InMemoryAssetStore()
    await store.put(
        LayeredAsset.build(
            "memory",
            "u-other",
            metadata={"title": "other tenant note"},
            summary="should not leak",
            tags=["jwt"],
        )
    )

    pack = await ContextPacker(store).pack_query("jwt", tenant_id="u-sylvan")
    assert pack.items == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pack_query_respects_limit() -> None:
    store = InMemoryAssetStore()
    for i in range(5):
        await store.put(
            LayeredAsset.build(
                "memory",
                "u-sylvan",
                metadata={"title": f"jwt note {i}"},
                summary=f"about jwt content {i}",
                tags=["jwt", "auth"],
            )
        )

    pack = await ContextPacker(store).pack_query("jwt auth", tenant_id="u-sylvan", limit=2)
    assert len(pack.items) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_penalizes_failed_memories() -> None:
    store = InMemoryAssetStore()
    good = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "pytest good", "validation_outcome": "pass", "score_overall": 0.9},
        summary="pytest 修复 复现 回归",
        tags=["pytest"],
    )
    bad = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "pytest bad", "validation_outcome": "fail", "score_overall": 0.1},
        summary="pytest 修复 复现 回归",
        tags=["pytest"],
    )
    await store.put(bad)
    await store.put(good)

    pack = await ContextPacker(store).pack(_task(), tenant_id="u-sylvan", limit=2)

    assert [item.asset_id for item in pack.items][:2] == [good.asset_id, bad.asset_id]
    assert "quality_delta" in pack.items[0].score_rationale


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_adds_recalled_execution_process_hint() -> None:
    store = InMemoryAssetStore()
    process = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={
            "memory_layer": "execution_process",
            "task_type": "coding.python.pytest",
            "step_id": 1,
            "skill_used": "coding-pytest",
            "model": "gpt-test",
            "tier": "cheap",
        },
        summary="执行过程: step=1; skill=coding-pytest; 先复现 pytest 报错，再做最小修复。",
        tags=["v3", "execution_process", "coding.python.pytest", "coding-pytest", "pytest"],
    )
    await store.put(process)

    pack = await ContextPacker(store).pack(_task(), tenant_id="u-sylvan", limit=1)

    assert pack.process_experiences
    assert pack.process_experiences[0].asset_id == process.asset_id
    summary = pack.summary()
    assert "相关执行过程经验" in summary
    assert "先复现 pytest 报错" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_exposes_process_experiences_for_anchor_expand_paths() -> None:
    store = InMemoryAssetStore()
    process = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={
            "memory_layer": "execution_process",
            "task_type": "coding.python.pytest",
            "step_id": 2,
            "skill_used": "coding-pytest",
        },
        summary="执行过程: step=2; skill=coding-pytest; MAX 模式也要带上历史执行经验。",
        tags=["v3", "execution_process", "coding.python.pytest", "coding-pytest", "pytest"],
    )
    await store.put(process)

    experiences = await ContextPacker(store).recall_process_experiences(
        _task(),
        tenant_id="u-sylvan",
    )

    assert experiences
    assert experiences[0].asset_id == process.asset_id
    assert "MAX 模式" in experiences[0].summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_uses_durable_resource_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryAssetStore()
    low = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "pytest low"},
        summary="pytest 修复 复现 回归",
        tags=["pytest"],
    )
    hot = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "pytest hot"},
        summary="pytest 修复 复现 回归",
        tags=["pytest"],
    )
    await store.put(low)
    await store.put(hot)

    @asynccontextmanager
    async def fake_session_scope(**_kwargs: Any) -> AsyncIterator[object]:
        yield object()

    async def fake_load_scores(
        _session: object,
        *,
        tenant_id: str,
        resource_keys: list[str],
    ) -> dict[str, float]:
        assert tenant_id == "u-sylvan"
        assert f"memory:{hot.asset_id}" in resource_keys
        return {f"memory:{hot.asset_id}": 1.0}

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    monkeypatch.setattr(
        "kun.engineering.credit_assignment.load_resource_credit_scores", fake_load_scores
    )

    pack = await ContextPacker(store).pack_query("pytest 修复", tenant_id="u-sylvan", limit=1)

    assert [item.asset_id for item in pack.items] == [hot.asset_id]
    assert "contribution=1.00" in pack.items[0].score_rationale


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_touches_selected_assets() -> None:
    store = InMemoryAssetStore()
    asset = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={"title": "pytest touch"},
        summary="pytest 回归",
        tags=["pytest"],
    )
    await store.put(asset)

    await ContextPacker(store).pack(_task(), tenant_id="u-sylvan", limit=1)
    touched = await store.get(asset.asset_id, tenant_id="u-sylvan")

    assert touched is not None
    assert touched.access_count >= 1
