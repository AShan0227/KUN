"""Tests for ImportanceScorer.score_with_anchors (V2.1 wire W4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import ImportanceScorer
from kun.core.attention_anchor import (
    AttentionAnchor,
    get_manager,
    reset_manager,
)


def _asset(asset_id: str = "ka-1", summary: str = "postgres rls migration") -> LayeredAsset:
    return LayeredAsset(
        tenant_id="t-test",
        asset_kind="memory",
        l1_metadata={"importance_tier": "long", "asset_id": asset_id},
        l2_summary=summary,
        tags=["postgres", "rls"],
        access_count=5,
        last_accessed=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _reset_anchors():
    reset_manager()
    yield
    reset_manager()


def test_score_with_no_anchors_no_pin_boost() -> None:
    s = ImportanceScorer()
    asset = _asset()
    score = s.score_with_anchors(asset=asset, query="postgres", user_id="u-1")
    assert score.pin == 0.0


def test_score_with_user_pin_anchor_gets_boost() -> None:
    """用户 pin 该 asset → score_with_anchors 自动拿 boost."""
    mgr = get_manager()
    mgr.add(
        AttentionAnchor(
            anchor_kind="user_pin",
            target_asset_ref="ka-postgres",
            weight_boost=0.4,
            user_id="u-1",
        )
    )

    s = ImportanceScorer()
    asset = _asset(asset_id="ka-postgres")
    score = s.score_with_anchors(
        asset=asset,
        query="postgres",
        user_id="u-1",
    )
    assert score.pin == 0.4


def test_score_with_anchors_other_user_no_boost() -> None:
    """别人 pin 的 asset → 当前 user 不拿 boost."""
    mgr = get_manager()
    mgr.add(
        AttentionAnchor(
            anchor_kind="user_pin",
            target_asset_ref="ka-postgres",
            weight_boost=0.4,
            user_id="u-other",
        )
    )
    s = ImportanceScorer()
    asset = _asset(asset_id="ka-postgres")
    score = s.score_with_anchors(
        asset=asset,
        query="x",
        user_id="u-1",
    )
    assert score.pin == 0.0


def test_score_with_task_dependency_exact_match() -> None:
    s = ImportanceScorer()
    asset = _asset(asset_id="ka-postgres", summary="postgres setup")
    score = s.score_with_anchors(
        asset=asset,
        query="x",
        task_meta={"required_resources": ["ka-postgres"]},
    )
    assert score.dependency == 1.0


def test_score_with_task_dependency_partial_match() -> None:
    """asset 内容含 required keyword → 部分匹配 0.6."""
    s = ImportanceScorer()
    asset = _asset(asset_id="ka-other", summary="postgres tutorial")
    score = s.score_with_anchors(
        asset=asset,
        query="x",
        task_meta={"required_resources": ["postgres"]},
    )
    assert score.dependency == 0.6


def test_score_with_no_required_resources_zero_dep() -> None:
    s = ImportanceScorer()
    asset = _asset()
    score = s.score_with_anchors(asset=asset, query="x")
    assert score.dependency == 0.0


def test_score_with_anchors_uses_dynamic_weights() -> None:
    """场景化权重生效 (任务明示按最新 → recency 升)."""
    s = ImportanceScorer()
    asset = _asset()
    # task_meta 含"最新"应让 recency 权重升, 影响 overall
    score_recent = s.score_with_anchors(
        asset=asset,
        query="postgres",
        task_meta={"intent_text": "用最新的方法"},
    )
    score_default = s.score_with_anchors(
        asset=asset,
        query="postgres",
    )
    # 两个 overall 应不一样 (dynamic weights 生效)
    assert score_recent.overall != score_default.overall


def test_score_with_anchors_multiple_anchors_takes_max() -> None:
    """多 anchor 取最大 boost (avoid 累加爆表)."""
    mgr = get_manager()
    mgr.add(
        AttentionAnchor(
            anchor_kind="user_pin",
            target_asset_ref="ka-x",
            weight_boost=0.15,
            user_id="u-1",
        )
    )
    mgr.add(
        AttentionAnchor(
            anchor_kind="permanent_redline",
            target_asset_ref="ka-x",
            weight_boost=0.30,
        )
    )
    s = ImportanceScorer()
    asset = _asset(asset_id="ka-x")
    score = s.score_with_anchors(asset=asset, query="x", user_id="u-1")
    assert score.pin == 0.30  # max, not sum
