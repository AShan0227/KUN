"""ScoreDescriptor — 统一所有打分系统 (ADR-018 §16.1).

合并前: Context 重要度、能力卡 stats、一致性分数、rubric 评分、surprise_score 五套.
合并后: 单一 ScoreDescriptor 基类, kind 区分语义.

收益: 打分展示 / 衰减 / 版本化 / 调试 UI 统一一套实现.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from kun.core.ids import new_id

ScoreKind = Literal[
    "importance",  # §3.2 Context 重要度
    "capability",  # §13.2 能力卡 success_rate / rubric_score
    "consistency",  # §8.4 多样本一致性
    "rubric",  # §8.1 评分表
    "surprise",  # §8.7 意外度
    "confidence",  # 单次置信度(来自多样本 / 能力卡 snapshot)
]


class ScoreDescriptor(BaseModel):
    """统一打分载体.

    Args:
        value: 0-1 标准化打分 (上层应用可投射到其他区间).
        confidence: 可选 0-1 置信度. None 表示单点测量无置信度概念.
        ci95: 可选 95% 置信区间 (lo, hi).
        components: 各分量的分解, 保证可解释性.
        weights: components 的权重.
        sample_size: 有效样本数 (考虑衰减后).
    """

    score_id: str = Field(default_factory=lambda: new_id("score"))
    kind: ScoreKind
    value: float = Field(ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    ci95: tuple[float, float] | None = None
    components: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    sample_size: int = Field(default=0, ge=0)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decay_half_life_days: int | None = None

    @field_validator("ci95")
    @classmethod
    def _validate_ci(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return v
        lo, hi = v
        if not (0.0 <= lo <= hi <= 1.0):
            raise ValueError(f"ci95 must be within [0,1] and lo <= hi, got {v}")
        return v

    @field_validator("weights")
    @classmethod
    def _validate_weights(cls, v: dict[str, float]) -> dict[str, float]:
        if v and (s := sum(v.values())) > 0 and not math.isclose(s, 1.0, abs_tol=0.01):
            raise ValueError(f"weights must sum to 1.0 (+/- 0.01), got {s}")
        return v

    # ---------- Helpers ----------

    def decayed_value(self, at: datetime | None = None) -> float:
        """Return decayed value at time `at` (default now).

        衰减公式: value * exp(-ln(2) * elapsed_days / half_life)
        half_life_days = None → 不衰减 (永久档).
        """
        if self.decay_half_life_days is None or self.decay_half_life_days <= 0:
            return self.value
        now = at or datetime.now(UTC)
        elapsed = (now - self.last_updated).total_seconds() / 86400
        if elapsed <= 0:
            return self.value
        factor = math.exp(-math.log(2) * elapsed / self.decay_half_life_days)
        return self.value * factor

    def is_stale(self, threshold_days: int = 30) -> bool:
        """True if this score hasn't been refreshed in `threshold_days`."""
        return (datetime.now(UTC) - self.last_updated) > timedelta(days=threshold_days)

    @classmethod
    def compose(
        cls,
        kind: ScoreKind,
        components: dict[str, float],
        weights: dict[str, float],
        *,
        sample_size: int = 0,
        half_life_days: int | None = None,
    ) -> ScoreDescriptor:
        """Build a composite score from weighted components."""
        if not weights:
            raise ValueError("weights required for compose()")
        missing = set(weights) - set(components)
        if missing:
            raise ValueError(f"components missing keys for weights: {missing}")
        value = sum(components[k] * weights[k] for k in weights)
        # clamp to [0,1] against float drift
        value = max(0.0, min(1.0, value))
        return cls(
            kind=kind,
            value=value,
            components=components,
            weights=weights,
            sample_size=sample_size,
            decay_half_life_days=half_life_days,
        )


def wilson_ci95(successes: int, trials: int) -> tuple[float, float] | None:
    """Wilson score 95% 置信区间 for 成功率.

    For capability card stats: success_rate_ci95 字段 (ADR-015).

    Returns (lo, hi) in [0,1], or None if trials = 0.
    """
    if trials <= 0:
        return None
    z = 1.96
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return max(0.0, center - half), min(1.0, center + half)
