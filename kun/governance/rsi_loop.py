"""RSI 闭环 10 步编排 (ADR-024).

完整流程:
  1. 人类定义目标 / 安全边界 / 验收标准
  2. Director 启动 (拆任务 + GoalAnchor + complexity + priority_profile)
  3. Gate 建立任务账本 (sandbox / lock / rollback / evidence_ledger)
  4. Executor 执行主线 + 监督线并行启动
  5. Supervisor 走 RCDH 自评 (ADR-021)
  6. Strategist 做 Strategy Search (Explorer Pool 并行)
  7. Safe Experimentation (sandbox / lock / rollback drill)
  8. Learning Signal 独立收集
  9. 合议层 (dedup / cluster / 排序)
  10. Capability Governance (写 runtime_capabilities + promotion_queue)

3 条工程化约束:
  - 自指限制: 改 Strategist/Supervisor/Director/Gate 自己 → L4 人审
  - 基础能力闸门: Phase 1a 95% CI 下界 ≥ 0.7 才进 1b
  - 候选能力晋级队列: runtime_enabled 默认 false + 超时重审

实施路径: L2 阶段编排 step 1-5 + 7-10, step 6 Strategist 真做.
"""

from __future__ import annotations

from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.rsi_loop")


SELF_REFERENTIAL_TARGETS = {
    "strategist",
    "supervisor",
    "director",
    "gate",
    "external_supervisor",
}


async def trigger_rsi_loop(
    request: Any,  # StrategySearchRequest
) -> Any:
    """从 strategy_search_request 触发完整 RSI 闭环.

    L2 阶段编排 10 步; 现在是骨架.
    """
    # TODO L2: 编排 step 1-10
    log.info("rsi_loop.triggered", request_id=getattr(request, "request_id", "?"))
    raise NotImplementedError("L2 阶段实装")


def is_self_referential(target_module: str) -> bool:
    """自指检查 — 改 Strategist/Supervisor/Director/Gate 自己?

    Strategist.submit_experiment 调用此判定, true → 强制 L4 人审.
    """
    return target_module.split(".")[0] in SELF_REFERENTIAL_TARGETS
