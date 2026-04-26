"""DynamicReplanner — 任务中途局部重规划 (V2.2 §22 + BATCH5 C20).

跟 OODA 外层循环 (kun/core/ooda_loop.py) 配套. 在 Reflect 阶段判断"当前 plan
是否还成立", 不成立 → 从当前 step 之后重新规划, 保留前面 step 的 sunk work
(不丢已完成的事).

跟"完整重 plan" (重新走一遍 intent → planner) 区别:
- 完整重 plan: 丢前面所有 step, 重新走一遍, 沉没成本 100%
- 局部重 plan (本模块): 保留 step 0..N-1 已完成, 从 step N 开始重新规划

核心 3 方法:
- detect_replan_needed(cycle) → (bool, reason): 看 reflection 决定是否需要 replan
- replan_from_step(original_plan, current_step_idx, new_observations) → Plan: 局部重规划
- calculate_sunk_cost(original_plan, current_step_idx) → float: 沉没成本估算

设计原则:
- 不主动接 orchestrator (留 TODO, 现 step loop 没 mid-task replan 钩子)
- 配 OODA: Reflect → detect → ADJUST → replan_from_step → DECIDE 走新 plan
- 配 marginal_roi: replan 收益 vs 沉没成本 + replan 自身成本要 > 阈值才值得

TODO: orchestrator wire by Claude (M5) — 需要 orchestrator step loop 加
mid-task replan 钩子, 跟 OODA 状态机集成.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReplanDecision:
    """是否 replan 的决策结果."""

    needs_replan: bool
    reason: str  # outcome_mismatch / step_failure_repeated / scope_drift / no_replan_needed
    confidence: float = 0.5  # 0..1, 决策置信度
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    """简化版 plan (实际生产用 brain.planner.Plan, 这里 stub for 测试)."""

    steps: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SunkCostEstimate:
    """沉没成本估算."""

    completed_steps: int
    total_planned_steps: int
    completed_cost_usd: float
    completed_duration_sec: float
    progress_ratio: float  # 0..1, 已完成 step / total
    can_reuse_outputs: bool  # 前面 step 的 output 是否可作为新 plan 的 input


class DynamicReplanner:
    """任务中途局部重规划器.

    用法:
        replanner = DynamicReplanner()
        decision = await replanner.detect_replan_needed(ooda_cycle)
        if decision.needs_replan:
            sunk = replanner.calculate_sunk_cost(original_plan, current_step_idx)
            new_plan = await replanner.replan_from_step(
                original_plan, current_step_idx, new_observations
            )
            # OODA cycle: ADJUST → DECIDE 走 new_plan

    Args:
        min_replan_confidence: 决策置信度下限 (默认 0.6)
        max_step_failure_count: 同一 step 失败 N 次自动触发 replan (默认 2)
        scope_drift_threshold: outcome 跟 expected 的 mismatch 比例 (默认 0.3)
    """

    def __init__(
        self,
        *,
        min_replan_confidence: float = 0.6,
        max_step_failure_count: int = 2,
        scope_drift_threshold: float = 0.3,
    ) -> None:
        self.min_replan_confidence = min_replan_confidence
        self.max_step_failure_count = max_step_failure_count
        self.scope_drift_threshold = scope_drift_threshold

    async def detect_replan_needed(self, cycle: Any) -> ReplanDecision:
        """检测当前 OODA cycle 是否需要 replan.

        判断依据 (按优先级):
        1. 最新 reflection.needs_adjust=True 且 reason 含 "scope_drift" → replan (高 confidence)
        2. 同 step 连续失败 ≥ max_step_failure_count → replan (高 confidence)
        3. action 跟 expected_outcome mismatch 比例 > threshold → replan (中 confidence)
        4. 否则 no replan
        """
        reflections = getattr(cycle, "reflections", []) or []
        actions = getattr(cycle, "actions_taken", []) or []

        # 1. 最新 reflection 显式说要 adjust + scope_drift
        if reflections:
            latest = reflections[-1]
            if latest.get("needs_adjust") and "scope_drift" in str(latest.get("reason", "")):
                return ReplanDecision(
                    needs_replan=True,
                    reason="scope_drift",
                    confidence=0.85,
                    metadata={"reflection": latest},
                )

        # 2. 同 step 连续失败
        if len(actions) >= self.max_step_failure_count:
            recent = actions[-self.max_step_failure_count :]
            failed = [a for a in recent if str(a.get("status", "")) in ("failed", "error")]
            if len(failed) >= self.max_step_failure_count:
                step_ids = {a.get("step_id") for a in failed}
                # 同一 step 反复失败
                if len(step_ids) <= 1:
                    return ReplanDecision(
                        needs_replan=True,
                        reason="step_failure_repeated",
                        confidence=0.90,
                        metadata={"failed_step_id": next(iter(step_ids), None)},
                    )

        # 3. outcome mismatch
        if reflections:
            latest = reflections[-1]
            mismatch = float(latest.get("outcome_mismatch_ratio", 0.0))
            if mismatch > self.scope_drift_threshold:
                return ReplanDecision(
                    needs_replan=True,
                    reason="outcome_mismatch",
                    confidence=0.7,
                    metadata={"mismatch_ratio": mismatch},
                )

        return ReplanDecision(needs_replan=False, reason="no_replan_needed", confidence=0.5)

    async def replan_from_step(
        self,
        original_plan: Plan,
        current_step_idx: int,
        new_observations: list[dict[str, Any]],
    ) -> Plan:
        """从 current_step_idx 之后重新规划, 保留前面 step.

        简化实装: 把 original_plan.steps[:current_step_idx] 保留, 从
        current_step_idx 开始, 用 new_observations 替换 metadata 后产新 step list.

        生产实装 (M5): 调用 LLM planner 真重 plan, 把 "completed_steps_outputs"
        作为 context 让 LLM 知道前面跑过啥.
        """
        if current_step_idx < 0 or current_step_idx > len(original_plan.steps):
            raise ValueError(
                f"current_step_idx out of range: {current_step_idx} "
                f"(plan has {len(original_plan.steps)} steps)"
            )

        # 保留已完成 step
        kept_steps = original_plan.steps[:current_step_idx]
        # 从 current step 之后产新 step (简化 stub: 用 observations 替换 description)
        new_steps: list[dict[str, Any]] = []
        for i, obs in enumerate(new_observations):
            new_steps.append(
                {
                    "step_id": current_step_idx + i + 1,
                    "description": str(obs.get("intent", "replanned step")),
                    "skill_hint": obs.get("skill_hint"),
                    "replan_origin": "dynamic_replanner",
                }
            )

        return Plan(
            steps=kept_steps + new_steps,
            metadata={
                **original_plan.metadata,
                "replanned_from_step": current_step_idx,
                "kept_step_count": len(kept_steps),
                "new_step_count": len(new_steps),
            },
        )

    def calculate_sunk_cost(self, original_plan: Plan, current_step_idx: int) -> SunkCostEstimate:
        """沉没成本估算 (用户决策时知道损失多少)."""
        completed = original_plan.steps[:current_step_idx]
        total = len(original_plan.steps)
        completed_cost = sum(float(s.get("cost_usd_estimate", 0.0)) for s in completed)
        completed_duration = sum(float(s.get("duration_sec_estimate", 0.0)) for s in completed)
        progress = current_step_idx / total if total > 0 else 0.0

        # 简化判定 can_reuse_outputs: 看 step 是否有 output_ref
        can_reuse = any(s.get("output_ref") for s in completed)

        return SunkCostEstimate(
            completed_steps=current_step_idx,
            total_planned_steps=total,
            completed_cost_usd=completed_cost,
            completed_duration_sec=completed_duration,
            progress_ratio=progress,
            can_reuse_outputs=can_reuse,
        )

    def is_replan_worth_it(
        self,
        decision: ReplanDecision,
        sunk_cost: SunkCostEstimate,
        *,
        replan_cost_estimate: float = 0.05,
    ) -> tuple[bool, str]:
        """ROI 判断: replan 值得吗?

        简化公式:
        - 如果 decision.confidence < min_replan_confidence → 不值
        - 如果 progress > 80% (快做完了) → 不值, 沉没成本太高
        - 如果 confidence ≥ 0.85 (强信号) → 值, 不管 progress
        - 否则 confidence × (1 - progress) > 0.4 → 值

        Returns:
            (worth_it, reason)
        """
        if decision.confidence < self.min_replan_confidence:
            return False, f"confidence_below_threshold:{decision.confidence:.2f}"
        if decision.confidence >= 0.85:
            return True, "high_confidence_signal"
        if sunk_cost.progress_ratio > 0.8:
            return False, f"too_much_progress:{sunk_cost.progress_ratio:.2f}"
        score = decision.confidence * (1.0 - sunk_cost.progress_ratio)
        if score > 0.4:
            return True, f"roi_positive:{score:.2f}"
        return False, f"roi_negative:{score:.2f}"


__all__ = [
    "DynamicReplanner",
    "Plan",
    "ReplanDecision",
    "SunkCostEstimate",
]
