"""Tester · 主线尾角色 (ADR-020).

Type      : task-bound (验证阶段实例化)
Line      : 主线 (5 阶段中的第 4 阶段)
Model     : 远程 (gpt-5.5)
Input     : Artifact
Output    : TestReport
闭环      : Gate 准入证据

主要职责:
  1. 跑 ValidationPipeline (ADR-018 §16.2, 半合并状态)
  2. 集成 SingleJudge / MultiJudge / DebateValidator
  3. 输出 TestReport (含 score / pass_ / details)
  4. RSI 实验场景下 (ADR-024 step 7-8): 跑 Learning Signal 收集
  5. 注意: Tester 是"角色", ValidationPipeline 是"工具" — 命名错开

实施路径:
  L1.2 · 把 engineering/validation.py 迁来
  L2   · 加 Learning Signal 独立收集 (RSI step 8)
  L2   · ValidationPipeline 调用方达 ≥ 3 (真合并)
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.tester")


class Tester(Protocol):
    """主线尾 Protocol — 验证 Artifact + 输出 TestReport."""

    async def validate(
        self,
        artifact: Any,
        *,
        task_meta: Any,  # TaskMeta
        goal: str | None = None,
    ) -> Any:  # TestReport
        """跑验证 pipeline → TestReport (含 score / pass_ / evidence)."""
        ...
