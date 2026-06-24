"""Executor · 主线主体角色 (ADR-020).

Type      : task-bound (每任务实例化)
Line      : 主线 (5 阶段中的第 2-3 阶段)
Model     : 远程 (gpt-5.5)
Input     : TaskSpec + runtime_experiments + runtime_capabilities
Output    : Artifact + capability writeback + events
闭环      : 路由 → 能力卡 → 路由

主要职责:
  1. 按 TaskSpec 执行任务 (delegates: engineering/orchestrator 的执行部分)
  2. 读 runtime_experiments 应用候选策略 override
  3. 读 runtime_capabilities 决定走哪条已启用 capability
  4. 写 capability card (ADR-024 RSI 闭环 step 4)
  5. emit events 给监督线
  6. Long-task mode: 每次 LLM call 顶部 pin GoalAnchor (ADR-022 Layer 2)
  7. Long-task mode: 加 anti-sycophancy system prompt (ADR-022 Layer 5)

实施路径:
  L1.2 · 把 engineering/orchestrator 的 execute 部分拆来
  L1.6 · 接 capability_router 进 LLMRouter.decide() (路由闭环)
  L1.8 · GoalAnchor 顶部 pinning
  L1.9 · Anti-sycophancy system prompt
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.executor")


class Executor(Protocol):
    """主线主体 Protocol — 执行任务 + 写能力卡 + 读 experiments/capabilities."""

    async def run(
        self,
        task_spec: Any,  # TaskSpec
        *,
        goal_anchor: Any | None = None,
        experiments: list[Any] | None = None,  # list[RuntimeExperiment]
    ) -> Any:  # Artifact
        """执行任务 → 产出 Artifact, 同步写 capability card / events."""
        ...
