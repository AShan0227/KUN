"""TaskPlanner tests."""

from __future__ import annotations

import pytest
from kun.brain.planner import TaskPlanner
from kun.datamodel.task import Constraint, Owner, Risk, TaskMeta, TaskRef, TaskSpec


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
def test_planner_keeps_single_step_without_spec() -> None:
    task = TaskRef(meta=_meta())
    plan = TaskPlanner().plan(task)

    assert len(plan.steps) == 1
    assert plan.steps[0].description == "完成接口改造"
    assert plan.steps[0].estimated_cost_usd == 0.12


@pytest.mark.unit
def test_planner_uses_task_spec_for_multi_step_plan() -> None:
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

    plan = TaskPlanner().plan(task)

    assert [step.skill_hint for step in plan.steps] == [
        "task.boundary_check",
        "coding-pytest",
        "coding-sqlalchemy",
        "task.validation",
    ]
    assert "约束" in plan.steps[0].description
    assert "cached answer" in plan.steps[-1].description
    assert sum(step.estimated_cost_usd for step in plan.steps) == pytest.approx(0.12)
