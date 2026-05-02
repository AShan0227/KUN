"""ContextPacker memory-layer filtering tests."""

import pytest
from kun.context.assets import LayeredAsset
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec


def _task() -> TaskRef:
    owner = Owner(tenant_id="tenant-memory-policy", user_id="u-memory-policy")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("fix pytest failure in payment parser", owner),
            task_id="task-memory-policy",
            task_type="coding.bugfix",
            success_criteria_short="fix pytest failure in payment parser",
            risk_level="medium",
            complexity_score=0.5,
            owner=owner,
        ),
        spec=TaskSpec(
            goal_detail="fix pytest failure in payment parser",
            success_metrics=["pytest passes"],
        ),
    )


def _asset(
    asset_id: str,
    *,
    memory_layer: str,
    summary: str,
    kind: str = "memory",
) -> LayeredAsset:
    return LayeredAsset.build(
        asset_kind=kind,  # type: ignore[arg-type]
        tenant_id="tenant-memory-policy",
        metadata={
            "memory_layer": memory_layer,
            "task_type": "coding.bugfix",
            "title": asset_id,
        },
        summary=summary,
        tags=["pytest", "payment", memory_layer],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_filters_memory_layers() -> None:
    store = InMemoryAssetStore()
    await store.put(
        _asset(
            "process",
            memory_layer="execution_process",
            summary="pytest payment parser failed because decimal normalization was skipped",
        )
    )
    await store.put(
        _asset(
            "meta",
            memory_layer="meta_decision",
            summary="payment parser bugfix used conservative SMART mode",
            kind="methodology",
        )
    )
    await store.put(
        _asset(
            "result",
            memory_layer="task_result",
            summary="payment parser pytest eventually passed",
        )
    )

    pack = await ContextPacker(store).pack(
        _task(),
        tenant_id="tenant-memory-policy",
        limit=5,
        memory_layers=["execution_process"],
    )

    assert pack.items
    assert {item.title for item in pack.items} == {"process"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_avoid_layers_blocks_process_memory() -> None:
    store = InMemoryAssetStore()
    await store.put(
        _asset(
            "process",
            memory_layer="execution_process",
            summary="pytest payment parser previous process",
        )
    )
    await store.put(
        _asset(
            "result",
            memory_layer="task_result",
            summary="pytest payment parser previous result",
        )
    )

    pack = await ContextPacker(store).pack(
        _task(),
        tenant_id="tenant-memory-policy",
        limit=5,
        avoid_memory_layers=["execution_process"],
    )

    assert pack.items
    assert {item.title for item in pack.items} == {"result"}
    assert pack.process_experiences == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_requested_layers_gate_process_experience_hints() -> None:
    store = InMemoryAssetStore()
    await store.put(
        _asset(
            "process",
            memory_layer="execution_process",
            summary="执行过程: step=1; skill=coding-pytest; previous process hint",
        )
    )
    await store.put(
        _asset(
            "result",
            memory_layer="task_result",
            summary="previous task result says pytest passed",
        )
    )

    result_only = await ContextPacker(store).pack(
        _task(),
        tenant_id="tenant-memory-policy",
        limit=5,
        memory_layers=["task_result"],
    )
    process_allowed = await ContextPacker(store).pack(
        _task(),
        tenant_id="tenant-memory-policy",
        limit=5,
        memory_layers=["execution_process"],
    )

    assert {item.title for item in result_only.items} == {"result"}
    assert result_only.process_experiences == []
    assert {item.title for item in process_allowed.items} == {"process"}
    assert process_allowed.process_experiences


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_packer_preferred_tags_soft_boost_strategy_assets() -> None:
    store = InMemoryAssetStore()
    await store.put(
        LayeredAsset.build(
            asset_kind="knowledge",
            tenant_id="tenant-memory-policy",
            metadata={"title": "generic-course"},
            summary="fix pytest payment parser course plan",
            tags=["generic"],
        )
    )
    await store.put(
        LayeredAsset.build(
            asset_kind="knowledge",
            tenant_id="tenant-memory-policy",
            metadata={"title": "education-course"},
            summary="fix pytest payment parser course plan",
            tags=["education"],
        )
    )

    pack = await ContextPacker(store).pack(
        _task(),
        tenant_id="tenant-memory-policy",
        kinds=["knowledge"],
        limit=1,
        preferred_tags=["education"],
    )

    assert [item.title for item in pack.items] == ["education-course"]
    assert "strategy_tag_boost" in pack.items[0].score_rationale
