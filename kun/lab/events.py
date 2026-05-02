"""LabEventEmitter — 把 KUN-Lab 实验结果 emit 进 events bus (Wire 21).

闭环关键: lab 跑完不能只在 in-memory ExperimentLog. 要把 (ensemble 完成 / recipe
推升 / 推送回滚) emit 成事件流, 让主仓库:
    - watchtower 看 lab 健康度 (fallback rate / cost spike)
    - idle_batch 消费 experiment.promoted → 真改主仓库参数
    - Grafana 可视化 lab 跑了多少 / 推了多少 recipe

Best-effort 设计 (跟 router._emit_fallback_event 同模式):
    - 没 tenant context → 静默跳过 (单元测试场景)
    - 没 DB session → 静默跳过 (lab 在 in-memory only 跑也不爆)
    - 任何 emit 异常 → 只 log, 不向上传 (lab 主流程不能被 observability 阻断)

事件 schema:
    experiment.created  payload={
        experiment_id, task_type, n_paths, total_cost_usd, winning_strategy,
        winning_score, selection_method, selection_reason, path_count_success
    }
    experiment.promoted payload={
        promotion_id, task_type, strategy, win_rate, total_count,
        avg_score, avg_cost_usd, target_module
    }
    experiment.rolled_back payload={
        promotion_id, reason, error
    }
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kun.lab.ensemble_executor import EnsembleResult
    from kun.lab.recipe_promoter import RecipePromotion

logger = logging.getLogger(__name__)


async def _emit_safe(event_type: str, payload: dict[str, Any]) -> bool:
    """Best-effort emit. 返 True 真发出, False 静默跳过.

    任何异常都吞掉 (lab 主流程不能死在 observability 上).
    """
    try:
        from kun.core.db import session_scope
        from kun.core.events import emit
        from kun.core.tenancy import current_tenant
        from kun.datamodel.events import Event

        tenant = current_tenant()
    except Exception as e:
        logger.debug("lab.event_skipped_no_tenant", extra={"event_type": event_type, "err": str(e)})
        return False

    try:
        async with session_scope() as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type=event_type,  # type: ignore[arg-type]
                    payload=payload,
                ),
            )
        return True
    except Exception as e:
        logger.debug("lab.event_emit_failed", extra={"event_type": event_type, "err": str(e)})
        return False


def summarize_ensemble(result: EnsembleResult, *, task_type: str) -> dict[str, Any]:
    """把 EnsembleResult 拆成 event payload (不存原始 prompt / output, 隐私安全)."""
    success_paths = [pr for pr in result.path_results if not pr.error]
    winner = (
        result.path_results[result.winning_path_idx]
        if 0 <= result.winning_path_idx < len(result.path_results)
        else None
    )
    return {
        "experiment_id": result.experiment_id,
        "task_type": task_type,
        "n_paths": len(result.path_results),
        "path_count_success": len(success_paths),
        "total_cost_usd": result.total_cost_usd,
        "total_latency_sec": result.total_latency_sec,
        "winning_path_idx": result.winning_path_idx,
        "winning_strategy": str(winner.config.get("strategy", "")) if winner else "",
        "winning_score": winner.score if winner else 0.0,
        "selection_method": result.config.selection_method,
        "selection_reason": result.selection_reason,
    }


def summarize_promotion(promo: RecipePromotion) -> dict[str, Any]:
    return {
        "promotion_id": promo.promotion_id,
        "task_type": promo.task_type,
        "strategy": promo.strategy,
        "win_rate": promo.win_rate,
        "total_count": promo.total_count,
        "avg_score": promo.avg_score,
        "avg_cost_usd": promo.avg_cost_usd,
        "target_module": promo.target_module,
        "promoted_at": promo.promoted_at.isoformat(),
    }


class LabEventEmitter:
    """callable wrapper. 装进 EnsembleExecutor / RecipePromoter 注入点.

    用法 (生产):
        emitter = LabEventEmitter()
        executor = EnsembleExecutor(invoker, event_emitter=emitter.on_experiment_completed)
        promoter = RecipePromoter(log, event_emitter=emitter.on_recipe_promoted)

    用法 (测试):
        captured = []
        executor = EnsembleExecutor(invoker, event_emitter=lambda r: captured.append(r))

    Args:
        task_type_default: 给 ensemble 事件用的 task_type fallback (executor 调用时
                           没传 task_type 参数时取这个)
    """

    def __init__(self, *, task_type_default: str = "kun_lab.unspecified") -> None:
        self.task_type_default = task_type_default

    async def on_experiment_completed(
        self,
        result: EnsembleResult,
        *,
        task_type: str | None = None,
    ) -> bool:
        payload = summarize_ensemble(result, task_type=task_type or self.task_type_default)
        return await _emit_safe("experiment.created", payload)

    async def on_recipe_promoted(self, promo: RecipePromotion) -> bool:
        payload = summarize_promotion(promo)
        return await _emit_safe("experiment.promoted", payload)

    async def on_recipe_rolled_back(
        self,
        promo: RecipePromotion,
        *,
        reason: str = "",
        error: str = "",
    ) -> bool:
        payload = {
            "promotion_id": promo.promotion_id,
            "task_type": promo.task_type,
            "strategy": promo.strategy,
            "reason": reason,
            "error": error,
        }
        return await _emit_safe("experiment.rolled_back", payload)


__all__ = [
    "LabEventEmitter",
    "summarize_ensemble",
    "summarize_promotion",
]
