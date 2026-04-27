"""启 (Qi) — KUN V2.3 实验室模式.

启是鲲的子模式, 不是独立 service. 共享所有核心 (LLM router / Skill / DB).
但启的高成本逻辑 (Darwin Gödel / 大量 ensemble / AI Scientist v2 树搜索)
严格 gate 在 qi_window_active() 检查里, 默认日常关闭.

跟 V2.2 kun/lab/ 的关系:
- kun/lab/ = V2.2 启 MVP (EnsembleExecutor / RecipePromoter / etc.) — 仍 work
- kun/qi/  = V2.3 启 V3 (时间窗口 / 日预算 / Darwin Gödel / 协议涌现)
- kun/qi/ 复用 kun/lab/ 心脏 (EnsembleExecutor 等), 加 V2.3 新东西

启的输出 (经过 shadow → canary → stable 验证后下放鲲):
- protocol           ← 鲲怎么干活的"标准说明书" (V2.3 IP)
- prediction_model   ← Predictive Coding 预测模型
- skill_pheromone    ← Pheromone 涌现的 skill 偏好
"""

from kun.qi.budget import QiBudgetExhaustedError, QiDailyBudget, get_qi_budget
from kun.qi.protocol import (
    InMemoryProtocolStorage,
    Protocol,
    ProtocolExecution,
    ProtocolHermesTemplate,
    ProtocolRegistry,
    ProtocolSkillStep,
    ProtocolStatus,
    ProtocolStorage,
    ProtocolTrigger,
    ProtocolVerificationSpec,
    SqlProtocolStorage,
    get_protocol_registry,
    reset_protocol_registry,
)
from kun.qi.window import (
    QiWindowConfig,
    QiWindowError,
    is_qi_window_active,
    require_qi_active,
)

__all__ = [
    "InMemoryProtocolStorage",
    "Protocol",
    "ProtocolExecution",
    "ProtocolHermesTemplate",
    "ProtocolRegistry",
    "ProtocolSkillStep",
    "ProtocolStatus",
    "ProtocolStorage",
    "ProtocolTrigger",
    "ProtocolVerificationSpec",
    "QiBudgetExhaustedError",
    "QiDailyBudget",
    "QiWindowConfig",
    "QiWindowError",
    "SqlProtocolStorage",
    "get_protocol_registry",
    "get_qi_budget",
    "is_qi_window_active",
    "require_qi_active",
    "reset_protocol_registry",
]
