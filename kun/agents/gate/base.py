"""Gate · 主线出口 + 治理门禁 (ADR-020 / ADR-024).

Type      : 常驻 service
Line      : 主线 (5 阶段中的第 5 阶段)
Model     : 远程 (gpt-5.5)
Input     : TestReport + evidence_ledger + RCDH diagnostic_record + External Supervisor debrief
Output    : 准入决策 + runtime_capabilities 写入 + promotion_queue 维护
闭环      : capability 晋级 → 实际启用

主要职责 (ADR-020 角色定位):
  1. 维护 evidence_ledger
  2. 拒绝无 diagnostic_id 的修复请求 (ADR-021 RCDH 强制约束)
  3. 拒绝 debrief 不过关的任务 (ADR-023 External Supervisor Mode B)
  4. 维护 promotion_queue: replay / shadow / canary / rollback drill 序列 (ADR-024)
  5. 写 runtime_capabilities.enabled = true 触发 capability 启用 (ADR-024 step 10)
  6. 不做"审美 / 产品体验判断" — 那是 External Supervisor + 人

Gate 是 "门禁执行者", 不是 "产品评委".

实施路径:
  L1.2 · 把 control_plane/runtime (Gate 部分) + capability_writeback 迁来
  L1.3 · alembic 0011 建 runtime_capabilities + promotion_queue 字段
  L2   · 加 RCDH report 验证 + Debrief 验证
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.gate")


class Gate(Protocol):
    """主线出口 Protocol — 证据 + 合议 + 合并启用门禁."""

    async def admit(
        self,
        artifact: Any,
        *,
        test_report: Any,  # TestReport
        diagnostic_record: Any | None = None,  # DiagnosticRecord (修复场景必填)
        debrief: Any | None = None,  # ExternalSupervisor Mode B debrief
    ) -> Any:  # GateDecision
        """治理决策 — 准入 / 退回 / 暂停."""
        ...

    async def enable_capability(
        self,
        capability_id: str,
    ) -> None:
        """写 runtime_capabilities.enabled = true (capability 晋级到生效)."""
        ...
