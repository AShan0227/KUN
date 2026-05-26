"""Director agent — 主线入口 (ADR-020).

具体实现 (随 L1.2 子任务逐步搬过来):
  - intent.py   ✅ Commit A · IntentInterpreter (从 kun.brain.intent 搬来)
  - planner.py  ✅ Commit B · TaskPlanner + PlanStep + ExecutionPlan
  - role_router.py ⏳ Commit C · TaskRouter (待迁, 重命名避免与 LLMRouter 冲突)

Director Protocol (base.py) 在 L1.7 后会有具体类实现 (复合 IntentInterpreter +
TaskPlanner + GoalAnchor 生成 + Input Classification).
"""

from kun.agents.director.base import Director
from kun.agents.director.intent import IntentInterpreter
from kun.agents.director.planner import ExecutionPlan, PlanStep, TaskPlanner

__all__ = [
    "Director",
    "ExecutionPlan",
    "IntentInterpreter",
    "PlanStep",
    "TaskPlanner",
]
