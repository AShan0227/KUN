"""RecipePromoter — 把 KUN-Lab 实验有效配方推主仓库 (V2.2 §26).

实验跑一段时间后, ExperimentLog 累计 N 个 (task_type, strategy, win_rate)
统计. RecipePromoter:
1. 周期 (默认 weekly) 跑 promote_eligible_recipes
2. 找符合条件的: total_count ≥ min_total + win_rate ≥ min_winrate
3. 通过 KnowledgePrecipitation 推主仓库 (走 §16.6 GuardPolicy 审计回滚)

主仓库消费: 收到 recipe → 改 ExecutionMode classifier 默认 mode 选择 / 改
hermes prompt template / 改 ImportanceScorer 权重 / etc.

设计原则:
- 主仓库不直接信 lab 推过来的, 走 §16.6 GuardPolicy: 影子 → canary → stable
- 推完不能直接生效, 必须主仓库 idle_batch 消费 + 验证后才生效
- 不影响生产 KUN 当前行为 (默认 off)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from kun.lab.experiment_log import ExperimentLog, RecipeStats

logger = logging.getLogger(__name__)


class RecipePromotion(BaseModel):
    """一次推送给主仓库的 recipe."""

    promotion_id: str
    task_type: str
    strategy: str
    win_rate: float
    total_count: int
    avg_score: float
    avg_cost_usd: float
    promoted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    target_module: str = ""  # e.g. "execution_mode_classifier" / "hermes_prompt"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecipePromoter:
    """从 ExperimentLog 推 recipe 到主仓库.

    Args:
        experiment_log: ExperimentLog 实例 (来自 get_experiment_log())
        min_total: 该 (task_type, strategy) 至少跑过 N 次才考虑 promote (默认 10)
        min_winrate: 至少 win_rate 多少才推 (默认 0.6)
        precipitation_dispatcher: 可选 callable. 给了的话推送到 KnowledgePrecipitation.
                                  None → 只返 promotion list 不推送 (测试用).
    """

    def __init__(
        self,
        experiment_log: ExperimentLog,
        *,
        min_total: int = 10,
        min_winrate: float = 0.6,
        precipitation_dispatcher: Any = None,
        event_emitter: Any = None,
        rollback_emitter: Any = None,
    ) -> None:
        """
        Args:
            event_emitter: 可选 async fn(RecipePromotion) — 每次推升 emit
                experiment.promoted (Wire 21). None → 不 emit.
            rollback_emitter: 可选 async fn(RecipePromotion, *, reason, error) —
                dispatcher 失败时 emit experiment.rolled_back. None → 不 emit.
        """
        if not 0 <= min_winrate <= 1:
            raise ValueError("min_winrate must be in [0, 1]")
        self._log = experiment_log
        self.min_total = min_total
        self.min_winrate = min_winrate
        self._dispatcher = precipitation_dispatcher
        self._event_emitter = event_emitter
        self._rollback_emitter = rollback_emitter
        self._promotions_history: list[RecipePromotion] = []

    def find_eligible_recipes(self) -> list[RecipeStats]:
        """找出满足 min_total + min_winrate 的所有 stats."""
        all_stats = self._log.recipe_stats(task_type=None)
        return [
            s
            for s in all_stats
            if s.total_count >= self.min_total and s.win_rate >= self.min_winrate
        ]

    async def promote_eligible(self, target_module: str = "") -> list[RecipePromotion]:
        """跑一遍 promotion. 返这次推的 list."""
        from kun.core.ids import new_id

        eligible = self.find_eligible_recipes()
        promotions: list[RecipePromotion] = []
        for stats in eligible:
            # 去重: 同 (task_type, strategy) 一周内只推一次
            if self._already_promoted_recently(stats):
                continue
            p = RecipePromotion(
                promotion_id=new_id("experiment"),
                task_type=stats.task_type,
                strategy=stats.strategy,
                win_rate=stats.win_rate,
                total_count=stats.total_count,
                avg_score=stats.avg_score,
                avg_cost_usd=stats.avg_cost_usd,
                target_module=target_module or self._infer_target_module(stats.strategy),
            )
            promotions.append(p)
            self._promotions_history.append(p)

            # Wire 28: Prometheus metric (best-effort)
            try:
                from kun.core.metrics import lab_promotion_total

                lab_promotion_total.labels(
                    task_type=p.task_type,
                    target_module=p.target_module or "general",
                ).inc()
            except Exception as exc:
                logger.debug("lab.promotion.metric_skipped err=%s", exc)

            # 推主仓库 (KnowledgePrecipitation)
            dispatch_error = ""
            if self._dispatcher is not None:
                try:
                    await self._dispatcher(p)
                except Exception as exc:
                    logger.exception("recipe_promoter.dispatcher failed for %s", p.promotion_id)
                    dispatch_error = f"{type(exc).__name__}: {exc}"

            # Wire 21: emit experiment.promoted (best-effort)
            if self._event_emitter is not None:
                try:
                    await self._event_emitter(p)
                except Exception:
                    logger.exception(
                        "recipe_promoter.event_emitter failed for %s", p.promotion_id
                    )

            # Wire 21: dispatcher 失败 → emit experiment.rolled_back
            if dispatch_error and self._rollback_emitter is not None:
                try:
                    await self._rollback_emitter(
                        p, reason="dispatcher_failed", error=dispatch_error
                    )
                except Exception:
                    logger.exception(
                        "recipe_promoter.rollback_emitter failed for %s", p.promotion_id
                    )
        return promotions

    def _already_promoted_recently(self, stats: RecipeStats, window_hours: int = 168) -> bool:
        """避免重复推 — 同 (task_type, strategy) 一周内只推一次."""
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        for p in self._promotions_history:
            if (
                p.task_type == stats.task_type
                and p.strategy == stats.strategy
                and p.promoted_at >= cutoff
            ):
                return True
        return False

    @staticmethod
    def _infer_target_module(strategy: str) -> str:
        """根据 strategy 名字推哪个主仓库模块该消费."""
        if "tier_" in strategy:
            return "execution_mode_classifier"
        if "chain_of_thought" in strategy or "diverse" in strategy:
            return "hermes_prompt_template"
        return "general"

    def get_promotions_history(self) -> list[RecipePromotion]:
        return list(self._promotions_history)

    def reset_history(self) -> None:
        self._promotions_history.clear()


__all__ = [
    "RecipePromoter",
    "RecipePromotion",
]
