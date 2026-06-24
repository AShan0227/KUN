"""Tester agent — 主线尾 (ADR-020).

具体实现 (随 L1.2 子任务搬过来):
  - validation.py   ✅ Commit D · ValidationPipeline + SingleJudge + DebateValidator + pick_tier
  - multi_judge.py  ✅ Commit D · jury_evaluate (MultiJudge 底层)

Tester Protocol (base.py) 在 L2 后会有具体类实现 (复合 ValidationPipeline +
Learning Signal 收集).
"""

from kun.agents.tester.base import Tester
from kun.agents.tester.multi_judge import jury_evaluate
from kun.agents.tester.validation import (
    MultiJudge,
    SingleJudge,
    ValidationPipeline,
    ValidationResult,
    pick_tier,
)

__all__ = [
    "MultiJudge",
    "SingleJudge",
    "Tester",
    "ValidationPipeline",
    "ValidationResult",
    "jury_evaluate",
    "pick_tier",
]
