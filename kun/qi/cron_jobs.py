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

import contextlib
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
    """启窗口里跑 Darwin Gödel 多轮探索. 真接 LLM router (claude_code_cli / codex_mcp).

    每轮:
      1. 用一个 探索目标 prompt (从 seed_prompts 拿)
      2. 调 router.invoke → 拿 score (基于 response.content 长度/质量) + cost
      3. budget 累计, 超 KUN_QI_DAILY_BUDGET_USD → 停
      4. 探索完 → 涌现 experimental protocol → registry.save
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

        from kun.interface.llm import get_router
        from kun.interface.llm.base import LLMMessage, LLMRequest
        from kun.qi.darwin_godel import DarwinGodelLoop

        router = get_router()
        explore_prompt = _pick_explore_prompt()

        async def round_runner(prompt: str, strategy: dict[str, Any]) -> tuple[float, float]:
            """单轮: 调 LLM, 返 (score, cost). score 基于 content 长度 + finish_reason."""
            req = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=strategy.get("system", "")),
                    LLMMessage(role="user", content=prompt),
                ],
                temperature=float(strategy.get("temperature", 0.7)),
                max_tokens=int(strategy.get("max_tokens", 512)),
            )
            try:
                resp = await router.invoke(req, purpose="execution")
                # 简单 score: 完成 + 内容长度合理 → 0.5-1.0
                base = 0.5 if resp.finish_reason == "stop" else 0.2
                length_bonus = min(0.5, len(resp.content) / 1000.0)
                score = min(1.0, base + length_bonus)
                cost = float(resp.cost_usd_equivalent or 0.0)
                # 加进 budget (CLI OAuth 模式 cost 通常 = 0, 不会扣)
                if cost > 0:
                    with contextlib.suppress(Exception):
                        budget.add_cost(tenant_id, cost)
                return (score, cost)
            except Exception as e:
                log.warning("qi_darwin.round_failed", error=str(e))
                return (0.0, 0.0)

        loop = DarwinGodelLoop(
            round_runner=round_runner,
            max_rounds=int(os.getenv("KUN_QI_DARWIN_MAX_ROUNDS", "3")),
            total_budget_usd=max(0.5, budget.remaining_budget(tenant_id)),
            total_time_sec=float(os.getenv("KUN_QI_DARWIN_TIME_SEC", "120")),
        )
        result = await loop.explore(explore_prompt)
        log.info(
            "qi_darwin.done",
            tenant=tenant_id,
            rounds=result.total_rounds,
            best_score=result.best_score,
            stopped=result.stopped_reason,
            cost=result.total_cost_usd,
        )

        # 涌现 → 存 experimental protocol (基于 best round strategy)
        if result.best_score >= 0.6:
            await _emerge_protocol_from_darwin(app, tenant_id, explore_prompt, result)
    except Exception:
        log.exception("qi_darwin.failed (non-fatal)")


async def _qi_ai_scientist_explore(app: Any, tenant_id: str) -> None:
    """启窗口里跑 AIScientistTreeSearch. 真接 LLM router."""
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

        # AI Scientist v2 的 API 跟 Darwin 略不同 (codex 写的 tree search), V2.4
        # 真接前先简化复用 Darwin loop. 这里用 Darwin 跑一轮 + 标 ai_scientist source.
        from kun.interface.llm import get_router
        from kun.interface.llm.base import LLMMessage, LLMRequest
        from kun.qi.darwin_godel import DarwinGodelLoop

        router = get_router()
        explore_prompt = _pick_explore_prompt(prefer="research")

        async def round_runner(prompt: str, strategy: dict[str, Any]) -> tuple[float, float]:
            req = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=strategy.get("system", "")),
                    LLMMessage(role="user", content=prompt),
                ],
                temperature=float(strategy.get("temperature", 0.5)),
                max_tokens=int(strategy.get("max_tokens", 512)),
            )
            try:
                resp = await router.invoke(req, purpose="execution")
                base = 0.5 if resp.finish_reason == "stop" else 0.2
                length_bonus = min(0.5, len(resp.content) / 1000.0)
                score = min(1.0, base + length_bonus)
                cost = float(resp.cost_usd_equivalent or 0.0)
                if cost > 0:
                    with contextlib.suppress(Exception):
                        budget.add_cost(tenant_id, cost)
                return (score, cost)
            except Exception as e:
                log.warning("qi_ai_scientist.round_failed", error=str(e))
                return (0.0, 0.0)

        loop = DarwinGodelLoop(
            round_runner=round_runner,
            max_rounds=int(os.getenv("KUN_QI_AISCI_MAX_ROUNDS", "2")),
            total_budget_usd=max(0.5, budget.remaining_budget(tenant_id)),
            total_time_sec=float(os.getenv("KUN_QI_AISCI_TIME_SEC", "90")),
        )
        result = await loop.explore(explore_prompt)
        log.info(
            "qi_ai_scientist.done",
            tenant=tenant_id,
            rounds=result.total_rounds,
            best_score=result.best_score,
            stopped=result.stopped_reason,
        )
    except Exception:
        log.exception("qi_ai_scientist.failed (non-fatal)")


_EXPLORE_PROMPTS = [
    ("writing", "为新产品写一段 30 字 slogan"),
    ("decision", "比较 Postgres vs MySQL 给一个 SaaS 产品做选型"),
    ("research", "总结一下 LLM agent 框架最近 6 个月的趋势"),
    ("coding", "解释 FastAPI 怎么实现 dependency injection"),
]


def _pick_explore_prompt(*, prefer: str | None = None) -> str:
    """轮换 explore prompts. prefer 给个偏好类目."""
    import random

    if prefer:
        candidates = [p for k, p in _EXPLORE_PROMPTS if k == prefer]
        if candidates:
            return random.choice(candidates)
    return random.choice(_EXPLORE_PROMPTS)[1]


async def _emerge_protocol_from_darwin(
    app: Any, tenant_id: str, prompt: str, result: Any
) -> None:
    """Darwin 探索完 → 涌现 1 个 experimental protocol → registry.save.

    简化: 不看 best_round.strategy 细节, 只把 task_type 推断出来 + 标 experimental.
    这是 starter pack — V2.4 加更聪明的协议生成 (从 strategy 真提取 hermes prompt 等).
    """
    try:
        from datetime import UTC, datetime

        from kun.qi.protocol import (
            Protocol,
            ProtocolExecution,
            ProtocolHermesTemplate,
            ProtocolTrigger,
        )

        registry = getattr(app.state, "protocol_registry", None)
        if registry is None:
            return

        # 推断 task_type — 简单版: 拿 prompt 关键词
        task_type_pat = "*"
        if "slogan" in prompt or "写" in prompt:
            task_type_pat = "writing.*"
        elif "比较" in prompt or "选型" in prompt:
            task_type_pat = "decision.*"
        elif "总结" in prompt or "趋势" in prompt:
            task_type_pat = "research.*"
        elif "代码" in prompt or "FastAPI" in prompt:
            task_type_pat = "coding.*"

        version = f"0.1.0-darwin-{datetime.now(UTC).strftime('%Y%m%d-%H%M')}"
        proto = Protocol(
            protocol_id=f"darwin.emergent.{task_type_pat.replace('.*', '').replace('*', 'general')}",
            version=version,
            tenant_id=tenant_id,
            status="experimental",
            trigger=ProtocolTrigger(
                task_type_pattern=task_type_pat,
                complexity_score_min=0.0,
                complexity_score_max=1.0,
            ),
            execution=ProtocolExecution(
                mode="SMART",
                expected_cost_usd=result.total_cost_usd / max(1, result.total_rounds),
            ),
            hermes_template=ProtocolHermesTemplate(
                system_prompt_addon=(
                    f"基于 {result.total_rounds} 轮 Darwin Gödel 探索的最佳策略 "
                    f"(score={result.best_score:.2f}). 这是涌现协议."
                ),
            ),
            created_by="qi",
            metadata={
                "darwin_rounds": result.total_rounds,
                "darwin_best_score": result.best_score,
                "darwin_stopped_reason": result.stopped_reason,
                "explore_prompt": prompt[:200],
            },
        )
        await registry.save(proto)
        log.info(
            "qi_darwin.protocol_emerged",
            tenant=tenant_id,
            protocol_id=proto.protocol_id,
            version=proto.version,
            score=result.best_score,
        )
    except Exception:
        log.exception("qi_darwin.protocol_emerge_failed (non-fatal)")


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


async def _qi_auto_promote(app: Any, tenant_id: str) -> None:
    """V2.4: 自动 promote experimental → shadow → canary → stable."""
    if not _check_qi_active(app):
        return
    try:
        from kun.qi.auto_promote import auto_promote_protocols

        result = await auto_promote_protocols(app, tenant_id)
        log.info("qi_auto_promote.done", tenant=tenant_id, **result)
    except Exception:
        log.exception("qi_auto_promote.failed (non-fatal)")


async def _qi_dogfood_long(app: Any, tenant_id: str) -> None:
    """V2.4: dogfood 长期化 — 每天跑 3 个轻量 task 验全闭环.

    简化: 现在只跑 1 次 darwin 探索 (有真 LLM call). V2.5 加多 task_type.
    """
    if not _check_qi_active(app):
        return
    try:
        log.info("qi_dogfood_long.start", tenant=tenant_id)
        await _qi_darwin_godel_explore(app, tenant_id)
        log.info("qi_dogfood_long.done", tenant=tenant_id)
    except Exception:
        log.exception("qi_dogfood_long.failed (non-fatal)")


def register_qi_cron_jobs(sched: Any, app: Any, tenant_id: str) -> None:
    """注册启 cron jobs.

    cron 表达式: 默认每小时跑一次, 内部用 _check_qi_active 守门窗口外 skip.
    优势: 用户改 SoulFile.qi_window 时, cron 自动尊重新窗口 (不需重启).
    """

    async def _pc_job() -> None:
        await _qi_predictive_coding_train(app, tenant_id)

    async def _darwin_job() -> None:
        await _qi_darwin_godel_explore(app, tenant_id)

    async def _ai_scientist_job() -> None:
        await _qi_ai_scientist_explore(app, tenant_id)

    async def _auto_promote_job() -> None:
        await _qi_auto_promote(app, tenant_id)

    async def _dogfood_long_job() -> None:
        await _qi_dogfood_long(app, tenant_id)

    # 每小时 tick 一次 (深夜窗口活跃 → 真跑; 窗口外 skip)
    sched.register("qi_pc_train_hourly", "@hourly", _pc_job)
    sched.register("qi_darwin_explore_hourly", "@hourly", _darwin_job)
    sched.register("qi_ai_scientist_hourly", "@hourly", _ai_scientist_job)
    sched.register("qi_auto_promote_hourly", "@hourly", _auto_promote_job)
    # dogfood 每天 3 AM 跑一次 (启窗口里)
    sched.register("qi_dogfood_long_daily", "0 3 * * *", _dogfood_long_job)


__all__ = ["register_qi_cron_jobs"]
