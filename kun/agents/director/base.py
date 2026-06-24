"""Director · 主线入口角色 (ADR-020).

Type      : 常驻 service
Line      : 主线 (5 阶段中的第 1 阶段)
Model     : 远程 (gpt-5.5)
Input     : 用户消息 + drift signals
Output    : TaskSpec + GoalAnchor + complexity + priority_profile + InputClassification
闭环      : 主线起点；anti-drift 入口

主要职责:
  1. 拆解用户消息为 TaskSpec (delegates: brain/intent + brain/planner)
  2. 输出 complexity (simple/medium/complex) + priority_profile (speed_first/cost_first)
  3. 生成 GoalAnchor (ADR-022 Layer 2)
  4. Long-task mode 下的 Input Classification (ADR-022 Layer 3, 6 类)

实施路径:
  L1.2 · 把 brain/intent + brain/planner + control_plane/mission_director 迁来
  L1.7 · 加 GoalAnchor / complexity / priority_profile 输出
  L2   · 加 Input Classifier + drift signal 接收
"""

from __future__ import annotations

from typing import Any, Protocol

from kun.core.logging import get_logger

log = get_logger("kun.agents.director")


class Director(Protocol):
    """主线入口 Protocol — 拆任务 / GoalAnchor / 输入分类."""

    async def interpret(
        self,
        user_message: str,
        *,
        owner: Any,  # Owner
    ) -> Any:  # TaskRef
        """拆解用户消息 → TaskSpec + GoalAnchor + complexity + priority_profile."""
        ...

    async def classify_input(
        self,
        new_input: str,
        *,
        goal_anchor: Any | None = None,
        source: str = "user",
    ) -> Any:  # InputClassification
        """长任务模式下的输入分类 (ADR-022 Layer 3)."""
        ...
