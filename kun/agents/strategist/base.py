"""Strategist · 监督线 on-demand 角色 (ADR-020 / ADR-024).

Type      : on-demand (被监督线触发后实例化)
Line      : 监督线
Model     : 远程 + 本地 (Explorer Pool 3 模式)
Input     : strategy_search_request (Supervisor / External Supervisor 写入)
Output    : StrategyExperiment (写入 runtime_experiments)
闭环      : 实验 → runtime_experiments → 启用

Explorer Pool 3 模式 (默认):
  - Explorer-Aggressive   — 探索激进新架构
  - Explorer-Conservative — 修复式优化
  - Explorer-Performance  — 性能 / 稳定向

复杂任务自动启用 3 个 explorer 并行; 简单任务只用 1 个 (Conservative).

主要职责:
  1. 读 strategy_search_request
  2. 多 Explorer 并行生成候选策略
  3. 设计实验方案 (forward vs backward, ADR-021)
  4. 每个候选带验收指标 + acceptance_threshold + rollback_on
  5. 写 StrategyExperiment 进 runtime_experiments
  6. 自指限制 (ADR-024 约束 1): 改 Strategist/Supervisor/Gate/Director 自己 → 强制人审

实施路径:
  L1.2 · 把 control_plane/capability_evolution 改造为 Strategist 雏形
  L2   · 加 Explorer Pool + 第一次 RSI 实例 (LLM 路由优化)
  L3   · forward/backward 双策略 auto-select
  L4   · Explorer Pool 配置化 + 多实例并行
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.strategist")


class Strategist(Protocol):
    """监督线 on-demand Protocol — 策略搜索 + 实验设计."""

    async def search(
        self,
        request: Any,  # StrategySearchRequest
    ) -> list[Any]:  # list[StrategyExperiment]
        """读 search request → Explorer Pool 并行 → 输出候选实验列表."""
        ...

    async def submit_experiment(
        self,
        experiment: Any,  # StrategyExperiment
    ) -> str:
        """写 StrategyExperiment 进 runtime_experiments. 自指限制在此强制."""
        ...
