"""ValueGate — 守望主决策 gate (V2.2 §19.4 — KUN 决策核心 C).

把 V2.1 §17 StrategyMatcher 从"router 内部用"提升为"守望主决策核心". 守望在每
一步开始前算 ROI, 用 marginal_roi 判停, 决定 continue / skip / stop / escalate.

设计要点:
- 守望不是事后监控, 是 "投资人 — 每一步前先算账"
- 每一步开头都过 gate, 算"再花这一步的钱+时间, 任务结果会变好多少"
- 不值得 → skip 这一步 / 改 action / 升级到人
- opt-in: orchestrator 里默认 None, 不影响现有测试

接口:
    gate = ValueGate(matcher=get_matcher(), criterion=ModulePresets.for_idle_batch_step())
    decision = await gate.check_step(
        task_ref=ref,
        step_plan=plan,
        prior_value_history=[0.5, 0.6, 0.65],
        signals=SignalBundle(...),
    )
    if decision.decision == "stop":
        ...
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kun.engineering.marginal_roi import MarginalROIStopCriterion, StopDecision

logger = logging.getLogger(__name__)


GateDecisionKind = Literal["continue", "skip", "stop", "escalate"]


@dataclass
class ValueGateDecision:
    """守望 gate 决策结果."""

    decision: GateDecisionKind
    reason: str  # marginal_stop / value_below_threshold / cost_exceed / escalate_to_user / etc
    expected_value: float = 0.0
    marginal_decision: StopDecision | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# 类型: 算 expected_value 的函数. orchestrator 注入具体实现.
ValueEstimatorFn = Callable[[dict[str, Any]], Awaitable[float]]


class ValueGate:
    """守望主决策 gate.

    用法 (orchestrator 内部):
        gate = ValueGate(
            min_value_threshold=0.30,
            marginal_criterion=ModulePresets.for_idle_batch_step(),
            value_estimator=my_estimator,  # 算 expected_value 的函数
        )
        for step in plan.steps:
            decision = await gate.check_step(
                task_ref=ref,
                step_plan=step,
                prior_value_history=value_history,
            )
            if decision.decision == "stop": break
            if decision.decision == "skip": continue
            ...

    Args:
        min_value_threshold: expected_value < 这个 → escalate. 默认 0.20.
        marginal_criterion: 边际收益判停器. 必须给.
        value_estimator: async fn(context: dict) → float. 默认估算 "当前累计 +
            一个固定 epsilon" (= 兜底).
        escalate_handler: 升级到人时调用的 callback (M5 接 ws ask_user).
    """

    def __init__(
        self,
        marginal_criterion: MarginalROIStopCriterion,
        *,
        min_value_threshold: float = 0.20,
        value_estimator: ValueEstimatorFn | None = None,
        escalate_handler: Callable[[ValueGateDecision], Awaitable[None]] | None = None,
    ) -> None:
        if not 0 <= min_value_threshold <= 1:
            raise ValueError("min_value_threshold must be in [0, 1]")
        self.marginal_criterion = marginal_criterion
        self.min_value_threshold = min_value_threshold
        self.value_estimator = value_estimator
        self.escalate_handler = escalate_handler
        self._stats = {
            "checks_total": 0,
            "decisions_continue": 0,
            "decisions_skip": 0,
            "decisions_stop": 0,
            "decisions_escalate": 0,
        }

    async def check_step(
        self,
        *,
        task_ref: Any,
        step_plan: Any,
        prior_value_history: list[float],
        context: dict[str, Any] | None = None,
    ) -> ValueGateDecision:
        """步骤开始前的决策检查.

        Returns:
            ValueGateDecision: continue / skip / stop / escalate + 原因
        """
        self._stats["checks_total"] += 1
        ctx = context or {}
        ctx.setdefault("task_id", getattr(getattr(task_ref, "meta", None), "task_id", "?"))
        ctx.setdefault("step_id", getattr(step_plan, "step_id", -1))

        # 1. 算 expected_value
        try:
            expected_value = await self._estimate_value(ctx, prior_value_history)
        except Exception:
            logger.exception("value_gate.estimate_failed (defaulting to 0.5)")
            expected_value = 0.5

        # 2. expected_value 太低 → escalate (问人是否继续)
        if expected_value < self.min_value_threshold:
            d = ValueGateDecision(
                decision="escalate",
                reason="value_below_threshold",
                expected_value=expected_value,
                metadata={"min_threshold": self.min_value_threshold, **ctx},
            )
            self._stats["decisions_escalate"] += 1
            await self._maybe_escalate(d)
            return d

        # 3. marginal_roi 判停
        marginal_decision = self.marginal_criterion.should_stop(prior_value_history)
        if marginal_decision.should_stop:
            d = ValueGateDecision(
                decision="stop",
                reason=f"marginal:{marginal_decision.reason}",
                expected_value=expected_value,
                marginal_decision=marginal_decision,
                metadata=ctx,
            )
            self._stats["decisions_stop"] += 1
            return d

        # 4. 默认 continue
        d = ValueGateDecision(
            decision="continue",
            reason="value_acceptable",
            expected_value=expected_value,
            marginal_decision=marginal_decision,
            metadata=ctx,
        )
        self._stats["decisions_continue"] += 1
        return d

    async def _estimate_value(
        self,
        ctx: dict[str, Any],
        prior_history: list[float],
    ) -> float:
        if self.value_estimator is not None:
            return await self.value_estimator(ctx)
        # 默认: 没有 estimator 时用兜底逻辑 (上一步 value + epsilon)
        if not prior_history:
            return 0.5
        return min(1.0, prior_history[-1] + 0.05)

    async def _maybe_escalate(self, decision: ValueGateDecision) -> None:
        if self.escalate_handler is None:
            return
        try:
            await self.escalate_handler(decision)
        except Exception:
            logger.exception("value_gate.escalate_handler failed")

    def get_stats(self) -> dict[str, int]:
        """监控用 (NUO 显示)."""
        return dict(self._stats)

    async def record_step_outcome(
        self,
        *,
        task_id: str,
        step_id: int,
        outcome_value: float,
        cost_usd: float = 0.0,
        success: bool = True,
    ) -> None:
        """orchestrator step 完成后调, 让 gate 记录"实际产出 value".

        outcome_value: 这一步带来的实际价值 (0..1). 通常调用方算法:
            - 若 success: 0.6 + 0.3 * (1 - cost_usd / budget) 或 capability_card 历史 success_rate
            - 若 fail: 0.1
            - 若 partial: 0.4
        """
        # 默认实现: 记日志 + 累计统计.
        # 实际 value 历史由 orchestrator 维护 (因为 orchestrator 持有 _value_history list).
        # ValueGate 只暴露 record_step_outcome 给后续学习/审计用.
        logger.info(
            "value_gate.step_outcome",
            extra={
                "task_id": task_id,
                "step_id": step_id,
                "outcome_value": outcome_value,
                "cost_usd": cost_usd,
                "success": success,
            },
        )


__all__ = [
    "GateDecisionKind",
    "ValueEstimatorFn",
    "ValueGate",
    "ValueGateDecision",
]
