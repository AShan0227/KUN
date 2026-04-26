"""DynamicReplanner 单测 (V2.2 §22 / BATCH5 C20 Wire 10)."""

from __future__ import annotations

import pytest
from kun.core.ooda_loop import OODACycle, OODAState
from kun.engineering.dynamic_replan import (
    DynamicReplanner,
    Plan,
)


def _cycle_with(reflections=None, actions=None) -> OODACycle:
    return OODACycle(
        task_ref="tk-1",
        current_state=OODAState.REFLECT,
        reflections=reflections or [],
        actions_taken=actions or [],
    )


# ---- detect_replan_needed ----


@pytest.mark.asyncio
async def test_no_signals_no_replan() -> None:
    r = DynamicReplanner()
    d = await r.detect_replan_needed(_cycle_with())
    assert d.needs_replan is False
    assert d.reason == "no_replan_needed"


@pytest.mark.asyncio
async def test_scope_drift_triggers_replan() -> None:
    r = DynamicReplanner()
    cycle = _cycle_with(reflections=[{"needs_adjust": True, "reason": "scope_drift detected"}])
    d = await r.detect_replan_needed(cycle)
    assert d.needs_replan is True
    assert d.reason == "scope_drift"
    assert d.confidence >= 0.85


@pytest.mark.asyncio
async def test_repeated_failure_same_step_triggers_replan() -> None:
    r = DynamicReplanner(max_step_failure_count=2)
    cycle = _cycle_with(
        actions=[
            {"step_id": 3, "status": "failed"},
            {"step_id": 3, "status": "failed"},
        ]
    )
    d = await r.detect_replan_needed(cycle)
    assert d.needs_replan is True
    assert d.reason == "step_failure_repeated"
    assert d.metadata["failed_step_id"] == 3


@pytest.mark.asyncio
async def test_failure_across_different_steps_no_replan() -> None:
    """失败但不是同一 step → 可能是常规波动, 不 replan."""
    r = DynamicReplanner(max_step_failure_count=2)
    cycle = _cycle_with(
        actions=[
            {"step_id": 1, "status": "failed"},
            {"step_id": 3, "status": "failed"},
        ]
    )
    d = await r.detect_replan_needed(cycle)
    assert d.needs_replan is False


@pytest.mark.asyncio
async def test_outcome_mismatch_above_threshold() -> None:
    r = DynamicReplanner(scope_drift_threshold=0.3)
    cycle = _cycle_with(reflections=[{"needs_adjust": False, "outcome_mismatch_ratio": 0.5}])
    d = await r.detect_replan_needed(cycle)
    assert d.needs_replan is True
    assert d.reason == "outcome_mismatch"


# ---- replan_from_step ----


@pytest.mark.asyncio
async def test_replan_keeps_completed_steps() -> None:
    r = DynamicReplanner()
    original = Plan(
        steps=[
            {"step_id": 1, "description": "step 1"},
            {"step_id": 2, "description": "step 2"},
            {"step_id": 3, "description": "step 3"},
        ]
    )
    new_plan = await r.replan_from_step(
        original,
        current_step_idx=2,
        new_observations=[
            {"intent": "new direction"},
            {"intent": "another step"},
        ],
    )
    # 前 2 step 保留
    assert len(new_plan.steps) == 4
    assert new_plan.steps[0]["description"] == "step 1"
    assert new_plan.steps[1]["description"] == "step 2"
    assert new_plan.steps[2]["description"] == "new direction"
    assert new_plan.steps[3]["description"] == "another step"
    assert new_plan.metadata["replanned_from_step"] == 2
    assert new_plan.metadata["kept_step_count"] == 2


@pytest.mark.asyncio
async def test_replan_invalid_idx_raises() -> None:
    r = DynamicReplanner()
    plan = Plan(steps=[{"step_id": 1}])
    with pytest.raises(ValueError):
        await r.replan_from_step(plan, current_step_idx=-1, new_observations=[])
    with pytest.raises(ValueError):
        await r.replan_from_step(plan, current_step_idx=10, new_observations=[])


# ---- calculate_sunk_cost ----


def test_sunk_cost_full_progress() -> None:
    r = DynamicReplanner()
    plan = Plan(
        steps=[
            {"cost_usd_estimate": 0.05, "duration_sec_estimate": 10},
            {"cost_usd_estimate": 0.10, "duration_sec_estimate": 20},
            {"cost_usd_estimate": 0.05, "duration_sec_estimate": 5},
        ]
    )
    sunk = r.calculate_sunk_cost(plan, current_step_idx=2)
    assert sunk.completed_steps == 2
    assert sunk.total_planned_steps == 3
    assert abs(sunk.completed_cost_usd - 0.15) < 1e-9
    assert abs(sunk.completed_duration_sec - 30) < 1e-9
    assert abs(sunk.progress_ratio - 2 / 3) < 1e-9


def test_sunk_cost_with_reusable_output() -> None:
    r = DynamicReplanner()
    plan = Plan(
        steps=[
            {"cost_usd_estimate": 0.10, "output_ref": "s3://artifact-1"},
        ]
    )
    sunk = r.calculate_sunk_cost(plan, current_step_idx=1)
    assert sunk.can_reuse_outputs is True


# ---- is_replan_worth_it ----


def test_high_confidence_always_worth() -> None:
    r = DynamicReplanner()
    # mock decision
    from kun.engineering.dynamic_replan import ReplanDecision, SunkCostEstimate

    d = ReplanDecision(needs_replan=True, reason="x", confidence=0.9)
    sunk = SunkCostEstimate(
        completed_steps=8,
        total_planned_steps=10,
        completed_cost_usd=1.0,
        completed_duration_sec=100,
        progress_ratio=0.8,
        can_reuse_outputs=False,
    )
    worth, reason = r.is_replan_worth_it(d, sunk)
    assert worth is True
    assert "high_confidence" in reason


def test_low_confidence_not_worth() -> None:
    r = DynamicReplanner()
    from kun.engineering.dynamic_replan import ReplanDecision, SunkCostEstimate

    d = ReplanDecision(needs_replan=True, reason="x", confidence=0.4)
    sunk = SunkCostEstimate(
        completed_steps=1,
        total_planned_steps=10,
        completed_cost_usd=0.05,
        completed_duration_sec=10,
        progress_ratio=0.1,
        can_reuse_outputs=False,
    )
    worth, _ = r.is_replan_worth_it(d, sunk)
    assert worth is False


def test_too_much_progress_not_worth_unless_high_confidence() -> None:
    r = DynamicReplanner()
    from kun.engineering.dynamic_replan import ReplanDecision, SunkCostEstimate

    # 中等 confidence + 进度高 → 不值
    d = ReplanDecision(needs_replan=True, reason="x", confidence=0.7)
    sunk = SunkCostEstimate(
        completed_steps=9,
        total_planned_steps=10,
        completed_cost_usd=2.0,
        completed_duration_sec=200,
        progress_ratio=0.9,
        can_reuse_outputs=False,
    )
    worth, _ = r.is_replan_worth_it(d, sunk)
    assert worth is False
