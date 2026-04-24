"""Task Planner (§7.1 L2) — break intent into sub-tasks.

This planner is intentionally conservative: it uses the structured TaskSpec
first and only falls back to a single direct step when the task has no useful
L2 blueprint yet.
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
    """Decompose a task into a small, deterministic execution plan."""

    def plan(self, task_ref: TaskRef) -> ExecutionPlan:
        if task_ref.spec is None:
            return ExecutionPlan(task_ref=task_ref, steps=[self._single_step(task_ref)])

        steps: list[PlanStep] = []
        spec = task_ref.spec

        needs_boundary_check = (
            bool(spec.constraints)
            or bool(spec.foreseen_risks)
            or task_ref.meta.risk_level in {"high", "critical"}
            or task_ref.meta.complexity_score >= 0.6
        )
        if needs_boundary_check:
            steps.append(
                PlanStep(
                    step_id=1,
                    description="核对任务目标、约束、风险和成功标准，确认执行边界",
                    skill_hint="task.boundary_check",
                )
            )

        if spec.blocking_task_ids:
            steps.append(
                PlanStep(
                    step_id=len(steps) + 1,
                    description="确认依赖任务已完成: " + ", ".join(spec.blocking_task_ids),
                    skill_hint="task.dependency_check",
                )
            )

        if spec.required_skills:
            for skill_id in spec.required_skills[:3]:
                steps.append(
                    PlanStep(
                        step_id=len(steps) + 1,
                        description=f"使用技能 {skill_id} 推进目标: {spec.goal_detail}",
                        skill_hint=skill_id,
                    )
                )
        elif spec.required_tools:
            for tool in spec.required_tools[:3]:
                steps.append(
                    PlanStep(
                        step_id=len(steps) + 1,
                        description=f"使用工具 {tool} 推进目标: {spec.goal_detail}",
                        skill_hint=f"tool.{tool}",
                    )
                )
        else:
            steps.append(
                PlanStep(
                    step_id=len(steps) + 1,
                    description=f"完成任务目标: {spec.goal_detail}",
                )
            )

        if spec.success_metrics:
            metrics_preview = "; ".join(spec.success_metrics[:3])
            steps.append(
                PlanStep(
                    step_id=len(steps) + 1,
                    description=f"按可验证指标检查并交付结果: {metrics_preview}",
                    skill_hint="task.validation",
                )
            )

        steps = steps[:5]
        if not steps:
            steps = [self._single_step(task_ref)]

        self._spread_estimates(task_ref, steps)
        return ExecutionPlan(task_ref=task_ref, steps=steps)

    @staticmethod
    def _single_step(task_ref: TaskRef) -> PlanStep:
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
        return step

    @staticmethod
    def _spread_estimates(task_ref: TaskRef, steps: list[PlanStep]) -> None:
        if not steps:
            return
        cost_each = task_ref.meta.estimated_cost_usd / len(steps)
        duration_each = task_ref.meta.estimated_duration_sec / len(steps)
        for step in steps:
            step.estimated_cost_usd = cost_each
            step.estimated_duration_sec = duration_each
