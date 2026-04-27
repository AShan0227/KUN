"""KUN-Lab 内测分区 (V2.2 §26).

独立分区, 单独算力跑高成本实验:
- ENSEMBLE 模式 (同任务 N 路径并发 + 投票选最优)
- Inference-Time Rethinking (V2.2 §27 - rethink 路径实验)
- CodeCapability 全开 + 多 judge (高成本 code 任务)
- OffTopicEval benchmark suite (V2.2 §28 - reject rate 评估)

设计原则:
- env 隔离: KUN_LAB_MODE=1 启用; 默认 off, 不影响生产 KUN
- 跟主仓库共用 V2.2 心脏 (StrategyMatcher / ValueGate / hermes / etc) — 不重建
- 实验日志 → ExperimentLog → RecipePromoter → KnowledgePrecipitation → 推主仓库
- 实验失败 / 高成本不归用户账, 走单独 lab 预算

启动: 见 docs/v2/KUN-V2.2-revisions.md §26.
"""

from kun.lab.adoption import (
    LabAdopter,
    LabRecipeAdoptionStep,
    get_adoption_step,
    install_lab_adoption_step,
    reset_adoption_step,
)
from kun.lab.cursor_storage import (
    CursorSnapshot,
    CursorStorage,
    InMemoryCursorStorage,
    SqlCursorStorage,
)
from kun.lab.ensemble_executor import (
    EnsembleConfig,
    EnsembleExecutor,
    EnsemblePathResult,
    EnsembleResult,
)
from kun.lab.events import LabEventEmitter, summarize_ensemble, summarize_promotion
from kun.lab.experiment_log import (
    Experiment,
    ExperimentLog,
    get_experiment_log,
    reset_experiment_log,
)
from kun.lab.llm_router_adapter import LLMRouterEnsembleAdapter, make_default_adapter
from kun.lab.precipitation_bridge import (
    HIGH_CONFIDENCE_WIN_RATE,
    LabRecipePrecipitationStep,
    install_lab_kp_bridge,
    make_kp_adopter,
)
from kun.lab.recipe_promoter import RecipePromoter
from kun.lab.recipe_registry import (
    MIN_CONFIDENCE_FOR_REGISTRY,
    LabRecipeEntry,
    LabRecipeRegistry,
    get_recipe_registry,
    make_registry_apply_hook,
    reset_recipe_registry,
)

__all__ = [
    "HIGH_CONFIDENCE_WIN_RATE",
    "MIN_CONFIDENCE_FOR_REGISTRY",
    "CursorSnapshot",
    "CursorStorage",
    "EnsembleConfig",
    "EnsembleExecutor",
    "EnsemblePathResult",
    "EnsembleResult",
    "Experiment",
    "ExperimentLog",
    "InMemoryCursorStorage",
    "LLMRouterEnsembleAdapter",
    "LabAdopter",
    "LabEventEmitter",
    "LabRecipeAdoptionStep",
    "LabRecipeEntry",
    "LabRecipePrecipitationStep",
    "LabRecipeRegistry",
    "RecipePromoter",
    "SqlCursorStorage",
    "get_adoption_step",
    "get_experiment_log",
    "get_recipe_registry",
    "install_lab_adoption_step",
    "install_lab_kp_bridge",
    "make_default_adapter",
    "make_kp_adopter",
    "make_registry_apply_hook",
    "reset_adoption_step",
    "reset_experiment_log",
    "reset_recipe_registry",
    "summarize_ensemble",
    "summarize_promotion",
]
