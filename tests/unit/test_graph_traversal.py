"""GraphTraversal + score_anchor_then_expand mempalace wire (Wire 30 / V2.2 §20)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from kun.context.graph_traversal import (
    DEFAULT_TRAVERSAL_TYPES,
    GraphTraversal,
    NeighborEntity,
)

# ---- NeighborEntity.score ----


def test_neighbor_score_decays_with_hops() -> None:
    near = NeighborEntity("asset", "a-2", "depends_on", 0.9, 1, (("asset", "a-1"), ("asset", "a-2")))
    far = NeighborEntity("asset", "a-3", "depends_on", 0.9, 2, (("asset", "a-1"), ("asset", "a-2"), ("asset", "a-3")))
    assert near.score > far.score


def test_neighbor_score_uses_confidence() -> None:
    high = NeighborEntity("asset", "a", "depends_on", 0.9, 1, (("asset", "src"), ("asset", "a")))
    low = NeighborEntity("asset", "b", "depends_on", 0.3, 1, (("asset", "src"), ("asset", "b")))
    assert high.score > low.score


def test_default_traversal_types_excludes_contradicts() -> None:
    """contradicts 默认排除 — mempalace 不沿矛盾走."""
    assert "contradicts" not in DEFAULT_TRAVERSAL_TYPES
    assert "depends_on" in DEFAULT_TRAVERSAL_TYPES


# ---- GraphTraversal._bfs (mock _fetch_edges) ----


@pytest.mark.asyncio
async def test_bfs_hop1_returns_direct_neighbors() -> None:
    """1 跳 → 直接邻居."""
    tr = GraphTraversal()
    tr._fetch_edges = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"target_kind": "asset", "target_id": "a-2", "relation_type": "depends_on", "confidence": 0.9},
            {"target_kind": "asset", "target_id": "a-3", "relation_type": "mentions", "confidence": 0.5},
        ]
    )
    neighbors = await tr.neighbors("asset", "a-1", hops=1)
    assert len(neighbors) == 2
    assert neighbors[0].entity_id == "a-2"  # 高 confidence 排前
    assert neighbors[0].score > neighbors[1].score


@pytest.mark.asyncio
async def test_bfs_hop2_walks_two_levels_dedup() -> None:
    """2 跳 + 去重."""
    tr = GraphTraversal()

    call_count = 0

    async def fake_fetch(**kwargs):
        nonlocal call_count
        call_count += 1
        src_id = kwargs["source_id"]
        if src_id == "a-1":
            return [
                {"target_kind": "asset", "target_id": "a-2", "relation_type": "depends_on", "confidence": 0.9},
            ]
        if src_id == "a-2":
            return [
                {"target_kind": "asset", "target_id": "a-3", "relation_type": "depends_on", "confidence": 0.7},
                {"target_kind": "asset", "target_id": "a-1", "relation_type": "depends_on", "confidence": 0.4},  # 回环
            ]
        return []

    tr._fetch_edges = fake_fetch  # type: ignore[method-assign]
    neighbors = await tr.neighbors("asset", "a-1", hops=2)

    ids = [n.entity_id for n in neighbors]
    assert "a-2" in ids
    assert "a-3" in ids
    assert "a-1" not in ids  # 起点不重入
    # 1 跳邻居 (a-2) score 应该高于 2 跳邻居 (a-3, decay)
    a2 = next(n for n in neighbors if n.entity_id == "a-2")
    a3 = next(n for n in neighbors if n.entity_id == "a-3")
    assert a2.score > a3.score


@pytest.mark.asyncio
async def test_bfs_hops_zero_returns_empty() -> None:
    tr = GraphTraversal()
    assert await tr.neighbors("asset", "a-1", hops=0) == []


@pytest.mark.asyncio
async def test_bfs_db_failure_returns_empty() -> None:
    """_fetch_edges 抛异常 → 静默返空 (best-effort)."""
    tr = GraphTraversal()
    tr._fetch_edges = AsyncMock(side_effect=RuntimeError("simulated db crash"))  # type: ignore[method-assign]
    result = await tr.neighbors("asset", "a-1", hops=1)
    assert result == []


@pytest.mark.asyncio
async def test_bfs_relation_types_filter() -> None:
    """relation_types 参数限制只走某些关系."""
    tr = GraphTraversal()
    captured_filters: list[Any] = []

    async def fake_fetch(*, source_kind, source_id, relation_types, limit):
        captured_filters.append(relation_types)
        return []

    tr._fetch_edges = fake_fetch  # type: ignore[method-assign]
    await tr.neighbors("asset", "a-1", hops=1, relation_types=["depends_on", "verifies"])
    assert captured_filters[0] == ("depends_on", "verifies")


# ---- score_anchor_then_expand 集成 graph_traversal ----


@pytest.mark.asyncio
async def test_score_expand_with_traversal_walks_neighbors_first() -> None:
    """有 graph_traversal → expand 优先返邻接, 不按 score 降序."""
    from kun.context.importance import ImportanceScorer
    from kun.datamodel.layered_asset import LayeredAsset

    def _asset(eid: str, score_overall: float) -> LayeredAsset:
        return LayeredAsset(
            asset_id=eid,
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={
                "entity_id": eid,
                "task_type": "general.x",
                "importance_signal": score_overall,
            },
        )

    candidates = [
        _asset("a-anchor", 0.9),  # score 最高 → anchor
        _asset("a-far", 0.7),  # score 第 2 高 (但不在 graph 邻接)
        _asset("a-neighbor", 0.5),  # score 低, 但是 anchor 的邻居
    ]

    scorer = ImportanceScorer()

    fake_traversal = AsyncMock()
    fake_traversal.neighbors = AsyncMock(
        return_value=[
            NeighborEntity(
                entity_kind="asset",
                entity_id="a-neighbor",
                relation_type="depends_on",
                confidence=0.85,
                hops=1,
                via_path=(("asset", "a-anchor"), ("asset", "a-neighbor")),
            ),
        ]
    )

    iterator = scorer.score_anchor_then_expand(
        candidates,
        query="test",
        graph_traversal=fake_traversal,
        graph_hops=1,
        candidate_entity_kind="asset",
        max_rounds=2,
        use_marginal_stop=False,
    )

    yielded: list[str] = []
    async for asset, _score in iterator:
        yielded.append(asset.l1_metadata["entity_id"])

    assert yielded[0] == "a-anchor"  # 第 1 轮 anchor 最高 score
    assert yielded[1] == "a-neighbor"  # 第 2 轮走 graph 邻接 (而不是 a-far)


@pytest.mark.asyncio
async def test_score_expand_no_traversal_falls_back_to_score_order() -> None:
    """没 graph_traversal → 按 score 降序 (现有行为)."""
    from kun.context.importance import ImportanceScorer
    from kun.datamodel.layered_asset import LayeredAsset

    def _asset(eid: str, signal: float) -> LayeredAsset:
        return LayeredAsset(
            asset_id=eid,
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={
                "entity_id": eid,
                "task_type": "general.x",
                "importance_signal": signal,
            },
        )

    candidates = [
        _asset("a-1", 0.9),
        _asset("a-2", 0.7),
        _asset("a-3", 0.5),
    ]

    scorer = ImportanceScorer()
    iterator = scorer.score_anchor_then_expand(
        candidates,
        query="test",
        max_rounds=3,
        use_marginal_stop=False,
        # 不传 graph_traversal
    )

    yielded: list[str] = []
    async for asset, _score in iterator:
        yielded.append(asset.l1_metadata["entity_id"])

    # 按 importance_signal 降序: a-1 → a-2 → a-3
    assert yielded == ["a-1", "a-2", "a-3"]


@pytest.mark.asyncio
async def test_score_expand_traversal_returns_empty_falls_back_to_score() -> None:
    """graph 邻接为空 (relationship 表没数据) → 回退按 score."""
    from kun.context.importance import ImportanceScorer
    from kun.datamodel.layered_asset import LayeredAsset

    candidates = [
        LayeredAsset(
            asset_id="a-1",
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={"entity_id": "a-1", "importance_signal": 0.9},
        ),
        LayeredAsset(
            asset_id="a-2",
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={"entity_id": "a-2", "importance_signal": 0.7},
        ),
    ]

    scorer = ImportanceScorer()

    fake_traversal = AsyncMock()
    fake_traversal.neighbors = AsyncMock(return_value=[])  # 没邻居

    iterator = scorer.score_anchor_then_expand(
        candidates,
        graph_traversal=fake_traversal,
        graph_hops=1,
        max_rounds=2,
        use_marginal_stop=False,
    )

    yielded: list[str] = []
    async for asset, _score in iterator:
        yielded.append(asset.l1_metadata["entity_id"])

    assert yielded == ["a-1", "a-2"]


@pytest.mark.asyncio
async def test_score_expand_traversal_failure_falls_back() -> None:
    """traversal.neighbors 抛异常 → 回退 score 降序, 不破."""
    from kun.context.importance import ImportanceScorer
    from kun.datamodel.layered_asset import LayeredAsset

    candidates = [
        LayeredAsset(
            asset_id="a-1",
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={"entity_id": "a-1", "importance_signal": 0.9},
        ),
        LayeredAsset(
            asset_id="a-2",
            asset_kind="memory",
            tenant_id="u-test",
            l1_metadata={"entity_id": "a-2", "importance_signal": 0.7},
        ),
    ]

    scorer = ImportanceScorer()

    fake_traversal = AsyncMock()
    fake_traversal.neighbors = AsyncMock(side_effect=RuntimeError("crash"))

    iterator = scorer.score_anchor_then_expand(
        candidates,
        graph_traversal=fake_traversal,
        max_rounds=2,
        use_marginal_stop=False,
    )

    yielded: list[str] = []
    async for asset, _score in iterator:
        yielded.append(asset.l1_metadata["entity_id"])

    assert yielded == ["a-1", "a-2"]


def test_asset_entity_id_helper_priority() -> None:
    """l1_metadata.entity_id > asset_id > id(asset) fallback."""
    from kun.context.importance import ImportanceScorer
    from kun.datamodel.layered_asset import LayeredAsset

    a1 = LayeredAsset(
        asset_id="x",
        asset_kind="memory",
        tenant_id="u-test",
        l1_metadata={"entity_id": "explicit-id"},
    )
    assert ImportanceScorer._asset_entity_id(a1) == "explicit-id"

    a2 = LayeredAsset(
        asset_id="x",
        asset_kind="memory",
        tenant_id="u-test",
        l1_metadata={"asset_id": "asset-fallback"},
    )
    assert ImportanceScorer._asset_entity_id(a2) == "asset-fallback"

    a3 = LayeredAsset(
        asset_id="x", asset_kind="memory", tenant_id="u-test", l1_metadata={}
    )
    assert ImportanceScorer._asset_entity_id(a3).startswith("asset-")
