"""启 (Qi) — 能力进化系统 (V7 §9.6).

V7 命名约定 (§5.1): 启 (Qi) 是 KUN 6.6 能力进化系统的负责人, 主理 capability
lifecycle / strategy replay / process audit / capability candidate.

V7 命名迁移 Phase A: 本 module 是 ``kun.agents.strategist`` 的 re-export 包,
新代码统一 import ``kun.agents.qi``, 旧代码继续可用 ``kun.agents.strategist``
(渐进迁移, 不一次大改). Phase D 后允许 deprecate 旧路径。

未来定位 (V7 §5.4): 启可能独立作为 agent 产品发布, 本 module path 是预留的
对外接口边界。

公共 API (re-export from ``kun.agents.strategist``):

    from kun.agents.qi import StrategistService, Strategy Experiment, ...

或新代码用别名:

    from kun.agents.qi import Qi  # alias for StrategistService
"""

from __future__ import annotations

from kun.agents.strategist import (
    CandidateCluster,
    CapabilityHistoryReader,
    ExperimentEmitter,
    ExplorerMode,
    ExplorerPoolConfig,
    Strategist,
    StrategistService,
    StrategyExperiment,
    cluster_by_kind,
    deduplicate,
    deliberate,
    experiment_as_dict,
    jaccard_similarity,
    load_explorer_pool_config,
    rank_candidates,
    select_repair_direction,
)

# Public alias — 启 (Qi) 作为对外角色名, 内部实现 StrategistService.
# 后续启独立发布时, ``class Qi`` 是 public API class name.
Qi = StrategistService

__all__ = [
    "CandidateCluster",
    "CapabilityHistoryReader",
    "ExperimentEmitter",
    "ExplorerMode",
    "ExplorerPoolConfig",
    "Qi",
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
