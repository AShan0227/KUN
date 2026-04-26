"""EmergentSwitch — 涌现方案识别与切换 (V2.1 §5.8 / 漏洞 5+7).

核心:
- 信号驱动 (8 种触发信号), 不每步查 (零额外开销 90% 任务)
- 切换决策走 §17.3 strategy_score 公式
- 防抖动: 冷却期 5 分钟 + 单任务 ≤2 次 + switch_score 阈值 0.15
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from kun.core.emergent_solution import EmergentSolution, EmergentSolutionLibrary

logger = logging.getLogger(__name__)


SwitchSignal = Literal[
    "surprise_high",  # surprise_score > 0.6
    "step_count_exceeded",  # actual_steps > 1.5 * estimated
    "replan_count_positive",  # 已重规划过
    "external_emergent_found",  # §3.10 异步守望发现
    "llm_metacognitive",  # §17.10 模式 C
    "user_correction",  # WS correction
    "capability_card_better",  # 历史更优路径
    "watchtower_signal",  # 守望主动发现
]


@dataclass
class TaskRuntimeStats:
    """任务运行时统计 (用于触发判断)."""

    task_id: str
    started_at: datetime
    estimated_steps: int = 1
    actual_steps: int = 0
    replan_count: int = 0
    last_surprise_score: float = 0.0
    switches_done: int = 0
    last_switch_at: datetime | None = None


@dataclass
class SwitchEvaluation:
    """切换评估结果."""

    should_switch: bool
    switch_score: float = 0.0
    chosen_solution: EmergentSolution | None = None
    reason: str = ""
    blocked_by: str = ""  # cooldown / max_switches / threshold / no_candidate


class EmergentSwitchManager:
    """涌现方案识别与切换管理器.

    V2.1.2 §5.8.4 防抖动:
    - 同任务切换后 N 分钟内不再评估 (默认 5 min)
    - 单任务总切换次数 ≤ 2
    - switch_score < 0.15 阈值不切
    - 用户偏好"不打扰" → 阈值升 0.30 + 默认不切只通知
    """

    def __init__(
        self,
        library: EmergentSolutionLibrary,
        *,
        cooldown_minutes: int = 5,
        max_switches_per_task: int = 2,
        switch_threshold: float = 0.15,
    ) -> None:
        self._library = library
        self.cooldown_minutes = cooldown_minutes
        self.max_switches_per_task = max_switches_per_task
        self.switch_threshold = switch_threshold
        self._stats: dict[str, TaskRuntimeStats] = {}

    def register_task(
        self,
        task_id: str,
        estimated_steps: int = 1,
    ) -> TaskRuntimeStats:
        st = TaskRuntimeStats(
            task_id=task_id,
            started_at=datetime.now(UTC),
            estimated_steps=estimated_steps,
        )
        self._stats[task_id] = st
        return st

    def step_completed(self, task_id: str, surprise_score: float = 0.0) -> None:
        st = self._stats.get(task_id)
        if st is None:
            st = self.register_task(task_id)
        st.actual_steps += 1
        st.last_surprise_score = surprise_score

    def detect_signals(
        self,
        task_id: str,
        task_type: str,
        external_signals: list[SwitchSignal] | None = None,
    ) -> list[SwitchSignal]:
        """检测当前是否有切换信号. 返触发的信号列表."""
        signals: list[SwitchSignal] = []
        st = self._stats.get(task_id)
        if st is None:
            return signals

        # 1. surprise 高
        if st.last_surprise_score > 0.6:
            signals.append("surprise_high")

        # 2. 步数超估
        if st.estimated_steps > 0 and st.actual_steps > 1.5 * st.estimated_steps:
            signals.append("step_count_exceeded")

        # 3. 已重规划过
        if st.replan_count > 0:
            signals.append("replan_count_positive")

        # 4. 外部涌现库有候选
        if self._library.has_active_for(task_type):
            signals.append("external_emergent_found")

        # 5-8. 外部信号 (来自 watchtower / LLM 元认知 / 用户 / capability_card)
        if external_signals:
            signals.extend(external_signals)

        return signals

    def evaluate_switch(
        self,
        task_id: str,
        task_type: str,
        current_strategy_outcome: float,
        current_remaining_cost_usd: float,
        signals: list[SwitchSignal],
        user_interruption_tolerance: Literal["low", "medium", "high"] = "medium",
    ) -> SwitchEvaluation:
        """评估是否切换."""
        st = self._stats.get(task_id)
        if st is None:
            return SwitchEvaluation(should_switch=False, blocked_by="task_not_registered")

        # 防抖动 1: 冷却期
        if (
            st.last_switch_at is not None
            and datetime.now(UTC) - st.last_switch_at
            < timedelta(minutes=self.cooldown_minutes)
        ):
            return SwitchEvaluation(should_switch=False, blocked_by="cooldown")

        # 防抖动 2: 总切换次数
        if st.switches_done >= self.max_switches_per_task:
            return SwitchEvaluation(should_switch=False, blocked_by="max_switches_reached")

        # 没信号不切
        if not signals:
            return SwitchEvaluation(should_switch=False, blocked_by="no_signal")

        # 找候选方案
        candidates = self._library.list_for_task_type(
            task_type,
            statuses=("shadow_testing", "canary", "stable"),
        )
        if not candidates:
            return SwitchEvaluation(should_switch=False, blocked_by="no_candidate")

        # 按 §17.3 公式打分 (新方案 vs 当前方案)
        best_candidate = max(
            candidates,
            key=lambda c: c.estimated_outcome_delta - 0.5 * c.estimated_cost_delta,
        )

        # switch_score 计算
        outcome_gain = best_candidate.estimated_outcome_delta
        cost_diff = best_candidate.estimated_cost_delta
        # latency_overhead 假设切换本身 1s 开销
        switch_latency_overhead = 1.0 / 60.0
        risk_penalty = 0.05 if best_candidate.status == "shadow_testing" else 0.0

        switch_score = (
            outcome_gain
            - 0.3 * cost_diff  # cost_diff 负 = 省钱 → 加分
            - 0.2 * switch_latency_overhead
            - 0.15 * risk_penalty
        )

        # 用户偏好: 不打扰 → 阈值升
        threshold = self.switch_threshold
        if user_interruption_tolerance == "low":
            threshold = 0.30

        if switch_score < threshold:
            return SwitchEvaluation(
                should_switch=False,
                switch_score=switch_score,
                chosen_solution=best_candidate,
                blocked_by=f"score_{switch_score:.2f}_below_{threshold:.2f}",
            )

        return SwitchEvaluation(
            should_switch=True,
            switch_score=switch_score,
            chosen_solution=best_candidate,
            reason=f"signals={signals[:3]} score={switch_score:.2f}",
        )

    def commit_switch(self, task_id: str) -> None:
        """切换执行后, 更新统计."""
        st = self._stats.get(task_id)
        if st is None:
            return
        st.switches_done += 1
        st.last_switch_at = datetime.now(UTC)
        st.replan_count += 1

    def get_stats(self, task_id: str) -> TaskRuntimeStats | None:
        return self._stats.get(task_id)

    def cleanup(self, task_id: str) -> None:
        self._stats.pop(task_id, None)


__all__ = [
    "EmergentSwitchManager",
    "SwitchEvaluation",
    "SwitchSignal",
    "TaskRuntimeStats",
]
