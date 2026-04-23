"""Central Importance Scorer (§3.2).

三因素:
  - 语义相关度 (embedding similarity)
  - 访问频率 (带饱和的计数器)
  - 近期性 (时间戳)

Used everywhere: 检索权重 / 衰减速度 / 层级归属.

ADR-018 §16.1 归并: 输出 ScoreDescriptor (kind="importance").
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from kun.context.assets import LayeredAsset
from kun.core.scoring import ScoreDescriptor


class ImportanceScorer:
    """打分器: 综合 3 因素打 importance 分."""

    def __init__(
        self,
        *,
        freq_saturation_k: float = 10.0,
        recency_decay_days: float = 30.0,
        weights: dict[str, float] | None = None,
    ) -> None:
        """
        Args:
            freq_saturation_k: 访问频率饱和参数; score = freq / (freq + k).
                              k=10 means 10 accesses ≈ 0.5, 100 accesses ≈ 0.9.
            recency_decay_days: 近期性衰减半衰期.
            weights: dict with keys {relevance, frequency, recency}, must sum 1.
        """
        self.k = freq_saturation_k
        self.decay_days = recency_decay_days
        self.weights = weights or {"relevance": 0.5, "frequency": 0.3, "recency": 0.2}

    # ---------- component scorers ----------

    @staticmethod
    def relevance(cosine_similarity: float) -> float:
        """Normalize cosine similarity [-1,1] to [0,1]."""
        return max(0.0, min(1.0, (cosine_similarity + 1.0) / 2.0))

    def frequency(self, access_count: int) -> float:
        """Saturating frequency score in [0,1]."""
        n = max(0, access_count)
        return n / (n + self.k)

    def recency(self, last_access: datetime, now: datetime | None = None) -> float:
        """Exponential decay by half-life."""
        now = now or datetime.now(UTC)
        elapsed = (now - last_access).total_seconds() / 86400
        if elapsed <= 0:
            return 1.0
        return math.exp(-math.log(2) * elapsed / self.decay_days)

    # ---------- compound scoring ----------

    def score(
        self,
        *,
        relevance_cos: float,
        access_count: int,
        last_access: datetime,
        now: datetime | None = None,
        half_life_days: int = 11,  # FadeMem 长期层
    ) -> ScoreDescriptor:
        """Compute a ScoreDescriptor for an asset."""
        components = {
            "relevance": self.relevance(relevance_cos),
            "frequency": self.frequency(access_count),
            "recency": self.recency(last_access, now),
        }
        return ScoreDescriptor.compose(
            kind="importance",
            components=components,
            weights=self.weights,
            sample_size=access_count,
            half_life_days=half_life_days,
        )

    def score_asset(
        self,
        asset: LayeredAsset,
        *,
        relevance_cos: float,
        now: datetime | None = None,
    ) -> ScoreDescriptor:
        """Convenience wrapper that pulls access stats off a LayeredAsset."""
        return self.score(
            relevance_cos=relevance_cos,
            access_count=asset.access_count,
            last_access=asset.last_accessed,
            now=now,
        )
