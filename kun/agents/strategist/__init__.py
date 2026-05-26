"""Strategist agent — 监督线 on-demand (ADR-020).

  base.py    · Strategist Protocol (角色契约)
  service.py · StrategistService 工程化候选生成 (L2.7, 第一条 RSI 实例)
"""

from kun.agents.strategist.base import Strategist
from kun.agents.strategist.deliberation import (
    CandidateCluster,
    cluster_by_kind,
    deduplicate,
    deliberate,
    jaccard_similarity,
    rank_candidates,
)
from kun.agents.strategist.explorer_pool import (
    ExplorerMode,
    ExplorerPoolConfig,
    load_explorer_pool_config,
)
from kun.agents.strategist.service import (
    CapabilityHistoryReader,
    ExperimentEmitter,
    StrategistService,
    StrategyExperiment,
    experiment_as_dict,
    select_repair_direction,
)

__all__ = [
    "CandidateCluster",
    "CapabilityHistoryReader",
    "ExperimentEmitter",
    "ExplorerMode",
    "ExplorerPoolConfig",
    "Strategist",
    "StrategistService",
    "StrategyExperiment",
    "cluster_by_kind",
    "deduplicate",
    "deliberate",
    "experiment_as_dict",
    "jaccard_similarity",
    "load_explorer_pool_config",
    "rank_candidates",
    "select_repair_direction",
]
