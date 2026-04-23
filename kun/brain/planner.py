"""Task Planner (§7.1 L2) — break intent into sub-tasks.

Walking-skeleton: planner returns a trivial single-step plan. Future iterations
add proper decomposition via LLM call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kun.datamodel.task import TaskRef


class PlanStep(BaseModel):
    step_id: int
    description: str
    skill_hint: str | None = None
    estimated_cost_usd: float = 0.0
    estimated_duration_sec: float = 0.0


class ExecutionPlan(BaseModel):
    task_ref: TaskRef
    steps: list[PlanStep] = Field(default_factory=list)

    def total_estimated_cost(self) -> float:
        return sum(s.estimated_cost_usd for s in self.steps)


class TaskPlanner:
    """Decompose a task into steps. For now: minimal single-step plan."""

    def plan(self, task_ref: TaskRef) -> ExecutionPlan:
        # Walking skeleton: every task is one step. Future: LLM-driven planning.
        step = PlanStep(
            step_id=1,
            description=task_ref.meta.success_criteria_short,
            skill_hint=(
                task_ref.spec.required_skills[0]
                if task_ref.spec and task_ref.spec.required_skills
                else None
            ),
            estimated_cost_usd=task_ref.meta.estimated_cost_usd,
            estimated_duration_sec=task_ref.meta.estimated_duration_sec,
        )
        return ExecutionPlan(task_ref=task_ref, steps=[step])
