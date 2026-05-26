"""Gate agent — 主线出口 + 治理门禁 (ADR-020 / ADR-024).

  base.py                  · Gate Protocol (角色契约)
  capability_writeback.py  · record_outcome / TaskOutcome (能力卡回写, L1.2)
  service.py               · GateService 准入门禁 + runtime_capabilities 写入 (L2.8)
"""

from kun.agents.gate.base import Gate
from kun.agents.gate.capability_writeback import Outcome, TaskOutcome, record_outcome
from kun.agents.gate.service import (
    CapabilityWriter,
    GateDecision,
    GateService,
    decision_as_dict,
)

__all__ = [
    "CapabilityWriter",
    "Gate",
    "GateDecision",
    "GateService",
    "Outcome",
    "TaskOutcome",
    "decision_as_dict",
    "record_outcome",
]
