"""V2.3 Wire 50 — Darwin Gödel 多轮探索 (V2.3 §4.3 + §10).

Darwin Gödel Machine (ICLR 2026) 启发: 不设限模型自我探索, 工程预算约束.
KUN 实装:
    - 启窗口内跑多轮 ensemble (每轮 5 path)
    - 每轮基于上轮结果调整 strategy (生成假设 → 实验 → 评估 → 下一假设)
    - 总预算 / 总时间 / 总轮数三个限制, 任一触发 → 停 + 总结

跟 V2.2 §26 EnsembleExecutor 关系:
    - EnsembleExecutor: 单次实验 (5 path)
    - DarwinGodelLoop: 多次 EnsembleExecutor 串起来, 每轮调 strategy

简化版: 不是真"LLM 生成假设", 是 strategy 配置的微调
(温度递增 / 路径数变化 / strategy 替换). 真生产可以接 LLM 生成 strategy.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DarwinRound:
    """单轮探索结果."""

    round_idx: int
    started_at: datetime
    finished_at: datetime
    strategy_config: dict[str, Any]  # 这轮用的 strategy 参数
    win_score: float  # 这轮最佳 path score
    cost_usd: float
    notes: str = ""


@dataclass
class DarwinExplorationResult:
    """整个 Darwin Gödel 探索的总结."""

    started_at: datetime
    finished_at: datetime
    rounds: list[DarwinRound] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_rounds: int = 0
    best_round_idx: int = -1
    best_strategy: dict[str, Any] = field(default_factory=dict)
    best_score: float = 0.0
    stopped_reason: str = ""  # "budget_exhausted" / "time_exhausted" / "rounds_max" / "converged"


# 单轮 runner 接口: prompt + strategy_config → (winning_score, cost_usd)
RoundRunner = Callable[[str, dict[str, Any]], Awaitable[tuple[float, float]]]


class DarwinGodelLoop:
    """启窗口内跑 Darwin Gödel 多轮探索.

    用法 (在启窗口内):
        loop = DarwinGodelLoop(
            round_runner=my_ensemble_round_fn,
            max_rounds=10,
            total_budget_usd=2.0,
            total_time_sec=300,
        )
        result = await loop.explore("biz_plan task")
        # result.best_strategy → 推荐给主仓库 protocol

    round_runner: 单轮跑实验 fn — 接 (prompt, strategy_config) → (score, cost).
    可以是 EnsembleExecutor 的 wrapper.
    """

    def __init__(
        self,
        round_runner: RoundRunner,
        *,
        max_rounds: int = 10,
        total_budget_usd: float = 2.0,
        total_time_sec: float = 300.0,
        convergence_threshold: float = 0.05,
        # strategy 进化策略 — 默认温度递减 + path count 探索
        strategy_evolver: Callable[[int, list[DarwinRound]], dict[str, Any]] | None = None,
    ) -> None:
        self._round_runner = round_runner
        self._max_rounds = max_rounds
        self._total_budget_usd = total_budget_usd
        self._total_time_sec = total_time_sec
        self._convergence_threshold = convergence_threshold
        self._strategy_evolver = strategy_evolver or _default_strategy_evolver

    async def explore(self, prompt: str) -> DarwinExplorationResult:
        """跑多轮探索."""
        result = DarwinExplorationResult(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        t_start = time.perf_counter()

        for round_idx in range(self._max_rounds):
            # 1. 检查 stop 条件
            elapsed = time.perf_counter() - t_start
            if elapsed > self._total_time_sec:
                result.stopped_reason = "time_exhausted"
                break
            if result.total_cost_usd > self._total_budget_usd:
                result.stopped_reason = "budget_exhausted"
                break
            # converged: 最近 3 轮 score 变化 < threshold
            if len(result.rounds) >= 3:
                last_3 = [r.win_score for r in result.rounds[-3:]]
                if max(last_3) - min(last_3) < self._convergence_threshold:
                    result.stopped_reason = "converged"
                    break

            # 2. 进化 strategy (基于 prior rounds)
            try:
                strategy = self._strategy_evolver(round_idx, result.rounds)
            except Exception:
                logger.exception("strategy_evolver failed at round %d", round_idx)
                strategy = {}

            # 3. 跑这一轮
            round_t = datetime.now(UTC)
            try:
                score, cost = await self._round_runner(prompt, strategy)
            except Exception as e:
                logger.exception("round %d runner failed", round_idx)
                score, cost = 0.0, 0.0
                logger.debug("round failure: %s", e)

            round_result = DarwinRound(
                round_idx=round_idx,
                started_at=round_t,
                finished_at=datetime.now(UTC),
                strategy_config=strategy,
                win_score=score,
                cost_usd=cost,
            )
            result.rounds.append(round_result)
            result.total_cost_usd += cost
            result.total_rounds += 1

            if score > result.best_score:
                result.best_score = score
                result.best_round_idx = round_idx
                result.best_strategy = strategy

            logger.info(
                "darwin_round=%d strategy=%s score=%.3f cost=%.4f",
                round_idx,
                strategy,
                score,
                cost,
            )
        else:
            result.stopped_reason = "rounds_max"

        result.finished_at = datetime.now(UTC)
        logger.info(
            "darwin.explore done rounds=%d best_score=%.3f total_cost=%.4f reason=%s",
            result.total_rounds,
            result.best_score,
            result.total_cost_usd,
            result.stopped_reason,
        )
        return result


def _default_strategy_evolver(round_idx: int, prior_rounds: list[DarwinRound]) -> dict[str, Any]:
    """简单 strategy 进化 — 跟蚁群类似, 多探索高分 strategy 附近.

    Round 0: tier_top + temp 0.1 (保守)
    Round 1: tier_strong + temp 0.5 (中性)
    Round 2: tier_top + temp 0.7 (探索高 temp)
    Round 3+: 基于 prior best, 微调 temp ±0.1
    """
    presets = [
        {"strategy": "tier_top_low_temp", "temperature": 0.1, "n_paths": 3},
        {"strategy": "tier_strong_mid_temp", "temperature": 0.5, "n_paths": 3},
        {"strategy": "chain_of_thought", "temperature": 0.7, "n_paths": 3},
    ]
    if round_idx < len(presets):
        return presets[round_idx]

    # round 3+ 基于 best 微调
    if not prior_rounds:
        return presets[0]
    best = max(prior_rounds, key=lambda r: r.win_score)
    config = dict(best.strategy_config)
    # 微调 temperature ±0.1 (随轮次交替)
    delta = 0.1 if round_idx % 2 == 0 else -0.1
    config["temperature"] = max(0.0, min(1.0, float(config.get("temperature", 0.5)) + delta))
    config["round_basis"] = best.round_idx
    return config


__all__ = [
    "DarwinExplorationResult",
    "DarwinGodelLoop",
    "DarwinRound",
    "RoundRunner",
]
