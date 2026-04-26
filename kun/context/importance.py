"""中央重要度打分器。

这个模块只做纯工程打分，不调 LLM。后续如果接 Qdrant/embedding，只要把
``embed_text`` 函数传进 ``ImportanceScorer`` 即可。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from kun.context.assets import LayeredAsset
from kun.core.scoring import ScoreDescriptor

EmbedText = Callable[[str], Sequence[float]]
ImportanceTier = Literal["permanent", "long", "short"]

# V2.1.2 §3.2 / §18.2: 5 维基线权重 (按场景动态算, 不写死任何一维上限)
DEFAULT_WEIGHTS = {
    "semantic": 0.20,
    "frequency": 0.20,
    "recency": 0.20,
    "dependency": 0.20,
    "pin": 0.20,
}
# V1 兼容: 旧 3 维权重 (传入只含 semantic/frequency/recency 时自动 backfill)
LEGACY_3D_WEIGHTS = {
    "semantic": 0.5,
    "frequency": 0.3,
    "recency": 0.2,
}
FREQUENCY_SATURATION_COUNT = 100
LONG_HALF_LIFE_DAYS = 11.25
PIN_HALF_LIFE_DAYS = 90.0  # V2 §3.5 tier 1 用户 pin
SHORT_HALF_LIFE_DAYS = 5.0


@dataclass(frozen=True)
class ImportanceScore:
    """Context 资产重要度分数 (V2.1.2 5 维)."""

    overall: float
    semantic: float
    frequency: float
    recency: float
    dependency: float = 0.0  # V2 任务依赖度
    pin: float = 0.0  # V2 用户显式 pin
    rationale: str = ""


class ImportanceScorer:
    """按 5 维 (语义相关 + 访问频率 + 近期性 + 任务依赖 + 用户 pin) 给资产打分.

    V2.1.2 §3.2 / §18.2 修订:
    - 不再写死"近期性 ≤ 0.25 铁律", 5 维权重按场景动态算
    - 加 dependency (任务硬依赖) 和 pin (用户显式 pin) 两维
    - 兼容 V1 旧 3 维权重 (会自动 backfill dependency=0 / pin=0)
    """

    def __init__(
        self,
        *,
        embed_text: EmbedText | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._embed_text = embed_text
        self.weights = _normalize_weights(weights or DEFAULT_WEIGHTS)

    def score(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        task_dependency_score: float = 0.0,
        pin_boost: float = 0.0,
        weights_override: dict[str, float] | None = None,
    ) -> ImportanceScore:
        """给一个资产打 0..1 的重要度分 (5 维 V2).

        Args:
            task_dependency_score: 0-1, 该资产是否当前任务硬依赖
                (TASK.md required_resources 命中 / 关联任务的能力卡 / 等)
            pin_boost: 0-1, 用户显式 pin 加权 (来自 AttentionAnchor.boost_for_asset)
            weights_override: V2 §18.2 按场景动态算的权重, 不传用 self.weights
        """
        now = now or datetime.now(UTC)
        semantic = self.semantic(asset=asset, query=query)
        frequency = self.frequency(asset.access_count)
        recency = self.recency(asset=asset, now=now)
        dependency = _clamp01(task_dependency_score)
        pin = _clamp01(pin_boost)

        weights = _normalize_weights(weights_override or self.weights)
        overall = _clamp01(
            weights.get("semantic", 0.0) * semantic
            + weights.get("frequency", 0.0) * frequency
            + weights.get("recency", 0.0) * recency
            + weights.get("dependency", 0.0) * dependency
            + weights.get("pin", 0.0) * pin
        )
        return ImportanceScore(
            overall=overall,
            semantic=semantic,
            frequency=frequency,
            recency=recency,
            dependency=dependency,
            pin=pin,
            rationale=(
                f"semantic={semantic:.3f}, frequency={frequency:.3f}, "
                f"recency={recency:.3f}, dependency={dependency:.3f}, "
                f"pin={pin:.3f}; weights={weights}"
            ),
        )

    def semantic(self, *, asset: LayeredAsset, query: str | None = None) -> float:
        """资产和 query 的语义相关度。"""
        if not query:
            return 1.0

        asset_text = _asset_text(asset)
        if not asset_text.strip():
            return 0.0

        if self._embed_text is not None:
            try:
                return _cosine_score(self._embed_text(query), self._embed_text(asset_text))
            except (TypeError, ValueError, ZeroDivisionError):
                # embedding 服务或注入函数异常时，回到本地兜底，不让打分器中断主流程。
                pass

        return _lexical_similarity(query, asset_text)

    @staticmethod
    def frequency(access_count: int) -> float:
        """访问频率分：100 次左右饱和到 1。"""
        n = max(0, access_count)
        return _clamp01(math.log1p(n) / math.log1p(FREQUENCY_SATURATION_COUNT))

    def recency(self, *, asset: LayeredAsset, now: datetime | None = None) -> float:
        """近期性分：按资产层级使用不同半衰期。"""
        half_life = half_life_days(asset)
        if half_life is None:
            return 1.0

        now = now or datetime.now(UTC)
        elapsed_days = (now - asset.last_accessed).total_seconds() / 86400
        if elapsed_days <= 0:
            return 1.0
        return _clamp01(math.exp(-elapsed_days / half_life))

    def score_descriptor(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        task_dependency_score: float = 0.0,
        pin_boost: float = 0.0,
        weights_override: dict[str, float] | None = None,
    ) -> ScoreDescriptor:
        """兼容现有统一打分展示层 (V2.1.2 5 维)."""
        score = self.score(
            asset=asset,
            query=query,
            now=now,
            task_dependency_score=task_dependency_score,
            pin_boost=pin_boost,
            weights_override=weights_override,
        )
        half_life = half_life_days(asset)
        weights_used = _normalize_weights(weights_override or self.weights)
        return ScoreDescriptor(
            kind="importance",
            value=score.overall,
            components={
                "semantic": score.semantic,
                "frequency": score.frequency,
                "recency": score.recency,
                "dependency": score.dependency,
                "pin": score.pin,
            },
            weights=weights_used,
            sample_size=max(0, asset.access_count),
            decay_half_life_days=None if half_life is None else round(half_life),
        )

    def score_with_anchors(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        task_meta: dict[str, object] | None = None,
        user_meta: dict[str, object] | None = None,
        context_meta: dict[str, object] | None = None,
    ) -> ImportanceScore:
        """V2.1 wire 便捷入口: 自动从 AttentionManager 拉 pin boost +
        从场景算动态权重.

        相当于:
        - boost_for_asset(asset, user, project) → pin_boost (§3.5 tier 1)
        - compute_dimension_weights(task, user, context) → weights_override
        - 返 5 维 score (含 dependency / pin)

        让调用方不需要手动算 anchor + 权重.
        """
        from kun.core.attention_anchor import get_manager  # 局部 import 避免循环

        # 取该 asset 的 pin boost
        asset_ref = asset.l1_metadata.get("asset_id") or getattr(asset, "asset_id", "")
        pin_boost = 0.0
        if asset_ref:
            try:
                pin_boost = get_manager().boost_for_asset(
                    str(asset_ref),
                    user_id=user_id,
                    project_id=project_id,
                )
            except Exception:
                pin_boost = 0.0

        # 任务依赖度: 从 task_meta.required_resources 简单匹配
        task_dependency_score = 0.0
        if task_meta:
            required = task_meta.get("required_resources", []) or []
            if isinstance(required, list) and asset_ref:
                if str(asset_ref) in required:
                    task_dependency_score = 1.0
                # 部分匹配 (asset 含 required keyword)
                else:
                    text_for_match = (
                        str(asset.l2_summary or "")
                        + " "
                        + " ".join(str(t) for t in (asset.tags or []))
                    ).lower()
                    for r in required:
                        if str(r).lower() in text_for_match:
                            task_dependency_score = max(task_dependency_score, 0.6)

        # 按场景算权重
        weights = self.compute_dimension_weights(
            task_meta=task_meta,
            user_meta=user_meta,
            context_meta=context_meta,
        )

        return self.score(
            asset=asset,
            query=query,
            now=now,
            task_dependency_score=task_dependency_score,
            pin_boost=pin_boost,
            weights_override=weights,
        )

    def compute_dimension_weights(
        self,
        *,
        task_meta: dict[str, object] | None = None,
        user_meta: dict[str, object] | None = None,
        context_meta: dict[str, object] | None = None,
    ) -> dict[str, float]:
        """V2.1.2 §18.2.1: 按场景动态算 5 维权重 (不写死任何上限).

        基线:5 维等权 0.20. 按场景调整:
        - 任务明示"按最新偏好" → recency +0.20
        - 任务有 required_resources → dependency +0.15
        - 长对话频繁切话题 → semantic +0.15
        - 用户刚 pin 资产 → pin +0.20
        - 高频复用任务 → frequency +0.10
        - 大决策 → 全维放平
        - critical 风险 → 单维 cap 0.50 防被一维带跑
        """
        task = task_meta or {}
        user = user_meta or {}
        context = context_meta or {}

        w = dict(DEFAULT_WEIGHTS)

        # 场景 1: 任务明示按最新
        intent = str(task.get("intent_text", "")).lower()
        if any(s in intent for s in ("最新", "现在", "刚才", "latest", "recent")):
            w["recency"] += 0.20

        # 场景 2: 有 required_resources
        if task.get("required_resources"):
            w["dependency"] += 0.15

        # 场景 3: 长对话频繁切话题
        topic_switches = context.get("topic_switches_in_session", 0)
        if isinstance(topic_switches, int) and topic_switches >= 3:
            w["semantic"] += 0.15

        # 场景 4: 用户刚 pin
        if user.get("recent_pin_action"):
            w["pin"] += 0.20

        # 场景 5: 高频复用 task_type
        if task.get("is_high_frequency_type"):
            w["frequency"] += 0.10

        # 场景 6: 大决策 → 全维放平 (向基线靠拢)
        if task.get("is_major_decision"):
            for k in w:
                w[k] = (w[k] + DEFAULT_WEIGHTS[k]) / 2

        # 场景 7: critical 风险 → 单维 cap 0.50
        if task.get("risk_level") == "critical":
            w = {k: min(v, 0.50) for k, v in w.items()}

        return _normalize_weights(w)

    def review_needed(self, *, asset: LayeredAsset, score: ImportanceScore) -> bool:
        """判断是否需要后续便宜模型复审。

        这里只返回信号，不在打分器里直接调模型或写事件，避免 T1 污染主流程。
        """
        repeatedly_used_but_low = asset.access_count >= 10 and score.overall < 0.25
        long_unvisited_but_kept = asset.access_count == 0 and score.recency < 0.05
        return repeatedly_used_but_low or long_unvisited_but_kept


def half_life_days(asset: LayeredAsset) -> float | None:
    """根据资产元数据/类型得到半衰期。None 表示永久档。"""
    tier = _importance_tier(asset)
    if tier == "permanent":
        return None
    if tier == "short":
        return SHORT_HALF_LIFE_DAYS
    return LONG_HALF_LIFE_DAYS


def _importance_tier(asset: LayeredAsset) -> ImportanceTier:
    raw = str(
        asset.l1_metadata.get("importance_tier")
        or asset.l1_metadata.get("retention_tier")
        or asset.l1_metadata.get("tier")
        or ""
    ).lower()
    tags = {tag.lower() for tag in asset.tags}
    if raw in {"permanent", "tier0", "forever"} or tags & {"permanent", "tier0"}:
        return "permanent"
    if raw in {"short", "short_term", "temporary"} or asset.asset_kind in {"task", "handoff"}:
        return "short"
    return "long"


def _asset_text(asset: LayeredAsset) -> str:
    parts: list[str] = [
        asset.asset_kind,
        " ".join(asset.tags),
        asset.l2_summary or "",
        " ".join(f"{key} {value}" for key, value in asset.l1_metadata.items()),
    ]
    return " ".join(parts)


def _cosine_score(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    cosine = dot / (left_norm * right_norm)
    return _clamp01((cosine + 1.0) / 2.0)


def _lexical_similarity(query: str, asset_text: str) -> float:
    query_terms = _terms(query)
    asset_terms = _terms(asset_text)
    if not query_terms or not asset_terms:
        return 0.0
    overlap = len(query_terms & asset_terms)
    return _clamp01(overlap / math.sqrt(len(query_terms) * len(asset_terms)))


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[\w.-]+", text) if len(part) >= 2}


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """V2.1.2: 接受 5 维 (semantic/frequency/recency/dependency/pin) 或
    V1 兼容 3 维 (semantic/frequency/recency, 自动 backfill dependency=0/pin=0).

    所有未指定维度按 0 填充, 然后归一化.
    """
    expected = {"semantic", "frequency", "recency", "dependency", "pin"}
    actual = set(weights.keys())
    unknown = actual - expected
    if unknown:
        raise ValueError(
            f"importance weights contain unknown dims {sorted(unknown)}. "
            f"Allowed: {sorted(expected)}"
        )
    # backfill 未指定的维度 = 0 (V1 旧 3 维传进来时, dependency / pin 自动 0)
    full = {key: float(weights.get(key, 0.0)) for key in expected}
    total = sum(full.values())
    if total <= 0:
        raise ValueError("importance weights must sum to a positive number")
    return {key: value / total for key, value in full.items()}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "ImportanceScore",
    "ImportanceScorer",
    "half_life_days",
]
