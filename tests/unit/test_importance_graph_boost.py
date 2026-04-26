"""ImportanceScorer 知识图谱 boost 集成测试 (V2.2 §20.3 Wire 7)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import ImportanceScorer


def _make_asset(asset_id: str, kind: str = "memory", text: str = "") -> LayeredAsset:
    return LayeredAsset(
        asset_id=asset_id,
        asset_kind=kind,
        l1_metadata={"name": asset_id, "asset_id": asset_id},
        l2_summary=text,
        l3_ref=None,
        tags=[],
        access_count=1,
        last_accessed=datetime.now(UTC),
        tenant_id="t-test",
    )


def _make_relationship(source_id: str, target_id: str, relation_type: str = "depends_on"):
    """Return a fake EntityRelationship object (only target_entity_id is used)."""

    class _Rel:
        pass

    r = _Rel()
    r.source_entity_id = source_id
    r.target_entity_id = target_id
    r.relation_type = relation_type
    return r


@pytest.mark.asyncio
async def test_no_anchor_returns_normal_score() -> None:
    """anchor_entity_id=None → 不做 boost, 退化到普通 score."""
    scorer = ImportanceScorer()
    candidates = [_make_asset("a"), _make_asset("b")]
    results = await scorer.score_with_graph_boost(
        candidates,
        anchor_entity_id=None,
        tenant_id="t-test",
        query="x",
    )
    assert len(results) == 2
    # 没 boost, 只是普通排序
    assert all("graph_boost" not in r[1].rationale for r in results)


@pytest.mark.asyncio
async def test_with_anchor_boosts_related_assets() -> None:
    """anchor 的邻接节点 (target_entity_id) 应该被 boost."""
    scorer = ImportanceScorer()
    candidates = [
        _make_asset("a-related"),
        _make_asset("b-unrelated"),
        _make_asset("c-related"),
    ]
    fake_rels = [
        _make_relationship("anchor-mem", "a-related"),
        _make_relationship("anchor-mem", "c-related"),
    ]

    with patch(
        "kun.datamodel.relationship.get_relationships_from",
        new=AsyncMock(return_value=fake_rels),
    ):
        results = await scorer.score_with_graph_boost(
            candidates,
            anchor_entity_kind="memory",
            anchor_entity_id="anchor-mem",
            tenant_id="t-test",
            query="x",
            graph_boost=0.20,
        )

    # related 节点应该有 graph_boost rationale
    boosted = {r[0].asset_id for r in results if "graph_boost" in r[1].rationale}
    assert "a-related" in boosted
    assert "c-related" in boosted
    assert "b-unrelated" not in boosted


@pytest.mark.asyncio
async def test_graph_boost_caps_at_one() -> None:
    """boost 不能让 overall 超 1.0."""
    scorer = ImportanceScorer()
    candidates = [_make_asset("hot")]
    fake_rels = [_make_relationship("anchor", "hot")]

    with patch(
        "kun.datamodel.relationship.get_relationships_from",
        new=AsyncMock(return_value=fake_rels),
    ):
        results = await scorer.score_with_graph_boost(
            candidates,
            anchor_entity_id="anchor",
            tenant_id="t-test",
            query="hot",
            graph_boost=0.99,  # 超大 boost
        )
    assert results[0][1].overall <= 1.0


@pytest.mark.asyncio
async def test_db_failure_falls_back_to_normal_score() -> None:
    """relationship DB 查失败 → 退化到普通 score, 不 crash."""
    scorer = ImportanceScorer()
    candidates = [_make_asset("a"), _make_asset("b")]

    with patch(
        "kun.datamodel.relationship.get_relationships_from",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        results = await scorer.score_with_graph_boost(
            candidates,
            anchor_entity_id="anchor-x",
            tenant_id="t-test",
        )
    # 没 crash, 返普通 score
    assert len(results) == 2
    assert all("graph_boost" not in r[1].rationale for r in results)


@pytest.mark.asyncio
async def test_results_sorted_by_overall_desc() -> None:
    scorer = ImportanceScorer()
    candidates = [
        _make_asset("low-no-boost", text="random"),
        _make_asset("high-with-boost", text="auth login"),
    ]
    fake_rels = [_make_relationship("anchor", "low-no-boost")]

    with patch(
        "kun.datamodel.relationship.get_relationships_from",
        new=AsyncMock(return_value=fake_rels),
    ):
        results = await scorer.score_with_graph_boost(
            candidates,
            anchor_entity_id="anchor",
            tenant_id="t-test",
            query="auth login",
            graph_boost=0.50,  # 大 boost 把 low 推到 high 之上
        )
    # low-no-boost 加 0.5 boost 后可能超过 high-with-boost
    overall_scores = [r[1].overall for r in results]
    # 验证排序
    assert overall_scores == sorted(overall_scores, reverse=True)
