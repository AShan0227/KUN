"""TaskPlanner tests."""

from __future__ import annotations

import json

import pytest
from kun.brain.planner import PlanStep, TaskPlanner
from kun.datamodel.task import Constraint, Owner, Risk, TaskMeta, TaskRef, TaskSpec
from kun.interface.llm.base import LLMRequest, LLMResponse


def _meta(*, risk: str = "low", complexity: float = 0.3) -> TaskMeta:
    owner = Owner(tenant_id="u-sylvan")
    return TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("planner", owner),
        task_type="coding.python.fastapi",
        risk_level=risk,
        complexity_score=complexity,
        owner=owner,
        estimated_cost_usd=0.12,
        estimated_duration_sec=60,
        success_criteria_short="完成接口改造",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_keeps_single_step_without_spec() -> None:
    task = TaskRef(meta=_meta())
    plan = await TaskPlanner().plan(task)

    assert len(plan.steps) == 1
    assert plan.steps[0].description == "完成接口改造"
    assert plan.steps[0].estimated_cost_usd == 0.12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_uses_task_spec_for_multi_step_plan() -> None:
    spec = TaskSpec(
        goal_detail="实现结果持久化并保证重复请求返回旧答案",
        success_metrics=["重复请求返回 cached answer", "原有 API 响应不变"],
        required_skills=["coding-pytest", "coding-sqlalchemy"],
        constraints=[Constraint(kind="no_irreversible", detail="不能破坏已有任务数据")],
        foreseen_risks=[
            Risk(
                description="数据库迁移和 ORM 不一致",
                severity="medium",
                mitigation_hint="补迁移和测试",
            )
        ],
    )
    task = TaskRef(meta=_meta(risk="high", complexity=0.7), spec=spec)

    plan = await TaskPlanner().plan(task)

    assert [step.skill_hint for step in plan.steps] == [
        "task.boundary_check",
        "coding-pytest",
        "coding-sqlalchemy",
        "task.validation",
    ]
    assert "约束" in plan.steps[0].description
    assert "cached answer" in plan.steps[-1].description
    assert sum(step.estimated_cost_usd for step in plan.steps) == pytest.approx(0.12)


class _FakeRouter:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[LLMRequest, str]] = []

    async def invoke(self, request: LLMRequest, *, purpose: str = "execution") -> LLMResponse:
        self.calls.append((request, purpose))
        return LLMResponse(content=self.content, model="stub", provider="fake", tier="top")


def _complex_task(*, content_hint: list[str] | None = None) -> TaskRef:
    spec = TaskSpec(
        goal_detail="审计复杂后端任务并拆成可验证步骤",
        success_metrics=["有计划", "有验证"],
        required_skills=["code-review"],
        constraints=[Constraint(kind="no_irreversible", detail="不能动生产数据")],
        subtasks_hint=content_hint or [],
    )
    return TaskRef(meta=_meta(risk="high", complexity=0.8), spec=spec)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_uses_llm_for_complex_task_and_validates_dag() -> None:
    router = _FakeRouter(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "description": "梳理目标和边界",
                        "skill_hint": "task.boundary_check",
                        "depends_on": [],
                    },
                    {
                        "step_id": 2,
                        "description": "审计实现",
                        "skill_hint": "code-review",
                        "depends_on": [1],
                    },
                ]
            },
            ensure_ascii=False,
        )
    )

    plan = await TaskPlanner().plan(_complex_task(), router=router)

    assert [step.depends_on for step in plan.steps] == [[], [1]]
    assert [step.skill_hint for step in plan.steps] == ["task.boundary_check", "code-review"]
    assert router.calls[0][1] == "planning"
    assert router.calls[0][0].profile is not None
    assert router.calls[0][0].profile.needs_reasoning is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_falls_back_when_llm_json_is_bad() -> None:
    router = _FakeRouter("not json")

    plan = await TaskPlanner().plan(_complex_task(), router=router)

    assert [step.skill_hint for step in plan.steps] == [
        "task.boundary_check",
        "code-review",
        "task.validation",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_does_not_call_llm_for_low_complexity_task() -> None:
    router = _FakeRouter('{"steps": [{"step_id": 1, "description": "LLM", "depends_on": []}]}')
    task = TaskRef(
        meta=_meta(complexity=0.2),
        spec=TaskSpec(goal_detail="小任务", success_metrics=[], required_skills=[]),
    )

    plan = await TaskPlanner().plan(task, router=router)

    assert router.calls == []
    assert plan.steps[0].description == "完成任务目标: 小任务"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_falls_back_when_llm_returns_cycle() -> None:
    router = _FakeRouter(
        json.dumps(
            {
                "steps": [
                    {"step_id": 1, "description": "A", "depends_on": [2]},
                    {"step_id": 2, "description": "B", "depends_on": [1]},
                ]
            }
        )
    )

    plan = await TaskPlanner().plan(_complex_task(), router=router)

    assert isinstance(plan.steps[0], PlanStep)
    assert [step.skill_hint for step in plan.steps] == [
        "task.boundary_check",
        "code-review",
        "task.validation",
    ]
