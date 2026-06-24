"""L2.2 · Input Classifier 6 类分流测试 (ADR-022 Layer 3)."""

from __future__ import annotations

import pytest
from kun.agents.director import GoalAnchor, classify_input


def _make_anchor(
    *,
    goal: str = "Refactor module X to use new pattern",
    success_criteria: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    invariants: list[str] | None = None,
) -> GoalAnchor:
    return GoalAnchor(
        task_id="tk-test",
        goal_statement=goal,
        success_criteria=success_criteria or ["all tests pass", "no regression"],
        out_of_scope=out_of_scope or [],
        invariants=invariants or [],
    )


@pytest.mark.unit
def test_interrupt_keyword_highest_priority():
    """interrupt 关键词命中, 不管 goal_anchor 是什么, 都归 interrupt."""
    anchor = _make_anchor(goal="anything")
    result = classify_input("stop! please halt this task", goal_anchor=anchor)
    assert result.category == "interrupt"
    assert result.confidence >= 0.9
    assert result.integration_hint["action"] == "cancel_task"


@pytest.mark.unit
def test_interrupt_chinese():
    """中文中断词."""
    anchor = _make_anchor()
    result = classify_input("算了，不要做了", goal_anchor=anchor)
    assert result.category == "interrupt"


@pytest.mark.unit
def test_explicit_pivot_with_pivot_keyword_and_low_overlap():
    """换方向关键词 + 与 goal 几乎无关 → explicit_pivot."""
    anchor = _make_anchor(goal="refactor authentication module")
    result = classify_input(
        "actually instead let's discuss cooking recipes", goal_anchor=anchor
    )
    assert result.category == "explicit_pivot"
    assert result.integration_hint["action"] == "pause_and_confirm"


@pytest.mark.unit
def test_explicit_pivot_chinese():
    """中文换方向关键词."""
    anchor = _make_anchor(goal="refactor authentication module")
    result = classify_input("换成做点其他事情吧", goal_anchor=anchor)
    assert result.category == "explicit_pivot"


@pytest.mark.unit
def test_on_topic_clarification_when_clarifying_with_overlap():
    """澄清关键词 + 与 goal 关键词有重叠 → on_topic_clarification."""
    anchor = _make_anchor(
        goal="refactor authentication module",
        success_criteria=["tests pass", "all auth flows work"],
    )
    result = classify_input(
        "actually let me clarify - I want the refactor to focus on jwt tokens",
        goal_anchor=anchor,
    )
    assert result.category == "on_topic_clarification"
    assert result.integration_hint["action"] == "integrate_and_refine_criteria"


@pytest.mark.unit
def test_scope_expansion_with_keyword_partial_overlap():
    """扩展关键词 + 部分相关 → scope_expansion."""
    anchor = _make_anchor(
        goal="refactor authentication module",
        success_criteria=["tests pass", "auth module clean"],
    )
    result = classify_input(
        "also please add password reset feature", goal_anchor=anchor
    )
    assert result.category == "scope_expansion"
    assert result.integration_hint["reason"] == "potential_scope_creep"


@pytest.mark.unit
def test_on_topic_progress_high_overlap():
    """关键词重叠 >= 0.3 → on_topic_progress."""
    anchor = _make_anchor(
        goal="refactor authentication module",
        success_criteria=["tests pass", "tokens validated"],
    )
    result = classify_input(
        "tests pass for the refactor authentication validation", goal_anchor=anchor
    )
    assert result.category == "on_topic_progress"


@pytest.mark.unit
def test_off_topic_noise_low_overlap_default():
    """与 goal 关键词无重叠, 也无任何关键词模式 → off_topic_noise."""
    anchor = _make_anchor(goal="refactor authentication module")
    result = classify_input(
        "the weather is nice today and I like pizza", goal_anchor=anchor
    )
    assert result.category == "off_topic_noise"
    assert result.integration_hint["action"] == "queue_for_post_task"


@pytest.mark.unit
def test_off_topic_noise_matches_out_of_scope_keyword():
    """anchor.out_of_scope 关键词匹配 → off_topic_noise (用户显式声明的范围外)."""
    anchor = _make_anchor(
        goal="refactor module X",
        out_of_scope=["touching unrelated modules", "modify database schema"],
    )
    result = classify_input("touching unrelated modules schema", goal_anchor=anchor)
    assert result.category == "off_topic_noise"
    assert "out_of_scope" in result.matched_signal


@pytest.mark.unit
def test_empty_input_is_noise():
    """空输入 → off_topic_noise."""
    anchor = _make_anchor()
    result = classify_input("", goal_anchor=anchor)
    assert result.category == "off_topic_noise"
    assert result.confidence == 1.0


@pytest.mark.unit
def test_no_anchor_falls_back_to_noise_default():
    """无 goal_anchor 且无强信号 → off_topic_noise (保守)."""
    result = classify_input("hello there random message", goal_anchor=None)
    assert result.category == "off_topic_noise"


@pytest.mark.unit
def test_interrupt_works_without_anchor():
    """interrupt 强信号即使无 anchor 也命中."""
    result = classify_input("cancel this please", goal_anchor=None)
    assert result.category == "interrupt"


@pytest.mark.unit
def test_confidence_in_valid_range():
    """所有分类的 confidence 都在 [0, 1]."""
    anchor = _make_anchor()
    inputs = [
        "stop",
        "instead do X",
        "actually I meant the auth refactor",
        "tests pass for auth refactor",
        "the weather",
        "",
        "also add Y to authentication",
    ]
    for inp in inputs:
        result = classify_input(inp, goal_anchor=anchor)
        assert 0.0 <= result.confidence <= 1.0
