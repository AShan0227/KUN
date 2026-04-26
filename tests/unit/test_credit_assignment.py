"""CreditAssignment 单测 (V2.2 §25 / Wire 14)."""

from __future__ import annotations

import pytest
from kun.engineering.credit_assignment import (
    CreditAssignment,
    StepCredit,
    heuristic_reflector,
)

# ---- record_step ----


def test_record_step_creates_initial_credit() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"memory": ["m1"], "model": ["claude"]},
        immediate_reward=0.10,
    )
    assert credit.step_id == 1
    assert credit.immediate_reward == 0.10
    # 平摊: 2 资源 → 各 0.5
    assert abs(credit.credit_share["memory:m1"] - 0.5) < 1e-9
    assert abs(credit.credit_share["model:claude"] - 0.5) < 1e-9


def test_record_step_immediate_reward_floor() -> None:
    """负 reward 被 floor 到 0 (避免负反馈反推)."""
    ca = CreditAssignment(immediate_reward_floor=0.0)
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"model": ["m"]},
        immediate_reward=-0.5,
    )
    assert credit.immediate_reward == 0.0


def test_record_step_empty_resources() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(task_id="tk-1", step_id=1, resources={})
    assert credit.credit_share == {}


# ---- finalize_task + reflector ----


@pytest.mark.asyncio
async def test_finalize_with_heuristic_reflector_marks_critical() -> None:
    """高 reward step 应该被 heuristic reflector 标 critical."""
    ca = CreditAssignment(critical_boost_factor=2.0)
    ca.record_step("tk-1", 1, {"model": ["a"]}, immediate_reward=0.1)
    ca.record_step("tk-1", 2, {"model": ["b"]}, immediate_reward=0.5)  # 高
    ca.record_step("tk-1", 3, {"model": ["c"]}, immediate_reward=0.1)
    report = await ca.finalize_task("tk-1", "pass", reflector=heuristic_reflector)

    assert report.task_outcome == "pass"
    assert 2 in report.critical_path_step_ids
    # 确认 step 2 的 credit 被 boost
    step_2 = next(s for s in report.step_credits if s.step_id == 2)
    assert step_2.is_critical_path is True
    assert step_2.credit_share["model:b"] == 2.0  # 1.0 × 2.0 boost


@pytest.mark.asyncio
async def test_finalize_no_reflector_falls_back_to_equal_share() -> None:
    """没 reflector → critical 列表为空, credit 不 boost."""
    ca = CreditAssignment()
    ca.record_step("tk-2", 1, {"skill": ["s1"]}, immediate_reward=0.1)
    ca.record_step("tk-2", 2, {"skill": ["s2"]}, immediate_reward=0.1)
    report = await ca.finalize_task("tk-2", "pass", reflector=None)
    assert report.critical_path_step_ids == []


@pytest.mark.asyncio
async def test_finalize_reflector_exception_safe() -> None:
    """reflector 抛异常 → log + 退化, 不破坏主流程."""

    async def bad_reflector(task_id, steps, outcome):
        raise RuntimeError("boom")

    ca = CreditAssignment()
    ca.record_step("tk-3", 1, {"model": ["x"]}, immediate_reward=0.2)
    report = await ca.finalize_task("tk-3", "pass", reflector=bad_reflector)
    assert report.critical_path_step_ids == []


@pytest.mark.asyncio
async def test_finalize_no_steps_returns_empty_report() -> None:
    ca = CreditAssignment()
    report = await ca.finalize_task("tk-empty", "fail")
    assert report.step_credits == []
    assert report.total_immediate_reward == 0.0


# ---- aggregate_resource_credits ----


@pytest.mark.asyncio
async def test_aggregate_resource_credits_sums_share_x_reward() -> None:
    ca = CreditAssignment(critical_boost_factor=1.5)
    ca.record_step("tk-4", 1, {"model": ["claude"]}, immediate_reward=0.5)
    ca.record_step("tk-4", 2, {"model": ["claude"]}, immediate_reward=0.3)
    report = await ca.finalize_task("tk-4", "pass", reflector=heuristic_reflector)
    agg = ca.aggregate_resource_credits(report)
    # claude 用了 2 步 → share=1.0 each, reward 0.5/0.3
    # 关键步 reward × boost (1.5 step 1) — heuristic 标 step 1 (高于平均 0.4)
    assert "model:claude" in agg
    assert agg["model:claude"] > 0


@pytest.mark.asyncio
async def test_aggregate_zero_reward_uses_baseline() -> None:
    """immediate_reward=0 时用 baseline 0.5 算 credit (避免全部 0)."""
    ca = CreditAssignment()
    ca.record_step("tk-5", 1, {"skill": ["s1"]}, immediate_reward=0.0)
    report = await ca.finalize_task("tk-5", "pass")
    agg = ca.aggregate_resource_credits(report)
    assert agg["skill:s1"] > 0  # 用 baseline


# ---- reset_task ----


def test_reset_task_clears_state() -> None:
    ca = CreditAssignment()
    ca.record_step("tk-6", 1, {"model": ["x"]})
    assert "tk-6" in ca._step_credits
    ca.reset_task("tk-6")
    assert "tk-6" not in ca._step_credits


# ---- 校验 ----


def test_invalid_critical_boost_raises() -> None:
    with pytest.raises(ValueError):
        CreditAssignment(critical_boost_factor=0.5)


# ---- heuristic_reflector ----


@pytest.mark.asyncio
async def test_heuristic_reflector_picks_above_average() -> None:
    steps = [
        StepCredit(step_id=1, immediate_reward=0.1),
        StepCredit(step_id=2, immediate_reward=0.5),
        StepCredit(step_id=3, immediate_reward=0.2),
    ]
    critical = await heuristic_reflector("tk-x", steps, "pass")
    assert 2 in critical


@pytest.mark.asyncio
async def test_heuristic_reflector_falls_back_to_last_step() -> None:
    """全 reward 0 → 兜底返最后一步."""
    steps = [
        StepCredit(step_id=1, immediate_reward=0.0),
        StepCredit(step_id=5, immediate_reward=0.0),
    ]
    critical = await heuristic_reflector("tk-x", steps, "pass")
    assert critical == [5]
