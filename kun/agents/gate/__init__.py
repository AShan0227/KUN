"""Gate agent — 主线出口 + 治理门禁 (ADR-020 / ADR-024).

具体实现 (随 L1.2 子任务搬过来):
  - capability_writeback.py  ✅ Commit E · record_outcome / TaskOutcome (能力卡回写)

Gate Protocol (base.py) 在 L2 后会有具体类实现 (复合 capability_writeback
+ runtime_capabilities 表读写 + promotion_queue + RCDH report 验证 +
ExternalSupervisor debrief 验证).
"""

from kun.agents.gate.base import Gate
from kun.agents.gate.capability_writeback import Outcome, TaskOutcome, record_outcome

__all__ = ["Gate", "Outcome", "TaskOutcome", "record_outcome"]
