"""V2.3 启 (Qi) cron jobs — 串起 Darwin / AI Scientist / PredictionTrainer.

启窗口打开时自动跑探索, 沉淀 protocol. 这是 V2.3 真闭环的最后一环 — 没有这些
cron, 启窗口打开了也"什么都不做", protocol 永远 0.

设计:
- 每个 cron job 跑前 require_qi_active 守门. 窗口外 → skip + log.
- 默认 cron expr 都在启窗口内 (1-5 AM). 用户可改 SoulFile.qi_window 调.
- 都是非阻塞: 每次 tick 拿 budget 看, 没钱 → skip.
- 错误不传染其他 job (cron_scheduler._safe_run 已包).
"""

from __future__ import annotations

import os
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.qi.cron")


async def _qi_predictive_coding_train(app: Any, tenant_id: str) -> None:
    """启窗口里调 PredictionTrainer.train() → 输出 model file.

    跑前: 守门窗口 + budget. 跑后: model 写到 KUN_PC_MODEL_PATH (如有), 鲲下次启动 load.
    """
    if not _check_qi_active(app):
        log.debug("qi_pc_train.skipped reason=window_inactive")
        return
    try:
        from kun.qi.predictive_coding import (
            PredictionTrainer,
            get_prediction_log,
            save_model,
        )

        log_singleton = get_prediction_log()
        trainer = PredictionTrainer(log_singleton)
        model = await trainer.train()
        model_path = os.getenv("KUN_PC_MODEL_PATH")
        if model_path:
            save_model(model, model_path)
            log.info(
                "qi_pc_train.saved",
                path=model_path,
                sample_size=model.sample_size,
                tenant=tenant_id,
            )
        else:
            log.info(
                "qi_pc_train.done_no_save",
                sample_size=model.sample_size,
                version=model.version,
                tenant=tenant_id,
            )
    except Exception:
        log.exception("qi_pc_train.failed (non-fatal)")


async def _qi_darwin_godel_explore(app: Any, tenant_id: str) -> None:
    """启窗口里跑 Darwin Gödel 多轮探索. 当前 stub: 不真跑 LLM, 只 log.

    真跑需要:
      1. 拉一些 task / prompt 作探索目标
      2. 拿 LLM router 提供 round_runner
      3. budget 守门 (启日预算上限)
      4. 探索完 → 涌现 experimental protocol → registry.save
    这一步留 V2.4 真接 LLM (现在没真 LLM key, 跑 mock 没意义).
    """
    if not _check_qi_active(app):
        log.debug("qi_darwin.skipped reason=window_inactive")
        return
    try:
        from kun.qi import get_qi_budget

        budget = get_qi_budget()
        spent = budget.get_today_spent(tenant_id)
        if spent >= budget._daily_limit:
            log.info("qi_darwin.budget_exhausted", spent=spent)
            return
        log.info(
            "qi_darwin.placeholder_run",
            tenant=tenant_id,
            note="V2.4: 真接 LLM router 跑 explore",
        )
    except Exception:
        log.exception("qi_darwin.failed (non-fatal)")


async def _qi_ai_scientist_explore(app: Any, tenant_id: str) -> None:
    """启窗口里跑 AIScientistTreeSearch. Stub 同 Darwin (V2.4 真接)."""
    if not _check_qi_active(app):
        log.debug("qi_ai_scientist.skipped reason=window_inactive")
        return
    try:
        from kun.qi import get_qi_budget

        budget = get_qi_budget()
        spent = budget.get_today_spent(tenant_id)
        if spent >= budget._daily_limit:
            log.info("qi_ai_scientist.budget_exhausted", spent=spent)
            return
        log.info(
            "qi_ai_scientist.placeholder_run",
            tenant=tenant_id,
            note="V2.4: 真接 LLM router 跑 tree search",
        )
    except Exception:
        log.exception("qi_ai_scientist.failed (non-fatal)")


def _check_qi_active(app: Any) -> bool:
    """守门: 启窗口活跃 + KUN_QI_ENABLED=1 才跑."""
    if os.getenv("KUN_QI_ENABLED", "0") != "1":
        return False
    if os.getenv("KUN_QI_FORCE_DISABLE") == "1":
        return False
    if os.getenv("KUN_QI_FORCE_ACTIVE") == "1":
        return True
    qi_window = getattr(app.state, "qi_window_config", None)
    if qi_window is None:
        return False
    try:
        from kun.qi.window import is_qi_window_active

        return is_qi_window_active(qi_window)
    except Exception:
        return False


def register_qi_cron_jobs(sched: Any, app: Any, tenant_id: str) -> None:
    """注册 3 个启 cron job.

    cron 表达式: 默认每小时跑一次, 内部用 _check_qi_active 守门窗口外 skip.
    优势: 用户改 SoulFile.qi_window 时, cron 自动尊重新窗口 (不需重启).
    """

    async def _pc_job() -> None:
        await _qi_predictive_coding_train(app, tenant_id)

    async def _darwin_job() -> None:
        await _qi_darwin_godel_explore(app, tenant_id)

    async def _ai_scientist_job() -> None:
        await _qi_ai_scientist_explore(app, tenant_id)

    # 每小时 tick 一次, 内部守门
    sched.register("qi_pc_train_hourly", "@hourly", _pc_job)
    sched.register("qi_darwin_explore_hourly", "@hourly", _darwin_job)
    sched.register("qi_ai_scientist_hourly", "@hourly", _ai_scientist_job)


__all__ = ["register_qi_cron_jobs"]
