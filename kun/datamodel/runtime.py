"""RuntimeState (§13.4) — 和 TASK.md 严格分离.

TASK.md = 身份证, 不可变.
RuntimeState = 进度, 可变.

存储: Redis (热数据) + PostgreSQL (快照, 每 N 步或状态变化时).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id

TaskStatus = Literal["queued", "running", "paused", "done", "failed", "cancelled"]


class StepRecord(BaseModel):
    """已完成步骤的记录."""

    model_config = ConfigDict(extra="forbid")

    step_id: int
    skill_used: str
    output_ref: str | None = None
    cost_usd_equivalent: float = 0.0
    cost_usd_actual: float = 0.0
    duration_sec: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


class NextStepPlan(BaseModel):
    """下一步将做什么."""

    skill: str
    input_preview: str = Field(max_length=500)
    estimated_cost_usd: float = 0.0


class LockHeld(BaseModel):
    """当前持有的资源锁."""

    resource: str
    acquired_at: datetime
    ttl_sec: int = 10


class Checkpoint(BaseModel):
    """中途质量检查点."""

    checkpoint_id: int
    at_step: int
    quality_score: float = Field(ge=0.0, le=1.0)
    notes: str = ""
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeState(BaseModel):
    """运行时状态. 可变, 每步或状态变化时快照到 Postgres."""

    model_config = ConfigDict(extra="forbid")

    state_id: str = Field(default_factory=lambda: new_id("runtime"))
    task_ref: str = Field(description="tk-... TASK.md id")
    current_step: int = 0
    total_planned_steps: int = 1
    status: TaskStatus = "queued"

    completed_steps: list[StepRecord] = Field(default_factory=list)
    next_step_plan: NextStepPlan | None = None
    locks_held: list[LockHeld] = Field(default_factory=list)

    # ADR-008: 两种成本字段并存
    accumulated_cost_usd_actual: float = 0.0
    accumulated_cost_usd_equivalent: float = 0.0
    accumulated_tokens: int = 0

    checkpoints: list[Checkpoint] = Field(default_factory=list)
    failures_this_run: int = 0

    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ---- helpers ----

    def accumulate_step(self, step: StepRecord) -> None:
        """Add a step and update aggregates."""
        self.completed_steps.append(step)
        self.current_step = step.step_id
        self.accumulated_cost_usd_actual += step.cost_usd_actual
        self.accumulated_cost_usd_equivalent += step.cost_usd_equivalent
        self.accumulated_tokens += step.tokens_in + step.tokens_out
        self.last_updated = datetime.now(UTC)

    def over_budget(self, estimated_cost_usd: float, multiplier: float = 1.2) -> bool:
        """True if accumulated equivalent cost exceeds estimated * multiplier."""
        if estimated_cost_usd <= 0:
            return False
        return self.accumulated_cost_usd_equivalent > estimated_cost_usd * multiplier
