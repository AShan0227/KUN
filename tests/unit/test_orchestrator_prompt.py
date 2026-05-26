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


# ===================== L1.8 + L1.9 · GoalAnchor pinning + Anti-sycophancy =====================


@pytest.mark.unit
def test_system_prompt_short_task_no_goal_anchor():
    """没 GoalAnchor (simple task) → system prompt 不含 anchor / anti-sycophancy 段."""
    from kun.engineering.orchestrator import _build_executor_system_prompt

    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("hi", owner),
        task_type="general.default",
        owner=owner,
        success_criteria_short="say hello",
    )
    task_ref = TaskRef(meta=meta)  # no goal_anchor

    prompt = _build_executor_system_prompt(
        task_ref=task_ref,
        audience_directive="developer style",
    )

    assert "GOAL ANCHOR" not in prompt
    assert "长任务执行规则" not in prompt
    assert "developer style" in prompt
    assert "KUN 系统里的执行角色" in prompt


@pytest.mark.unit
def test_system_prompt_long_task_pins_anchor_at_top():
    """长任务 (有 GoalAnchor) → system prompt 顶部 pin GoalAnchor + anti-sycophancy 段."""
    from kun.agents.director.anchor import GoalAnchor
    from kun.engineering.orchestrator import _build_executor_system_prompt

    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("refactor module X", owner),
        task_type="coding.refactor",
        complexity="complex",
        priority_profile="speed_first",
        owner=owner,
        success_criteria_short="all tests pass",
        estimated_steps=6,
    )
    anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement="refactor module X to use new pattern",
        success_criteria=["all tests pass", "no regression"],
        out_of_scope=["touching unrelated modules"],
        invariants=["public API unchanged"],
    )
    task_ref = TaskRef(meta=meta, goal_anchor=anchor)

    prompt = _build_executor_system_prompt(
        task_ref=task_ref,
        audience_directive="developer style",
    )

    # 顶部 pin: GoalAnchor render 在最前面
    assert prompt.startswith("═══ GOAL ANCHOR")
    # GoalAnchor 内容真的出现
    assert "refactor module X to use new pattern" in prompt
    assert "all tests pass" in prompt
    assert "touching unrelated modules" in prompt
    assert "public API unchanged" in prompt
    # Anti-sycophancy 段在 GoalAnchor 之后, base directive 之前
    anchor_pos = prompt.find("═══ GOAL ANCHOR")
    antisyc_pos = prompt.find("长任务执行规则")
    base_pos = prompt.find("KUN 系统里的执行角色")
    assert anchor_pos < antisyc_pos < base_pos
    # Anti-sycophancy 关键短语都在
    assert "不要" in prompt and "主动满足" in prompt
    assert "needs_user_confirmation" in prompt


@pytest.mark.unit
def test_system_prompt_long_task_with_skills_and_context():
    """长任务 + skills + context: 顺序应该是 anchor → anti-syc → base → audience → skills → context."""
    from kun.agents.director.anchor import GoalAnchor
    from kun.engineering.orchestrator import _build_executor_system_prompt

    owner = Owner(tenant_id="u-sylvan")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("complex task", owner),
        task_type="coding.refactor",
        complexity="complex",
        owner=owner,
        success_criteria_short="finish refactor",
    )
    anchor = GoalAnchor(task_id=meta.task_id, goal_statement="finish refactor")
    task_ref = TaskRef(meta=meta, goal_anchor=anchor)

    prompt = _build_executor_system_prompt(
        task_ref=task_ref,
        audience_directive="developer style",
        skills_summary="可用 skill: pdf-read, csv-query",
        skill_directive="如果检测到 csv 文件, 用 csv-query",
        context_summary="相关 context: 项目 ABC 风格指南",
    )

    # 顺序断言
    positions = {
        "anchor": prompt.find("═══ GOAL ANCHOR"),
        "antisyc": prompt.find("长任务执行规则"),
        "base": prompt.find("KUN 系统里的执行角色"),
        "audience": prompt.find("developer style"),
        "skills": prompt.find("可用 skill"),
        "skill_dir": prompt.find("如果检测到 csv 文件"),
        "context": prompt.find("相关 context"),
    }
    # 所有 section 都存在
    assert all(v >= 0 for v in positions.values()), positions
    # 顺序: anchor < antisyc < base < audience < skills < skill_dir < context
    ordered = [
        "anchor",
        "antisyc",
        "base",
        "audience",
        "skills",
        "skill_dir",
        "context",
    ]
    for prev, nxt in zip(ordered[:-1], ordered[1:], strict=True):
        assert positions[prev] < positions[nxt], (
            f"order broken: {prev}@{positions[prev]} should precede {nxt}@{positions[nxt]}"
        )
