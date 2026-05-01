"""Memory policy tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.datamodel.task import Owner, Risk, RiskLevel, TaskMeta, TaskRef, TaskSpec
from kun.memory.policy import MemoryDepth, MemoryLayer, decide_memory_policy


def _task(
    *,
    goal: str,
    task_type: str = "general.todo",
    risk_level: RiskLevel = "low",
    complexity_score: float = 0.3,
    required_skills: list[str] | None = None,
    foreseen_risks: list[Risk] | None = None,
) -> TaskRef:
    owner = Owner(tenant_id="tenant-memory-policy")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint(goal, owner),
            task_type=task_type,
            risk_level=risk_level,
            complexity_score=complexity_score,
            owner=owner,
            success_criteria_short=goal,
        ),
        spec=TaskSpec(
            goal_detail=goal,
            success_metrics=["done"],
            required_skills=required_skills or [],
            foreseen_risks=foreseen_risks or [],
        ),
    )


@pytest.mark.unit
def test_simple_low_complexity_task_uses_no_memory() -> None:
    ticket = decide_memory_policy(
        _task(goal="回复用户一句确认收到", complexity_score=0.1),
    )

    assert ticket.use_memory is False
    assert ticket.depth == MemoryDepth.NO_MEMORY
    assert ticket.layers == []
    assert ticket.asset_kinds == []
    assert ticket.preferred_tags == []
    assert ticket.max_items == 0
    assert ticket.allow_mid_run_retrieval is False
    assert MemoryLayer.META_DECISION in ticket.avoid_layers
    assert "simple_low_complexity" in ticket.reason


@pytest.mark.unit
def test_bug_code_task_prefers_process_and_behavior_memory() -> None:
    ticket = decide_memory_policy(
        _task(
            goal="修复 pytest 报错并补回归测试",
            task_type="coding.python.pytest",
            complexity_score=0.5,
            required_skills=["coding-pytest"],
        ),
    )

    assert ticket.use_memory is True
    assert ticket.depth == MemoryDepth.TARGETED
    assert ticket.layers[:2] == [MemoryLayer.EXECUTION_PROCESS, MemoryLayer.BEHAVIOR]
    assert ticket.asset_kinds == ["memory", "methodology", "skill", "knowledge"]
    assert "repo" in ticket.preferred_tags
    assert "tests" in ticket.preferred_tags
    assert ticket.max_items == 3
    assert ticket.allow_mid_run_retrieval is False
    assert "code_or_bug_prefers_process_behavior" in ticket.reason


@pytest.mark.unit
def test_product_ops_strategy_task_prefers_meta_decision_and_methodology() -> None:
    ticket = decide_memory_policy(
        _task(
            goal="制定下月留存增长策略和产品运营实验",
            task_type="product.ops.retention",
            complexity_score=0.55,
        ),
        strategy_pack=SimpleNamespace(pack_id="product_ops"),
    )

    assert ticket.use_memory is True
    assert ticket.layers[:2] == [MemoryLayer.META_DECISION, MemoryLayer.METHODOLOGY]
    assert ticket.asset_kinds == ["memory", "methodology", "knowledge", "skill", "role_template"]
    assert "product" in ticket.preferred_tags
    assert "growth" in ticket.preferred_tags
    assert ticket.max_items == 3
    assert ticket.allow_mid_run_retrieval is True
    assert "strategy_ops_prefers_meta_methodology" in ticket.reason
    assert "strategy_pack=product_ops" in ticket.reason


@pytest.mark.unit
def test_high_risk_task_uses_few_precise_memories_and_marks_risk() -> None:
    ticket = decide_memory_policy(
        _task(
            goal="生产环境支付链路变更前制定回滚方案",
            task_type="coding.backend.payments",
            risk_level="high",
            complexity_score=0.8,
            foreseen_risks=[Risk(description="错误变更会影响付款", severity="high")],
        ),
        watchtower_decision=SimpleNamespace(
            strategy_pack_id="coding",
            alert_flags=["high_estimated_cost"],
        ),
    )

    assert ticket.use_memory is True
    assert ticket.depth == MemoryDepth.TARGETED
    assert ticket.risk is True
    assert ticket.max_items == 2
    assert len(ticket.layers) <= 2
    assert "skill" in ticket.asset_kinds
    assert MemoryLayer.BEHAVIOR in ticket.avoid_layers
    assert ticket.allow_mid_run_retrieval is True
    assert "task_risk_high" in ticket.risk_flags
    assert "foreseen_high_risk" in ticket.risk_flags
    assert "high_estimated_cost" in ticket.risk_flags
    assert "risk_flags=" in ticket.reason


@pytest.mark.unit
def test_strategy_pack_context_tags_flow_into_memory_policy() -> None:
    ticket = decide_memory_policy(
        _task(
            goal="给教育产品设计下周课程复习路径",
            task_type="education.course.plan",
            complexity_score=0.45,
        ),
        strategy_pack=SimpleNamespace(
            pack_id="education",
            context_tags=["education", "learning_profile"],
            methodology_refs=["spaced_repetition"],
        ),
    )

    assert ticket.use_memory is True
    assert "education" in ticket.preferred_tags
    assert "learning_profile" in ticket.preferred_tags
    assert "spaced_repetition" in ticket.preferred_tags
    assert "preferred_tags=" in ticket.reason
    kwargs = ticket.as_context_packer_kwargs()
    assert kwargs["kinds"]
    assert kwargs["preferred_tags"]
    assert kwargs["high_risk_task"] is False


@pytest.mark.unit
def test_high_risk_memory_policy_ticket_marks_context_packer_kwargs() -> None:
    ticket = decide_memory_policy(
        _task(
            goal="生产环境支付链路变更前制定回滚方案",
            task_type="coding.backend.payments",
            risk_level="high",
            complexity_score=0.8,
        ),
    )

    kwargs = ticket.as_context_packer_kwargs()

    assert ticket.risk is True
    assert kwargs["high_risk_task"] is True
