"""ValueGate 单测 (V2.2 §19.4 — 守望主决策 gate)."""

from __future__ import annotations

import pytest
from kun.engineering.marginal_roi import MarginalROIStopCriterion, ModulePresets
from kun.watchtower.value_gate import ValueGate, ValueGateDecision


def _fake_task_ref(task_id: str = "tk-1"):
    class M:
        pass

    m = M()
    m.task_id = task_id

    class T:
        pass

    t = T()
    t.meta = m
    return t


def _fake_step(step_id: int = 1):
    class S:
        pass

    s = S()
    s.step_id = step_id
    return s


# ---- 基础行为 ----


@pytest.mark.asyncio
async def test_continue_decision_when_value_acceptable() -> None:
    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(min_steps=10),  # 不会触发
        min_value_threshold=0.20,
    )
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(1),
        prior_value_history=[0.5, 0.7],
    )
    assert d.decision == "continue"
    assert d.reason == "value_acceptable"


@pytest.mark.asyncio
async def test_escalate_when_value_below_threshold() -> None:
    """custom estimator 返超低 value → escalate."""

    async def low_value_estimator(ctx):
        return 0.05  # 低于 0.20

    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(),
        min_value_threshold=0.20,
        value_estimator=low_value_estimator,
    )
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.5],
    )
    assert d.decision == "escalate"
    assert d.reason == "value_below_threshold"
    assert d.expected_value == 0.05


@pytest.mark.asyncio
async def test_stop_when_marginal_roi_triggers() -> None:
    """marginal stop → stop decision."""
    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(delta_threshold=0.10, window_k=2, min_steps=2),
        min_value_threshold=0.10,
    )
    # value_history 增量小, 触发 marginal_stop
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.5, 0.51, 0.52],
    )
    assert d.decision == "stop"
    assert "marginal" in d.reason


# ---- 默认 estimator ----


@pytest.mark.asyncio
async def test_default_estimator_no_history() -> None:
    g = ValueGate(marginal_criterion=MarginalROIStopCriterion())
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[],
    )
    # default estimator: 没历史 → 0.5
    assert d.expected_value == 0.5


@pytest.mark.asyncio
async def test_default_estimator_uses_last_plus_epsilon() -> None:
    g = ValueGate(marginal_criterion=MarginalROIStopCriterion())
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.4, 0.6],
    )
    # default: last (0.6) + 0.05 = 0.65
    assert abs(d.expected_value - 0.65) < 1e-9


# ---- escalate handler ----


@pytest.mark.asyncio
async def test_escalate_handler_called_on_low_value() -> None:
    captured: list[ValueGateDecision] = []

    async def handler(d: ValueGateDecision) -> None:
        captured.append(d)

    async def low_estimator(ctx):
        return 0.01

    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(),
        min_value_threshold=0.20,
        value_estimator=low_estimator,
        escalate_handler=handler,
    )
    await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[],
    )
    assert len(captured) == 1
    assert captured[0].decision == "escalate"


@pytest.mark.asyncio
async def test_escalate_handler_exception_not_propagated() -> None:
    async def bad_handler(d):
        raise ValueError("boom")

    async def low_estimator(ctx):
        return 0.01

    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(),
        min_value_threshold=0.20,
        value_estimator=low_estimator,
        escalate_handler=bad_handler,
    )
    # 不应该抛 (handler 异常被吞)
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[],
    )
    assert d.decision == "escalate"


# ---- estimator 异常 ----


@pytest.mark.asyncio
async def test_estimator_exception_uses_default() -> None:
    async def bad_estimator(ctx):
        raise RuntimeError("boom")

    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(),
        value_estimator=bad_estimator,
    )
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.7],
    )
    # estimator 失败 → 默认 0.5
    assert d.expected_value == 0.5


# ---- stats ----


@pytest.mark.asyncio
async def test_stats_track_decisions() -> None:
    g = ValueGate(
        marginal_criterion=MarginalROIStopCriterion(min_steps=10),
        min_value_threshold=0.20,
    )

    # 1 continue
    await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.6],
    )

    stats = g.get_stats()
    assert stats["checks_total"] == 1
    assert stats["decisions_continue"] == 1
    assert stats["decisions_escalate"] == 0


async def _ret(v: float) -> float:
    return v


@pytest.mark.asyncio
async def test_record_step_outcome_doesnt_throw() -> None:
    g = ValueGate(marginal_criterion=MarginalROIStopCriterion())
    # 应该顺利 (它只 log)
    await g.record_step_outcome(
        task_id="tk-1",
        step_id=1,
        outcome_value=0.7,
        cost_usd=0.05,
        success=True,
    )


# ---- input 校验 ----


def test_invalid_min_value_threshold_raises() -> None:
    with pytest.raises(ValueError):
        ValueGate(
            marginal_criterion=MarginalROIStopCriterion(),
            min_value_threshold=1.5,
        )


# ---- preset integration ----


@pytest.mark.asyncio
async def test_using_module_preset() -> None:
    """ValueGate 跟 ModulePresets 配合."""
    g = ValueGate(
        marginal_criterion=ModulePresets.for_idle_batch_step(),
        min_value_threshold=0.10,
    )
    d = await g.check_step(
        task_ref=_fake_task_ref(),
        step_plan=_fake_step(),
        prior_value_history=[0.5, 0.55, 0.55, 0.55],  # min_steps=3 met, marginal 全 < 0.05
    )
    assert d.decision == "stop"
