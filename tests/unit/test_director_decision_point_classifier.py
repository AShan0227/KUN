"""Decision-Point Classifier 测试 — Executor 主动停下问用户的硬规则判定.

覆盖 6 类决策点 + 优先级 + 中英文关键词 + anchor None / out_of_scope 两情况.
"""

from __future__ import annotations

import pytest
from kun.agents.director.decision_point_classifier import (
    DecisionPointResult,
    classify_decision_point,
)


@pytest.mark.unit
def test_destructive_drop_schema_medium_risk_hits_destructive():
    """destructive 关键词 + risk=medium → destructive_action."""
    result = classify_decision_point(
        action_intent="执行 drop schema public 清理旧表",
        task_meta_risk="medium",
    )
    assert result.category == "destructive_action"
    assert result.confidence >= 0.7
    assert "drop schema" in result.matched_signal


@pytest.mark.unit
def test_destructive_rm_rf_high_risk():
    """destructive 关键词 (英文) + risk=high → destructive_action."""
    result = classify_decision_point(
        action_intent="rm -rf / to free up disk",
        task_meta_risk="high",
    )
    assert result.category == "destructive_action"
    assert "rm -rf" in result.matched_signal


@pytest.mark.unit
def test_destructive_keyword_but_low_risk_falls_through():
    """destructive 关键词 + risk=low → 不算 destructive (走兜底)."""
    result = classify_decision_point(
        action_intent="跑 unit test 里的 drop table mock",
        task_meta_risk="low",
    )
    assert result.category == "no_decision"


@pytest.mark.unit
def test_product_direction_chinese_keyword():
    """中文 product 关键词 → product_direction."""
    result = classify_decision_point(
        action_intent="选哪个垂直做下一个 MVP",
        task_meta_risk="low",
    )
    assert result.category == "product_direction"
    assert result.confidence >= 0.7


@pytest.mark.unit
def test_auth_posture_change_priority_over_product():
    """'Auth 升级' 同时命中 auth + product, 应该优先 auth_posture_change."""
    result = classify_decision_point(
        action_intent="Auth 升级到中期 posture",
        task_meta_risk="medium",
    )
    assert result.category == "auth_posture_change"
    assert "auth" in result.matched_signal.lower()


@pytest.mark.unit
def test_external_api_unlock_chinese_medium_risk():
    """'调真 LLM' + risk=medium → external_api_unlock."""
    result = classify_decision_point(
        action_intent="调真 LLM 跑这个 benchmark",
        task_meta_risk="medium",
    )
    assert result.category == "external_api_unlock"
    assert result.confidence >= 0.7


@pytest.mark.unit
def test_external_api_low_risk_not_decision():
    """'调真 LLM' + risk=low → no_decision (low risk 不算财务决策)."""
    result = classify_decision_point(
        action_intent="调真 LLM 跑一个 smoke test",
        task_meta_risk="low",
    )
    assert result.category == "no_decision"


@pytest.mark.unit
def test_auth_kun_auth_enabled_flag():
    """'KUN_AUTH_ENABLED=true' 切真生产 → auth_posture_change."""
    result = classify_decision_point(
        action_intent="把 KUN_AUTH_ENABLED=true 切真生产",
        task_meta_risk="high",
    )
    assert result.category == "auth_posture_change"


@pytest.mark.unit
def test_out_of_anchor_with_anchor_dict():
    """anchor.out_of_scope=['重写 Director'], action='重写 Director.intent' → out_of_anchor."""
    anchor = {
        "out_of_scope": ["重写 Director"],
        "goal_statement": "fix bug in executor",
    }
    result = classify_decision_point(
        action_intent="重写 Director.intent 模块",
        task_meta_risk="low",
        task_anchor=anchor,
    )
    assert result.category == "out_of_anchor"
    assert "Director" in result.matched_signal or "重写" in result.matched_signal


@pytest.mark.unit
def test_no_decision_normal_action_with_no_anchor():
    """普通 action + anchor=None → no_decision."""
    result = classify_decision_point(
        action_intent="改 1 行代码 + 跑测试",
        task_meta_risk="low",
        task_anchor=None,
    )
    assert result.category == "no_decision"


@pytest.mark.unit
def test_no_decision_empty_input():
    """空字符串 → no_decision."""
    result = classify_decision_point(
        action_intent="",
        task_meta_risk="medium",
    )
    assert result.category == "no_decision"


@pytest.mark.unit
def test_suggested_question_and_options_set_for_decision_categories():
    """category != no_decision 时 suggested_question / suggested_options 都不为空."""
    samples: list[DecisionPointResult] = [
        classify_decision_point(
            action_intent="drop schema kun",
            task_meta_risk="critical",
        ),
        classify_decision_point(
            action_intent="切真 JWT 上线",
            task_meta_risk="medium",
        ),
        classify_decision_point(
            action_intent="启用 anthropic api key 调真 LLM",
            task_meta_risk="medium",
        ),
        classify_decision_point(
            action_intent="选哪个垂直进入下一阶段",
        ),
    ]
    for r in samples:
        assert r.category != "no_decision", f"unexpected no_decision for {r}"
        assert r.suggested_question, f"missing question for {r}"
        assert r.suggested_options, f"missing options for {r}"
        assert 2 <= len(r.suggested_options) <= 4


@pytest.mark.unit
def test_confidence_high_when_classified_as_decision():
    """非 no_decision 时 confidence >= 0.7."""
    classified = [
        classify_decision_point(
            action_intent="git push --force origin main",
            task_meta_risk="high",
        ),
        classify_decision_point(
            action_intent="新行业落地 — 选哪个做先发",
        ),
        classify_decision_point(
            action_intent="KUN_AUTH_ENABLED=true",
            task_meta_risk="medium",
        ),
        classify_decision_point(
            action_intent="启用 openai api key 调真 LLM",
            task_meta_risk="high",
        ),
    ]
    for r in classified:
        assert r.category != "no_decision"
        assert r.confidence >= 0.7, f"low confidence: {r}"


@pytest.mark.unit
def test_no_decision_returns_no_suggested_template():
    """no_decision 时 suggested_question / options 都是 None."""
    result = classify_decision_point(action_intent="跑个 pytest")
    assert result.category == "no_decision"
    assert result.suggested_question is None
    assert result.suggested_options is None


@pytest.mark.unit
def test_anchor_none_does_not_break_classification():
    """anchor=None 时其他规则仍正常工作 (覆盖 anchor=None 分支)."""
    result = classify_decision_point(
        action_intent="drop table users",
        task_meta_risk="high",
        task_anchor=None,
    )
    assert result.category == "destructive_action"


@pytest.mark.unit
def test_anchor_with_empty_out_of_scope():
    """anchor 提供但 out_of_scope 空列表 → 不会误判 out_of_anchor."""
    anchor = {"out_of_scope": [], "goal_statement": "anything"}
    result = classify_decision_point(
        action_intent="普通改个 typo",
        task_meta_risk="low",
        task_anchor=anchor,
    )
    assert result.category == "no_decision"


@pytest.mark.unit
def test_result_is_frozen_dataclass():
    """DecisionPointResult 是 frozen — 不可改变 (ADR-024 contract)."""
    result = classify_decision_point(action_intent="hi")
    with pytest.raises((AttributeError, Exception)):
        result.category = "destructive_action"  # type: ignore[misc]
