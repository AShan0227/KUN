"""Task Execution Brain (legacy compat 层).

⚠️ 2026-05-26 起本目录退役 (ADR-020 L1.2 拆 orchestrator):
  - IntentInterpreter → kun.agents.director.intent
  - TaskPlanner       → kun.agents.director.planner (待 L1.2 Commit B 迁移)
  - TaskRouter        → kun.agents.director.role_router (待 L1.2 Commit C 迁移)

新代码请直接从 kun.agents.director 导入. 本模块仅保留 re-export 让既有
tests / orchestrator 不破裂; 待所有 importer 迁完后整个 kun/brain/ 可删.
"""

from kun.agents.director.intent import IntentInterpreter
from kun.brain.planner import TaskPlanner  # noqa: F401  待 Commit B 迁移
from kun.brain.router import TaskRouter  # noqa: F401  待 Commit C 迁移

__all__ = ["IntentInterpreter", "TaskPlanner", "TaskRouter"]
