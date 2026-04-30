"""Production value estimators for ValueGate (V2.2 §19.4 Wire 2).

替换 default heuristic estimator (last_value + 0.05) → 真信号驱动:
1. capability_card 历史 success_rate (针对该 task_type / model)
2. 预算剩余比 (cost / budget)
3. 上一步 multi_judge 一致率 (如果有)
4. context complexity penalty

各信号独立可关 (env / config), 默认全开.

用法:
    estimator = ProductionValueEstimator()
    gate = ValueGate(
        marginal_criterion=...,
        value_estimator=estimator.estimate,
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProductionValueEstimator:
    """信号驱动的 expected_value 估算.

    Args:
        capability_weight: capability_score 权重. 默认 0.35.
        budget_weight: 预算剩余权重. 默认 0.25.
        multi_judge_weight: 上一步 multi_judge consensus 权重. 默认 0.2.
        history_weight: ValueGate 历史贡献信用权重. 默认 0.2.
        floor: value 下限 (无信号兜底). 默认 0.5.
    """

    capability_weight: float = 0.35
    budget_weight: float = 0.25
    multi_judge_weight: float = 0.2
    history_weight: float = 0.2
    floor: float = 0.5

    def __post_init__(self) -> None:
        total = (
            self.capability_weight
            + self.budget_weight
            + self.multi_judge_weight
            + self.history_weight
        )
        if abs(total - 1.0) > 0.01:
            logger.warning("ProductionValueEstimator weights sum to %.2f, expected 1.0", total)

    async def estimate(self, ctx: dict[str, Any]) -> float:
        """主入口. 从 context dict 提取 task_type / cost / budget / multi_judge 信号.

        ctx 期望:
        - task_id: str
        - step_id: int
        - purpose: str
        - mode: "FAST" | "SMART" | "MAX"
        - task_type: str (optional)
        - estimated_cost_usd: float (optional)
        - accumulated_cost_usd: float (optional)
        - budget_usd: float (optional)
        - last_multi_judge_consensus: float 0..1 (optional)
        """
        # 1. capability score (查 capability_card)
        cap_value = await self._capability_score(ctx)

        # 2. budget remaining ratio (1.0 = 全预算可用; 0.0 = 烧光)
        budget_value = self._budget_remaining(ctx)

        # 3. 上一步 multi_judge consensus (默认 0.7 中立)
        judge_value = self._multi_judge_value(ctx)

        # 4. 跨任务 ValueGate 历史信用
        history_value = self._history_value(ctx)

        # 加权求和
        total = (
            self.capability_weight * cap_value
            + self.budget_weight * budget_value
            + self.multi_judge_weight * judge_value
            + self.history_weight * history_value
        )
        # 兜底: 任何信号都没拿到 → floor
        if cap_value == 0.0 and budget_value == 1.0 and judge_value == 0.7 and history_value == 0.0:
            return self.floor

        return max(0.0, min(1.0, total))

    async def _capability_score(self, ctx: dict[str, Any]) -> float:
        """查 capability_card 拿该 task_type 的 historical success_rate."""
        task_type = ctx.get("task_type") or ctx.get("purpose")
        tenant_id = ctx.get("tenant_id")
        if not task_type or not tenant_id:
            return 0.0  # 没信息

        try:
            from sqlalchemy import select

            from kun.core.db import session_scope
            from kun.core.orm import CapabilityCardRow

            async with session_scope(tenant_id=str(tenant_id)) as s:
                # 查 model 类的 capability cards
                stmt = (
                    select(CapabilityCardRow)
                    .where(
                        CapabilityCardRow.tenant_id == tenant_id,
                        CapabilityCardRow.entity_type == "model",
                    )
                    .limit(10)
                )
                rows = (await s.execute(stmt)).scalars().all()
                if not rows:
                    return 0.0
                # 平均 reliability
                scores = [float(r.overall_reliability or 0.0) for r in rows]
                return sum(scores) / len(scores) if scores else 0.0
        except Exception:
            logger.exception("capability_score lookup failed")
            return 0.0

    def _budget_remaining(self, ctx: dict[str, Any]) -> float:
        """预算剩余比 (1.0 = 全预算可用)."""
        accumulated = float(ctx.get("accumulated_cost_usd", 0.0))
        budget = float(ctx.get("budget_usd", 0.0))
        if budget <= 0:
            return 1.0  # 没预算限制 = 全可用
        remaining = budget - accumulated
        return max(0.0, min(1.0, remaining / budget))

    def _multi_judge_value(self, ctx: dict[str, Any]) -> float:
        """上一步 multi_judge 一致率 (默认 0.7 中立)."""
        consensus = ctx.get("last_multi_judge_consensus")
        if consensus is None:
            return 0.7  # 中立
        return max(0.0, min(1.0, float(consensus)))

    def _history_value(self, ctx: dict[str, Any]) -> float:
        """Read durable/hot ValueGate resource credit seeded by Orchestrator."""

        raw_keys = ctx.get("value_gate_resource_keys") or []
        if not isinstance(raw_keys, list):
            return 0.0
        try:
            from kun.engineering.credit_assignment import get_contribution_tracker

            tracker = get_contribution_tracker()
            scores = []
            for raw in raw_keys:
                if not raw:
                    continue
                # Use the full durable key.  Some ids intentionally contain
                # colons, e.g. ``value_gate:task_type:ops.workflow``.
                scores.append(tracker.contribution_score(str(raw)))
            return max(scores) if scores else 0.0
        except Exception:
            logger.exception("value_gate.history_lookup_failed")
            return 0.0


__all__ = ["ProductionValueEstimator"]
