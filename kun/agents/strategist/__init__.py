"""Strategist agent — 监督线 on-demand (ADR-020).

  base.py    · Strategist Protocol (角色契约)
  service.py · StrategistService 工程化候选生成 (L2.7, 第一条 RSI 实例)
"""

from kun.agents.strategist.base import Strategist
from kun.agents.strategist.service import (
    CapabilityHistoryReader,
    ExperimentEmitter,
    StrategistService,
    StrategyExperiment,
    experiment_as_dict,
    select_repair_direction,
)

__all__ = [
    "CapabilityHistoryReader",
    "ExperimentEmitter",
    "Strategist",
    "StrategistService",
    "StrategyExperiment",
    "experiment_as_dict",
    "select_repair_direction",
]
