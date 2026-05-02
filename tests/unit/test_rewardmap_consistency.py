"""V2.2 Wire 16+17 — RewardMap stage_rewards + ThoughtActionConsistency 测试.

§25.4a: StepCredit 加 stage_rewards (RewardMap 启发, ICLR 2026)
§27: ThoughtActionConsistency (FaithCoT 启发)
"""

from __future__ import annotations

import pytest
from kun.engineering.credit_assignment import (
    CreditAssignment,
    StageReward,
    StepCredit,
)
from kun.engineering.execution_protocol import (
    ExecutionStep,
    ThoughtActionConsistency,
)

# ---- StageReward + StepCredit.stage_rewards ----


def test_stage_reward_clamped_in_unit_interval() -> None:
    sr = StageReward(stage="perceive", reward=0.5)
    assert sr.reward == 0.5
    with pytest.raises(Exception):
        StageReward(stage="perceive", reward=1.5)


def test_step_credit_default_stage_rewards_empty() -> None:
    sc = StepCredit(step_id=1)
    assert sc.stage_rewards == []
    assert sc.compute_stage_aggregated_reward() == 0.0


def test_step_credit_stage_rewards_aggregate() -> None:
    sc = StepCredit(
        step_id=1,
        stage_rewards=[
            StageReward(stage="perceive", reward=0.8),
            StageReward(stage="understand", reward=0.6),
            StageReward(stage="reason", reward=0.4),
            StageReward(stage="decide", reward=0.2),
        ],
    )
    # 平均 = 0.5
    assert abs(sc.compute_stage_aggregated_reward() - 0.5) < 1e-9


def test_credit_assignment_record_step_with_stage_rewards() -> None:
    """传 stage_rewards → immediate_reward 自动算 (覆盖手动 reward)."""
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"model": ["claude"]},
        immediate_reward=0.0,  # 不该用这个
        stage_rewards=[
            StageReward(stage="perceive", reward=0.7),
            StageReward(stage="understand", reward=0.7),
            StageReward(stage="reason", reward=0.7),
            StageReward(stage="decide", reward=0.7),
        ],
    )
    # 平均 = 0.7
    assert abs(credit.immediate_reward - 0.7) < 1e-9
    assert len(credit.stage_rewards) == 4


def test_credit_assignment_without_stage_rewards_uses_manual() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-2",
        step_id=1,
        resources={"model": ["x"]},
        immediate_reward=0.42,
    )
    assert credit.immediate_reward == 0.42
    assert credit.stage_rewards == []


# ---- ThoughtActionConsistency ----


@pytest.mark.asyncio
async def test_consistency_high_when_thought_matches_action() -> None:
    checker = ThoughtActionConsistency()
    step = ExecutionStep(
        step_id=1,
        thought="我需要回顾历史记忆找到相关信息",
        action_type="use_memory",
        action_payload={},
        expected_outcome="拿到记忆",
    )
    score, reason = await checker.check(step)
    # 含 "记忆" + "历史" 2 个 keyword → 0.9
    assert score >= 0.7
    assert "heuristic_high" in reason


@pytest.mark.asyncio
async def test_consistency_low_when_thought_unrelated() -> None:
    checker = ThoughtActionConsistency()
    step = ExecutionStep(
        step_id=1,
        thought="今天天气真好",  # 跟 action 完全无关
        action_type="web_search",
        action_payload={},
        expected_outcome="找信息",
    )
    score, _ = await checker.check(step)
    assert score < 0.5  # 没 keyword 命中
    assert checker.needs_rethink(score) is True


@pytest.mark.asyncio
async def test_consistency_with_llm_judge_fallback() -> None:
    """启发式低 + LLM judge → 用 LLM 兜底."""

    async def fake_judge(thought, action_type):
        return 0.85

    checker = ThoughtActionConsistency(llm_judge=fake_judge)
    step = ExecutionStep(
        step_id=1,
        thought="天气很好",
        action_type="web_search",
        action_payload={},
        expected_outcome="x",
    )
    score, reason = await checker.check(step)
    assert score >= 0.85  # LLM 兜底高
    assert "llm_judge" in reason


@pytest.mark.asyncio
async def test_consistency_llm_judge_exception_falls_back() -> None:
    async def bad_judge(thought, action_type):
        raise RuntimeError("llm down")

    checker = ThoughtActionConsistency(llm_judge=bad_judge)
    step = ExecutionStep(
        step_id=1,
        thought="天气很好",
        action_type="web_search",
        action_payload={},
        expected_outcome="x",
    )
    _, reason = await checker.check(step)
    # llm 失败 → 退化启发式
    assert "heuristic_only" in reason


@pytest.mark.asyncio
async def test_consistency_direct_llm_default_neutral() -> None:
    """direct_llm 没强 keyword → 默认 0.7 (中性偏高)."""
    checker = ThoughtActionConsistency()
    step = ExecutionStep(
        step_id=1,
        thought="anything",
        action_type="direct_llm",
        action_payload={},
        expected_outcome="x",
    )
    score, _ = await checker.check(step)
    assert score == 0.7


def test_needs_rethink_threshold() -> None:
    checker = ThoughtActionConsistency(consistency_threshold=0.5)
    assert checker.needs_rethink(0.4) is True
    assert checker.needs_rethink(0.6) is False
    assert checker.needs_rethink(0.5) is False  # 不严格小于


# ---- ExecutionStep 加新字段 ----


def test_execution_step_default_consistency_one() -> None:
    step = ExecutionStep(
        step_id=1,
        thought="x",
        action_type="direct_llm",
        action_payload={},
        expected_outcome="y",
    )
    assert step.thought_action_consistency == 1.0
    assert step.rethink_count == 0


def test_execution_step_consistency_clamped() -> None:
    step = ExecutionStep(
        step_id=1,
        thought="x",
        action_type="direct_llm",
        action_payload={},
        expected_outcome="y",
        thought_action_consistency=1.5,  # 超 1
    )
    assert step.thought_action_consistency == 1.0
    step2 = ExecutionStep(
        step_id=1,
        thought="x",
        action_type="direct_llm",
        action_payload={},
        expected_outcome="y",
        thought_action_consistency=-0.5,  # 负
    )
    assert step2.thought_action_consistency == 0.0
