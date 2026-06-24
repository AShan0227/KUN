"""Supervisor · 监督线常驻 (ADR-020 / ADR-021 / ADR-022).

Type      : 常驻 service
Line      : 监督线 (与主线并行旁路)
Model     : 远程 (gpt-5.5)
Input     : event stream (NATS / DB)
Output    : anomaly / drift signals / RCDH 诊断 / strategy_search_request
闭环      : RSI 触发起点

主要职责:
  1. 订阅 event stream (订阅 kun.* NATS subjects)
  2. 走 RCDH 异常归因 (ADR-021 4 级强制)
  3. 写 diagnostic_records (走完 L0→L3)
  4. 长任务 anti-drift: 注入 plan_review (ADR-022 Layer 4)
  5. 写 strategy_search_request 触发 Strategist (ADR-024 step 5-6)
  6. 监督线三级阈值 → 4 级升级路径 (weak/mid/strong → role/task/gate/human)

监督线核心运行机制以 ADR-021 + ADR-022 + ADR-023 为准.

实施路径:
  L1.2 · 把 control_plane/nuo + runtime_observation + idle_batch 迁来
  L2   · Supervisor service 真做 (事件流 + 异常阈值 + 写 strategy_search_request)
  L2   · 加 RCDH 诊断层级 + diagnostic_records 表写入
  L2   · Periodic Plan Review Heartbeat (每 3 步/5 分钟)
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor")


class Supervisor(Protocol):
    """监督线常驻 Protocol — 旁路观察 + 异常归因 + RCDH."""

    async def observe(
        self,
        event: Any,  # Event
    ) -> None:
        """事件流处理: 检测异常 / 漂移 / 阻塞 → 写 supervisor_observations."""
        ...

    async def diagnose(
        self,
        symptom: Any,
        *,
        scope_modules: list[str] | None = None,  # narrow_scope 圈定的 ≤5 模块
    ) -> Any:  # DiagnosticRecord
        """走 RCDH 4 级诊断 (ADR-021) → DiagnosticRecord."""
        ...

    async def request_strategy_search(
        self,
        diagnostic: Any,  # DiagnosticRecord
    ) -> None:
        """写 strategy_search_request 触发 Strategist (ADR-024 step 5)."""
        ...
