"""Tests for intent_clarifier (V2.1 §5.1.1 + §5.1.2 / T15 + T34)."""

from __future__ import annotations

from kun.engineering.intent_clarifier import (
    ELLIPSIS_HINTS,
    IntentClarifier,
    IntentSaturation,
    SaturationResult,
)


def test_saturation_full_info() -> None:
    """信息全 → 高分."""
    result = IntentSaturation.evaluate(
        task_meta={
            "intent_one_sentence": "echo hello",
            "complexity_score": 0.1,
            "risk_level": "low",
        },
        user_prompt="给我打印 hello world",
    )
    assert result.saturation_score >= 0.6
    assert result.needs_clarification is False


def test_saturation_missing_intent() -> None:
    """缺 intent_one_sentence → 触发反问."""
    result = IntentSaturation.evaluate(
        task_meta={"complexity_score": 0.5, "risk_level": "low"},
        user_prompt="x",
    )
    assert "intent_one_sentence" in result.missing_fields
    assert result.needs_clarification is True


def test_saturation_critical_no_threshold() -> None:
    """critical 任务无 approval_threshold → 风险信号."""
    result = IntentSaturation.evaluate(
        task_meta={
            "intent_one_sentence": "deploy prod",
            "risk_level": "critical",
            "complexity_score": 0.9,
            # 缺 approval_threshold_money / risk_tolerance
            "success_criteria_short": "部署成功",
            "deliverable": "release notes",
            "deadline_iso_or_none": None,
        },
        user_prompt="部署到生产",
    )
    assert "critical_no_approval_threshold" in result.risk_signals
    assert "critical_no_risk_tolerance" in result.risk_signals
    assert result.needs_clarification is True


def test_saturation_high_complexity_short_prompt() -> None:
    """复杂任务但 prompt 太短 → 风险信号."""
    result = IntentSaturation.evaluate(
        task_meta={
            "intent_one_sentence": "重构",
            "complexity_score": 0.8,
            "risk_level": "low",
        },
        user_prompt="重构",  # 极短
    )
    assert any("high_complexity_short_prompt" in s for s in result.risk_signals)


def test_saturation_ellipsis_detected() -> None:
    """省略词 → 触发反问."""
    result = IntentSaturation.evaluate(
        task_meta={
            "intent_one_sentence": "处理订单",
            "complexity_score": 0.4,
            "risk_level": "low",
        },
        user_prompt="处理订单和发邮件等等",
    )
    assert any(s.startswith("ellipsis:") for s in result.risk_signals)
    assert result.needs_clarification is True


def test_saturation_force_plan_only_when_score_low() -> None:
    """信息严重不足 → 强制 plan-only."""
    result = IntentSaturation.evaluate(
        task_meta={"complexity_score": 0.9, "risk_level": "critical"},
        user_prompt="x",
    )
    # 缺多字段 + critical 多信号 → score < 0.4
    assert result.force_plan_only is True


def test_clarifier_no_request_if_full() -> None:
    """信息全 → 不反问."""
    result = SaturationResult(saturation_score=0.95, needs_clarification=False)
    req = IntentClarifier.build_request(result, {}, "x")
    assert req is None


def test_clarifier_questions_for_missing_fields() -> None:
    """缺字段 → 生成对应问题."""
    result = SaturationResult(
        saturation_score=0.3,
        missing_fields=["intent_one_sentence", "deliverable"],
        needs_clarification=True,
    )
    req = IntentClarifier.build_request(result, {}, "做点事")
    assert req is not None
    assert len(req.questions) == 2
    field_names = [q.field_name for q in req.questions]
    assert "intent_one_sentence" in field_names
    assert "deliverable" in field_names


def test_clarifier_choice_format_for_deliverable() -> None:
    """deliverable 用 choice 格式而非开放问."""
    result = SaturationResult(
        saturation_score=0.5,
        missing_fields=["deliverable"],
        needs_clarification=True,
    )
    req = IntentClarifier.build_request(result, {}, "x")
    assert req is not None
    q = req.questions[0]
    assert q.question_kind == "choice"
    assert "代码文件" in q.choices
    assert q.default_suggestion != ""  # 必有默认猜测


def test_clarifier_critical_threshold_question() -> None:
    """critical 缺 threshold → 选金额."""
    result = SaturationResult(
        saturation_score=0.5,
        risk_signals=["critical_no_approval_threshold"],
        needs_clarification=True,
    )
    req = IntentClarifier.build_request(result, {}, "x")
    assert req is not None
    q = req.questions[0]
    assert q.question_kind == "choice"
    assert "$10" in q.choices


def test_clarifier_ellipsis_expansion_lists_options() -> None:
    """省略词反问列出可能涵盖项 (KUN 主动补齐)."""
    result = SaturationResult(
        saturation_score=0.5,
        risk_signals=["ellipsis:等等"],
        needs_clarification=True,
    )
    req = IntentClarifier.build_request(result, {}, "处理订单, 发邮件, 退款 等等")
    assert req is not None
    q = req.questions[0]
    assert q.question_kind == "choice"
    assert len(q.choices) > 0
    assert "全局视角" in q.rationale


def test_clarifier_summary_includes_count() -> None:
    """summary 告诉用户有几个问题."""
    result = SaturationResult(
        saturation_score=0.3,
        missing_fields=["intent_one_sentence", "deliverable"],
        risk_signals=["critical_no_approval_threshold"],
        needs_clarification=True,
    )
    req = IntentClarifier.build_request(result, {"risk_level": "critical"}, "x")
    assert req is not None
    assert "3" in req.summary


def test_clarifier_default_action_with_force_plan_only() -> None:
    """force_plan_only 时跳过的默认行为不同."""
    result = SaturationResult(
        saturation_score=0.2,
        missing_fields=["intent_one_sentence"],
        needs_clarification=True,
        force_plan_only=True,
    )
    req = IntentClarifier.build_request(result, {}, "x")
    assert req is not None
    assert "plan-only" in req.suggested_default_action.lower()


def test_ellipsis_hints_includes_chinese_and_english() -> None:
    assert "等等" in ELLIPSIS_HINTS
    assert "etc" in ELLIPSIS_HINTS
    assert "..." in ELLIPSIS_HINTS
