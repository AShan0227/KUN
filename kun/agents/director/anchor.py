"""GoalAnchor — 长任务防漂移的 immutable 目标锚 (ADR-022).

Director 在 long-task mode 下生成 GoalAnchor; Executor 每次 LLM call 顶部
强制 pin 这个 anchor 进 system prompt. 新的用户消息**不会**覆盖它.

5 层 Anti-drift 工程化约束 (ADR-022) 的 Layer 2.

数据流:
  Director.interpret() → GoalAnchor (pydantic)
                       → kun.governance.goal_anchors 写入 DB (alembic 0011)
                       → Executor.run() 从 DB 读 + system prompt 顶部 pin
                       → 每次 LLM call 都看到 anchor
                       → Supervisor 周期性 plan_review 时 verify anchor 不变

ORM 对应: GoalAnchorRow (kun.core.orm).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kun.core.ids import new_id


class GoalAnchor(BaseModel):
    """长任务 immutable 目标锚 — pin 在 Executor system prompt 顶部.

    Constraints:
      - goal_statement: ≤ 200 字符 (DB 层也有 check constraint)
      - success_criteria: 3-5 条可验证条件
      - out_of_scope: 明确"本任务不做什么"(scope_expansion 分类器用)
      - invariants: 全程必须保持的不变量
      - immutable=True 默认; 不允许新指令覆盖
    """

    model_config = ConfigDict(extra="forbid")

    anchor_id: str = Field(default_factory=lambda: new_id("goal_anchor"))
    task_id: str
    goal_statement: str = Field(max_length=200, min_length=1)
    success_criteria: list[str] = Field(default_factory=list, max_length=10)
    out_of_scope: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    immutable: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("goal_statement")
    @classmethod
    def _goal_not_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("goal_statement must not be empty after strip")
        return stripped

    def render_for_system_prompt(self) -> str:
        """渲染为 system prompt 顶部段 (Executor 在 long-task mode 下用)."""
        lines = [
            "═══ GOAL ANCHOR (immutable, pinned, do not override) ═══",
            f"GOAL: {self.goal_statement}",
        ]
        if self.success_criteria:
            lines.append("SUCCESS CRITERIA:")
            lines.extend(f"  - {c}" for c in self.success_criteria)
        if self.out_of_scope:
            lines.append("OUT OF SCOPE (refuse if user requests these):")
            lines.extend(f"  - {item}" for item in self.out_of_scope)
        if self.invariants:
            lines.append("INVARIANTS (must hold throughout):")
            lines.extend(f"  - {inv}" for inv in self.invariants)
        lines.append("═" * 60)
        return "\n".join(lines)


__all__ = ["GoalAnchor"]
