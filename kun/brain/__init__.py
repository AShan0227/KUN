"""Task Execution Brain — legacy compat re-export 层 (deprecated).

⚠️ 2026-05-26 起本目录退役 (ADR-020 L1.2 拆 orchestrator). 全部内容已迁:
  - IntentInterpreter → kun.agents.director.intent ✅
  - TaskPlanner       → kun.agents.director.planner ✅
  - TaskRouter        → kun.agents.director.role_router ✅ (改名避免与 LLMRouter 撞)

新代码请直接从 kun.agents.director 导入. 待所有 importer 迁完 (grep
"from kun.brain" 全 0 命中) 后, 整个 kun/brain/ 目录可删.
"""

from kun.agents.director.intent import IntentInterpreter
from kun.agents.director.planner import TaskPlanner
from kun.agents.director.role_router import TaskRouter

__all__ = ["IntentInterpreter", "TaskPlanner", "TaskRouter"]
