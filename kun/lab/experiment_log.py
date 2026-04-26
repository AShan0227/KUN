"""ExperimentLog — KUN-Lab 实验记录 (V2.2 §26).

记录所有 lab 实验的 (config, output, score, cost) → 后续 RecipePromoter 分析
"哪种 ensemble 配方有效" → 推主仓库 KnowledgePrecipitation.

设计:
- 默认 in-memory store (单进程实验), M5 接 SQLAlchemy
- 实验按 task_type 分组聚合
- 提供 query: best_recipe_for(task_type), top_winning_strategies(), etc.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from kun.lab.ensemble_executor import EnsembleResult

logger = logging.getLogger(__name__)


class Experiment(BaseModel):
    """单次实验记录."""

    experiment_id: str
    task_type: str  # 用户任务分类 (跟主仓库 task_type 同 taxonomy)
    prompt_hash: str = ""  # 任务 prompt 的 hash (隐私: 不存原文)
    ensemble_result: EnsembleResult
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    user_judgement: str = ""  # 可选: 用户后续判断 (good/bad/neutral)
    notes: str = ""


class RecipeStats(BaseModel):
    """某 task_type + strategy 的胜率统计."""

    task_type: str
    strategy: str
    win_count: int = 0
    total_count: int = 0
    avg_score: float = 0.0
    avg_cost_usd: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.win_count / self.total_count if self.total_count > 0 else 0.0


class ExperimentLog:
    """KUN-Lab 实验日志 (M5 接 DB)."""

    def __init__(self) -> None:
        self._experiments: list[Experiment] = []

    def record(
        self,
        task_type: str,
        ensemble_result: EnsembleResult,
        prompt_hash: str = "",
        notes: str = "",
    ) -> Experiment:
        exp = Experiment(
            experiment_id=ensemble_result.experiment_id,
            task_type=task_type,
            prompt_hash=prompt_hash,
            ensemble_result=ensemble_result,
            notes=notes,
        )
        self._experiments.append(exp)
        return exp

    def list_all(self) -> list[Experiment]:
        return list(self._experiments)

    def by_task_type(self, task_type: str) -> list[Experiment]:
        return [e for e in self._experiments if e.task_type == task_type]

    def best_recipe_for(self, task_type: str) -> RecipeStats | None:
        """该 task_type 上胜率最高的 strategy."""
        stats = self.recipe_stats(task_type)
        if not stats:
            return None
        return max(stats, key=lambda s: s.win_rate)

    def recipe_stats(self, task_type: str | None = None) -> list[RecipeStats]:
        """按 (task_type, strategy) 聚合胜率."""
        relevant = self.by_task_type(task_type) if task_type is not None else self._experiments
        # (task_type, strategy) → (win_count, total_count, score_sum, cost_sum)
        agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0.0])

        for exp in relevant:
            er = exp.ensemble_result
            winner_idx = er.winning_path_idx
            for pr in er.path_results:
                if pr.error:
                    continue
                key = (exp.task_type, str(pr.config.get("strategy", "unknown")))
                agg[key][1] += 1  # total_count
                agg[key][2] += pr.score
                agg[key][3] += pr.cost_usd
                if pr.path_idx == winner_idx:
                    agg[key][0] += 1  # win_count

        results: list[RecipeStats] = []
        for (tt, strat), (wins, total, score_sum, cost_sum) in agg.items():
            if total == 0:
                continue
            results.append(
                RecipeStats(
                    task_type=tt,
                    strategy=strat,
                    win_count=int(wins),
                    total_count=int(total),
                    avg_score=score_sum / total,
                    avg_cost_usd=cost_sum / total,
                )
            )
        return results

    def top_winning_strategies(self, top_k: int = 5) -> list[tuple[str, float]]:
        """全局 top N 胜出 strategy. 返 [(strategy, win_rate), ...]."""
        strategy_wins: Counter[str] = Counter()
        strategy_totals: Counter[str] = Counter()
        for exp in self._experiments:
            er = exp.ensemble_result
            winner_idx = er.winning_path_idx
            for pr in er.path_results:
                if pr.error:
                    continue
                strat = str(pr.config.get("strategy", "unknown"))
                strategy_totals[strat] += 1
                if pr.path_idx == winner_idx:
                    strategy_wins[strat] += 1

        rates: list[tuple[str, float]] = []
        for strat, total in strategy_totals.items():
            if total > 0:
                rates.append((strat, strategy_wins[strat] / total))
        rates.sort(key=lambda x: x[1], reverse=True)
        return rates[:top_k]

    def total_lab_cost_usd(self) -> float:
        return sum(e.ensemble_result.total_cost_usd for e in self._experiments)

    def reset(self) -> None:
        self._experiments.clear()


_log: ExperimentLog | None = None


def get_experiment_log() -> ExperimentLog:
    global _log
    if _log is None:
        _log = ExperimentLog()
    return _log


def reset_experiment_log() -> None:
    global _log
    _log = None


def _summarize_for_event(stats: list[RecipeStats]) -> dict[str, Any]:
    """给后续 RecipePromoter 推主仓库用的 dump."""
    return {
        "summary_at": datetime.now(UTC).isoformat(),
        "recipes": [
            {
                "task_type": s.task_type,
                "strategy": s.strategy,
                "win_rate": s.win_rate,
                "total_count": s.total_count,
                "avg_score": s.avg_score,
                "avg_cost_usd": s.avg_cost_usd,
            }
            for s in stats
        ],
    }


__all__ = [
    "Experiment",
    "ExperimentLog",
    "RecipeStats",
    "_summarize_for_event",
    "get_experiment_log",
    "reset_experiment_log",
]
