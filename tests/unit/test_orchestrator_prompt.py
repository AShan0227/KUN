"""Execution prompt assembly tests."""

from __future__ import annotations

import pytest
from kun.datamodel.task import Constraint, Owner, Risk, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import _execution_user_prompt


@pytest.mark.unit
def test_execution_prompt_includes_task_spec_context() -> None:
    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("build report", owner),
        task_type="writing.report",
        risk_level="medium",
        owner=owner,
        success_criteria_short="生成可交付报告",
    )
    spec = TaskSpec(
        goal_detail="基于原始数据生成一份销售报告",
        success_metrics=["包含同比", "包含风险提示"],
        required_tools=["csv_reader"],
        external_resources=["crm export"],
        constraints=[Constraint(kind="no_external_paid_api", detail="不能调用外部付费 API")],
        foreseen_risks=[
            Risk(
                description="数据可能缺字段",
                severity="medium",
                mitigation_hint="缺字段时先说明",
            )
        ],
        fallback_plan="输出缺失字段清单",
    )

    prompt = _execution_user_prompt(TaskRef(meta=meta, spec=spec), "整理报告")

    assert "整理报告" in prompt
    assert "基于原始数据生成一份销售报告" in prompt
    assert "包含同比" in prompt
    assert "不能调用外部付费 API" in prompt
    assert "数据可能缺字段" in prompt
    assert "输出缺失字段清单" in prompt


@pytest.mark.unit
def test_execution_prompt_carries_prior_step_outputs() -> None:
    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("build report", owner),
        task_type="writing.report",
        risk_level="low",
        owner=owner,
        success_criteria_short="生成可交付报告",
    )

    prompt = _execution_user_prompt(
        TaskRef(meta=meta),
        "复核并交付",
        prior_outputs=[
            (1, "已经完成数据读取"),
            (2, "发现 3 个异常值，需要在结论里提示"),
        ],
    )

    assert "已完成步骤输出摘要" in prompt
    assert "已经完成数据读取" in prompt
    assert "3 个异常值" in prompt
