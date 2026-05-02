"""Tests for ImportanceScorer 5-dim extension (V2.1.2 §3.2 / §18.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.context.assets import LayeredAsset
from kun.context.importance import (
    DEFAULT_WEIGHTS,
    LEGACY_3D_WEIGHTS,
    ImportanceScorer,
)


def _asset(*, access_count: int = 0, summary: str = "postgres rls migration") -> LayeredAsset:
    return LayeredAsset(
        tenant_id="t-test",
        asset_kind="memory",
        l1_metadata={"importance_tier": "long"},
        l2_summary=summary,
        tags=["postgres"],
        access_count=access_count,
        last_accessed=datetime.now(UTC),
    )


def test_default_weights_are_5d_balanced() -> None:
    """V2.1.2 默认 5 维各 0.20."""
    assert set(DEFAULT_WEIGHTS) == {"semantic", "frequency", "recency", "dependency", "pin"}
    for v in DEFAULT_WEIGHTS.values():
        assert abs(v - 0.20) < 1e-6


def test_legacy_3d_weights_still_work() -> None:
    """V1 兼容: 传入 3 维, 自动 backfill 0."""
    s = ImportanceScorer(weights=LEGACY_3D_WEIGHTS)
    assert "dependency" in s.weights
    assert "pin" in s.weights
    assert s.weights["dependency"] == 0.0
    assert s.weights["pin"] == 0.0
    # 3 维归一化后应仍 sum=1
    assert abs(sum(s.weights.values()) - 1.0) < 1e-6


def test_score_includes_dependency_and_pin() -> None:
    """打分输出 5 维分量."""
    asset = _asset(access_count=5)
    s = ImportanceScorer()
    score = s.score(
        asset=asset,
        query="postgres",
        task_dependency_score=0.8,
        pin_boost=0.5,
    )
    assert score.dependency == 0.8
    assert score.pin == 0.5
    # overall 应该被 dependency + pin 加权拉高
    assert score.overall > 0.2


def test_score_dependency_clamps_to_01() -> None:
    asset = _asset()
    s = ImportanceScorer()
    score = s.score(
        asset=asset,
        query="x",
        task_dependency_score=2.0,  # 超出 1
        pin_boost=-0.5,  # 负
    )
    assert score.dependency == 1.0
    assert score.pin == 0.0


def test_compute_dimension_weights_recency_boost_for_latest() -> None:
    """V2.1.2 §18.2.1 场景 1: 任务明示按最新偏好 → recency +0.20."""
    s = ImportanceScorer()
    base = s.compute_dimension_weights()
    boosted = s.compute_dimension_weights(
        task_meta={"intent_text": "用最新的方法做"},
    )
    assert boosted["recency"] > base["recency"]


def test_compute_dimension_weights_dependency_boost() -> None:
    """V2.1.2 场景 2: 有 required_resources → dependency +0.15."""
    s = ImportanceScorer()
    base = s.compute_dimension_weights()
    boosted = s.compute_dimension_weights(
        task_meta={"required_resources": ["postgres", "redis"]},
    )
    assert boosted["dependency"] > base["dependency"]


def test_compute_dimension_weights_pin_boost_after_recent_pin() -> None:
    """场景 4: 用户刚 pin → pin +0.20."""
    s = ImportanceScorer()
    base = s.compute_dimension_weights()
    boosted = s.compute_dimension_weights(
        user_meta={"recent_pin_action": True},
    )
    assert boosted["pin"] > base["pin"]


def test_compute_dimension_weights_critical_caps_each_dim() -> None:
    """场景 7: critical 风险 → 单维 cap 0.50."""
    s = ImportanceScorer()
    weights = s.compute_dimension_weights(
        task_meta={
            "risk_level": "critical",
            "intent_text": "用最新最新最新",  # 即使 recency 想升, 也 cap
            "required_resources": ["x"],
        },
        user_meta={"recent_pin_action": True},
    )
    for k, v in weights.items():
        assert v <= 0.50 + 1e-6, f"{k} = {v} 超过 critical 上限"


def test_compute_dimension_weights_normalizes_to_one() -> None:
    s = ImportanceScorer()
    weights = s.compute_dimension_weights(
        task_meta={"intent_text": "最新", "required_resources": ["x"]},
        user_meta={"recent_pin_action": True},
    )
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_compute_dimension_weights_major_decision_levels_out() -> None:
    """场景 6: 大决策 → 全维向基线放平."""
    s = ImportanceScorer()
    no_major = s.compute_dimension_weights(
        task_meta={"intent_text": "最新方法"},  # recency 升
    )
    major = s.compute_dimension_weights(
        task_meta={
            "intent_text": "最新方法",
            "is_major_decision": True,
        },
    )
    # major 决策下 recency 应该比无 major 决策更接近基线 0.20
    assert abs(major["recency"] - 0.20) < abs(no_major["recency"] - 0.20)


def test_score_with_weights_override() -> None:
    """支持运行时按场景动态算的权重 override."""
    asset = _asset(access_count=5)
    s = ImportanceScorer()
    custom_weights = {
        "semantic": 0.5,
        "frequency": 0.1,
        "recency": 0.1,
        "dependency": 0.2,
        "pin": 0.1,
    }
    score = s.score(
        asset=asset,
        query="postgres",
        weights_override=custom_weights,
        task_dependency_score=0.9,
    )
    # 跟默认权重 0.20 dependency 打分结果应不同
    score_default = s.score(asset=asset, query="postgres", task_dependency_score=0.9)
    assert score.overall != score_default.overall


def test_score_descriptor_5d_components() -> None:
    """ScoreDescriptor 现在带 5 维 components."""
    asset = _asset(access_count=2)
    s = ImportanceScorer()
    desc = s.score_descriptor(
        asset=asset,
        query="x",
        task_dependency_score=0.4,
        pin_boost=0.3,
    )
    assert set(desc.components) == {
        "semantic",
        "frequency",
        "recency",
        "dependency",
        "pin",
    }
    assert desc.components["dependency"] == 0.4
    assert desc.components["pin"] == 0.3


def test_unknown_weight_dim_rejected() -> None:
    with pytest.raises(ValueError, match="unknown dims"):
        ImportanceScorer(weights={"semantic": 0.5, "freaky_dim": 0.5})
