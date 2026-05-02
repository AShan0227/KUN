"""Marginal ROI stop criterion (V2.2 §19.2 — KUN 决策核心 A).

边际收益递减检测: 对任意"可重复添加资源"的过程 P (拉记忆 / 多 judge / 搜索 /
多 agent 竞争 / idle-batch 子步骤), 设第 N 步的边际产出 ΔV(N) = V(N) - V(N-1).
若连续 K 步 ΔV < δ, 则强制停止, 即使预算允许.

用法:
    criterion = MarginalROIStopCriterion(delta_threshold=0.05, window_k=2)
    values = []
    for step in iterate():
        v = process(step)
        values.append(v)
        stop, reason = criterion.should_stop(values)
        if stop:
            break

设计原则:
- 不评估单步绝对值 (那是别的 decision 维度), 只评估"相对上一步的提升"
- 默认窗口 K=2 (连续 2 步无明显提升才停, 避免单点波动误停)
- δ 可配置 (默认 0.05 = 5% 提升), 不同模块可设不同阈值
- 不依赖外部 (纯算法), 配合任意 value 估算 fn 都能用
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StopDecision:
    """边际停止决策结果."""

    should_stop: bool
    reason: str  # "marginal_below_threshold" / "no_history" / "single_step" / "still_improving"
    last_marginal: float = 0.0
    avg_marginal: float = 0.0
    step_count: int = 0


@dataclass
class MarginalROIStopCriterion:
    """通用边际收益递减检测器.

    Args:
        delta_threshold: 单步边际提升阈值, < 这个就算 "无明显提升". 默认 0.05.
        window_k: 连续 K 步都无提升才停. 默认 2.
        min_steps: 至少跑这么多步才开始检查停止. 默认 2 (没法对 1 步做边际判断).
        absolute_floor: 绝对值下限, 若当前 V < 这个值, 即使有提升也停 (任务质量太差直接放弃). 默认 0.0 (不启用).
    """

    delta_threshold: float = 0.05
    window_k: int = 2
    min_steps: int = 2
    absolute_floor: float = 0.0

    def __post_init__(self) -> None:
        if self.window_k < 1:
            raise ValueError("window_k must be >= 1")
        if self.min_steps < 1:
            raise ValueError("min_steps must be >= 1")
        if not -1 <= self.delta_threshold <= 1:
            # 允许负值 — 用来表达"跌幅 > |delta| 算明显下降, 该停"
            raise ValueError("delta_threshold must be in [-1, 1]")

    def should_stop(self, value_history: list[float]) -> StopDecision:
        """根据 value 历史判定是否停止.

        Args:
            value_history: 每步累计/当前 value 序列. 长度 N 表示已跑 N 步.

        Returns:
            StopDecision 含 should_stop 和原因.
        """
        n = len(value_history)
        if n == 0:
            return StopDecision(should_stop=False, reason="no_history", step_count=0)
        if n < self.min_steps:
            return StopDecision(
                should_stop=False,
                reason="below_min_steps",
                step_count=n,
            )

        # 绝对底线: 当前值太低 → 停 (任务质量崩了)
        current = value_history[-1]
        if self.absolute_floor > 0 and current < self.absolute_floor:
            return StopDecision(
                should_stop=True,
                reason="below_absolute_floor",
                last_marginal=0.0,
                step_count=n,
            )

        # 计算最近 window_k 步的边际提升
        marginals = self._marginals(value_history, self.window_k)
        last_marginal = marginals[-1] if marginals else 0.0
        avg_marginal = sum(marginals) / len(marginals) if marginals else 0.0

        # 连续 K 步全部 < delta_threshold → 停
        if all(m < self.delta_threshold for m in marginals):
            return StopDecision(
                should_stop=True,
                reason="marginal_below_threshold",
                last_marginal=last_marginal,
                avg_marginal=avg_marginal,
                step_count=n,
            )

        return StopDecision(
            should_stop=False,
            reason="still_improving",
            last_marginal=last_marginal,
            avg_marginal=avg_marginal,
            step_count=n,
        )

    @staticmethod
    def _marginals(history: list[float], window_k: int) -> list[float]:
        """计算最近 window_k 步的边际提升 ΔV(N) = V(N) - V(N-1)."""
        if len(history) < 2:
            return []
        all_deltas = [history[i] - history[i - 1] for i in range(1, len(history))]
        return all_deltas[-window_k:]


@dataclass
class ValueEstimator:
    """通用 value 估算 helper. 调用方可以塞自定义 estimator, 或用内置.

    内置 strategies:
    - cumulative_quality: 累积质量分 (e.g. multi_judge 一致率)
    - distinct_information: 信息独立性 (检测 overlap, 跟前一步重复多 → value 低)
    - exponential_decay: 探索次数 N → value = 1 - exp(-N/scale), 趋近 1
    """

    strategy: str = "cumulative_quality"
    custom_fn: object = None  # callable[[Any, list], float]
    decay_scale: float = 5.0  # exponential_decay 用

    def estimate(self, current_item: object, prior_items: list[object]) -> float:
        """估算当前 item 的 value."""
        if self.custom_fn is not None:
            return float(self.custom_fn(current_item, prior_items))  # type: ignore[operator]

        if self.strategy == "cumulative_quality":
            # 调用方应把 quality 直接塞进 current_item (e.g. confidence 字段)
            quality = self._extract_quality(current_item)
            return quality

        if self.strategy == "distinct_information":
            # 跟 prior 重叠多 → value 低. 简化: 字符串包含/embedding 距离需调用方定制
            return self._distinct_score(current_item, prior_items)

        if self.strategy == "exponential_decay":
            import math

            n = len(prior_items) + 1
            return 1 - math.exp(-n / self.decay_scale)

        raise ValueError(f"unknown strategy: {self.strategy}")

    def _extract_quality(self, item: object) -> float:
        # 兼容 dict / pydantic / dataclass
        if isinstance(item, dict):
            v = item.get("confidence", item.get("quality", item.get("score", 0.5)))
            return float(v) if v is not None else 0.5
        for attr in ("confidence", "quality", "score"):
            if hasattr(item, attr):
                return float(getattr(item, attr))
        return 0.5

    def _distinct_score(self, current: object, prior: list[object]) -> float:
        # 简化: 检查 current str repr 跟 prior 是否完全重复
        if not prior:
            return 1.0
        cur_str = str(current)
        duplicates = sum(1 for p in prior if str(p) == cur_str)
        # 重复越多 value 越低
        return max(0.0, 1.0 - duplicates / len(prior))


# ============================================================================
# Module presets — 各 KUN 模块的推荐配置
# ============================================================================


@dataclass
class ModulePresets:
    """V2.2 §19.2 列的 6 类应用, 各自推荐配置."""

    @staticmethod
    def for_multi_judge() -> MarginalROIStopCriterion:
        """多 judge 评议: 一致率提升慢就停."""
        return MarginalROIStopCriterion(
            delta_threshold=0.03,  # 3% 一致率提升才算"明显"
            window_k=2,
            min_steps=2,  # 至少跑 2 个 judge
        )

    @staticmethod
    def for_memory_expand() -> MarginalROIStopCriterion:
        """拉记忆: 第 K 条对 LLM 输出影响估值."""
        return MarginalROIStopCriterion(
            delta_threshold=0.10,  # 记忆要带来 10% 影响才值得加
            window_k=1,  # 单步评估即可 (因为成本高)
            min_steps=1,
        )

    @staticmethod
    def for_idle_batch_step() -> MarginalROIStopCriterion:
        """idle-batch 子步: 任务质量提升慢就停."""
        return MarginalROIStopCriterion(
            delta_threshold=0.05,
            window_k=2,
            min_steps=3,  # idle-batch 至少跑前 3 步 (基础数据采集 + 验证 + 蒸馏)
        )

    @staticmethod
    def for_external_scan() -> MarginalROIStopCriterion:
        """外部信息扫描: 信息独立性下降就停 (overlap > 70%)."""
        return MarginalROIStopCriterion(
            delta_threshold=0.30,  # 新源信息独立性 < 30% 就停
            window_k=1,
            min_steps=1,
        )

    @staticmethod
    def for_multi_agent() -> MarginalROIStopCriterion:
        """多 agent 竞争方案: 多样性低就停."""
        return MarginalROIStopCriterion(
            delta_threshold=0.15,  # 方案多样性提升 15% 才值得多生成
            window_k=1,
            min_steps=2,  # 至少 2 个方案才能比
        )

    @staticmethod
    def for_search_pages() -> MarginalROIStopCriterion:
        """搜索翻页: 跟前几页 overlap 多就停."""
        return MarginalROIStopCriterion(
            delta_threshold=0.20,
            window_k=2,
            min_steps=1,
        )


__all__ = [
    "MarginalROIStopCriterion",
    "ModulePresets",
    "StopDecision",
    "ValueEstimator",
]
