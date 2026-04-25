"""Task Planner (§7.1 L2) — break intent into sub-tasks.

This planner is intentionally conservative: it uses the structured TaskSpec
first and only falls back to a single direct step when the task has no useful
L2 blueprint yet.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from kun.core.logging import get_logger
from kun.datamodel.runtime import validate_dag
from kun.datamodel.task import TaskRef
from kun.interface.llm.base import LLMMessage, LLMRequest, LLMResponse, TaskProfile

log = get_logger("kun.brain.planner")

_PLANNER_SYSTEM_PROMPT = """你是 Genesis 的任务拆解层。
只输出严格 JSON，不要 Markdown，不要解释。
目标：把用户任务拆成一个小型 DAG。每个 step 都必须可执行、可验证、边界清楚。
JSON schema:
{
  "steps": [
    {
      "step_id": 1,
      "description": "要做什么",
      "skill_hint": "可选技能或工具名",
      "depends_on": []
    }
  ]
}
规则:
- step_id 使用正整数，不能重复。
- depends_on 只能引用已存在的 step_id。
- 不要超过 8 步。
- 高风险/有约束任务，第一步必须先核对边界、风险和成功标准。
"""


class PlanStep(BaseModel):
    step_id: int
    description: str
    skill_hint: str | None = None
    depends_on: list[int] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    estimated_duration_sec: float = 0.0


class ExecutionPlan(BaseModel):
    task_ref: TaskRef
    steps: list[PlanStep] = Field(default_factory=list)

    def total_estimated_cost(self) -> float:
        return sum(s.estimated_cost_usd for s in self.steps)


class TaskPlanner:
    """Decompose a task into a small, deterministic execution plan."""

    async def plan(
        self,
        task_ref: TaskRef,
        *,
        router: Any | None = None,
    ) -> ExecutionPlan:
        # OTel: business-level span around planning so we can graph
        # task_type → step count distribution and find where heuristics
        # collapse to single_step (tells us where to invest L2 blueprints).
        from opentelemetry import trace

        tracer = trace.get_tracer("kun.brain.planner")
        with tracer.start_as_current_span("kun.planner.plan") as span:
            span.set_attribute("kun.task_id", task_ref.meta.task_id)
            span.set_attribute("kun.task_type", task_ref.meta.task_type)
            span.set_attribute("kun.complexity_score", task_ref.meta.complexity_score)
            span.set_attribute("kun.has_spec", task_ref.spec is not None)
            span.set_attribute("kun.has_subtasks_hint", bool(_subtasks_hint(task_ref)))
            via_llm = False
            fallback_reason = ""
            if router is not None and self._should_use_llm(task_ref):
                try:
                    plan = await self.plan_via_llm(task_ref, router=router)
                    via_llm = True
                except Exception as exc:
                    fallback_reason = type(exc).__name__
                    log.warning(
                        "planner.llm_failed_fallback",
                        task_id=task_ref.meta.task_id,
                        error=repr(exc),
                    )
                    plan = self._plan_inner(task_ref)
            else:
                plan = self._plan_inner(task_ref)
            span.set_attribute("kun.planner.via_llm", via_llm)
            if fallback_reason:
                span.set_attribute("kun.planner.fallback_reason", fallback_reason)
            span.set_attribute("kun.steps_planned", len(plan.steps))
            span.set_attribute("kun.estimated_cost_usd", plan.total_estimated_cost())
            return plan

    async def plan_via_llm(self, task_ref: TaskRef, *, router: Any) -> ExecutionPlan:
        """Ask the LLM routing layer to produce a JSON execution DAG."""
        response: LLMResponse = await router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_PLANNER_SYSTEM_PROMPT, cache=True),
                    LLMMessage(role="user", content=_planner_user_prompt(task_ref)),
                ],
                profile=TaskProfile(
                    task_type=task_ref.meta.task_type,
                    risk_level=task_ref.meta.risk_level,
                    needs_reasoning=True,
                    max_cost_usd=task_ref.meta.estimated_cost_usd,
                    max_duration_sec=task_ref.meta.estimated_duration_sec,
                ),
                temperature=0.1,
                max_tokens=1200,
            ),
            purpose="planning",
        )
        return _parse_plan_json(response.content, task_ref)

    def _plan_inner(self, task_ref: TaskRef) -> ExecutionPlan:
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
        validate_dag({step.step_id: step.depends_on for step in steps})
        return ExecutionPlan(task_ref=task_ref, steps=steps)

    @staticmethod
    def _should_use_llm(task_ref: TaskRef) -> bool:
        return task_ref.meta.complexity_score >= 0.5 or bool(_subtasks_hint(task_ref))

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


def _subtasks_hint(task_ref: TaskRef) -> list[str]:
    if task_ref.spec is None:
        return []
    return task_ref.spec.subtasks_hint


def _planner_user_prompt(task_ref: TaskRef) -> str:
    spec = task_ref.spec
    payload: dict[str, Any] = {
        "task_id": task_ref.meta.task_id,
        "task_type": task_ref.meta.task_type,
        "risk_level": task_ref.meta.risk_level,
        "complexity_score": task_ref.meta.complexity_score,
        "success_criteria_short": task_ref.meta.success_criteria_short,
        "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
        "estimated_duration_sec": task_ref.meta.estimated_duration_sec,
    }
    if spec is not None:
        payload["spec"] = spec.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False)


def _parse_plan_json(content: str, task_ref: TaskRef) -> ExecutionPlan:
    raw = _extract_json_object(content)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("planner response must be a JSON object")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("planner response must contain non-empty steps")

    steps: list[PlanStep] = []
    seen: set[int] = set()
    for item in raw_steps[:8]:
        if not isinstance(item, dict):
            raise ValueError("planner step must be an object")
        step = PlanStep(
            step_id=int(item["step_id"]),
            description=str(item["description"]).strip(),
            skill_hint=(
                str(item["skill_hint"]).strip()
                if item.get("skill_hint") not in {None, ""}
                else None
            ),
            depends_on=[int(dep) for dep in item.get("depends_on", [])],
        )
        if step.step_id <= 0:
            raise ValueError("planner step_id must be positive")
        if not step.description:
            raise ValueError("planner step description must be non-empty")
        if step.step_id in seen:
            raise ValueError(f"duplicate planner step_id: {step.step_id}")
        seen.add(step.step_id)
        steps.append(step)

    validate_dag({step.step_id: step.depends_on for step in steps})
    TaskPlanner._spread_estimates(task_ref, steps)
    return ExecutionPlan(task_ref=task_ref, steps=steps)


def _extract_json_object(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    raise ValueError("planner response does not contain a JSON object")
