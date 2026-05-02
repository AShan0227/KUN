"""AttentionBudgetGuard tests."""

from __future__ import annotations

import pytest
from kun.engineering.attention_budget import AgentSnapshot, AttentionBudgetGuard


def test_can_start_session_when_under_default_limit() -> None:
    guard = AttentionBudgetGuard(max_active_sessions_default=2)

    assert guard.register_session("u-1", "s-1") is True
    assert guard.can_start_session("u-1") is True


def test_cannot_start_session_when_limit_reached() -> None:
    guard = AttentionBudgetGuard(max_active_sessions_default=1)

    assert guard.register_session("u-1", "s-1") is True
    assert guard.can_start_session("u-1") is False
    assert guard.register_session("u-1", "s-2") is False


def test_end_session_releases_capacity() -> None:
    guard = AttentionBudgetGuard(max_active_sessions_default=1)
    guard.register_session("u-1", "s-1")

    guard.end_session("u-1", "s-1")

    assert guard.can_start_session("u-1") is True


def test_queue_excess_records_task_meta_fifo() -> None:
    guard = AttentionBudgetGuard()

    q1 = guard.queue_excess("u-1", {"task_id": "t-1"})
    q2 = guard.queue_excess("u-1", {"task_id": "t-2"})

    assert q1 != q2
    assert guard.queue_depth("u-1") == 2
    first = guard.pop_next_queued("u-1")
    second = guard.pop_next_queued("u-1")
    assert first is not None
    assert second is not None
    assert first.task_meta["task_id"] == "t-1"
    assert second.task_meta["task_id"] == "t-2"
    assert guard.pop_next_queued("u-1") is None


def test_user_specific_limit_overrides_default() -> None:
    guard = AttentionBudgetGuard(max_active_sessions_default=1)
    guard.set_user_limit("u-1", 2)

    assert guard.register_session("u-1", "s-1") is True
    assert guard.register_session("u-1", "s-2") is True
    assert guard.can_start_session("u-1") is False


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        AttentionBudgetGuard(max_active_sessions_default=0)

    guard = AttentionBudgetGuard()
    with pytest.raises(ValueError):
        guard.set_user_limit("u-1", 0)


def test_summarize_agent_status_limits_each_agent_to_five_lines() -> None:
    guard = AttentionBudgetGuard()
    summary = guard.summarize_agent_status(
        [
            AgentSnapshot(
                agent_id="agent-1",
                task_id="task-1",
                status="running",
                goal="整理报价",
                current_step="查合同",
                progress_pct=0.5,
                cost_usd=0.1234,
                risk_level="medium",
            ),
            AgentSnapshot(
                agent_id="agent-2",
                task_id="task-2",
                status="blocked",
                goal="发邮件",
                current_step="等审批",
                progress_pct=0.2,
                cost_usd=0.05,
                risk_level="high",
            ),
        ],
    )

    blocks = summary.split("\n\n")
    assert len(blocks) == 2
    assert all(len(block.splitlines()) <= 5 for block in blocks)
    assert "agent-1 · running" in summary
    assert "成本: $0.1234" in summary


def test_summarize_empty_agents() -> None:
    guard = AttentionBudgetGuard()

    assert guard.summarize_agent_status([]) == "当前没有活跃 agent。"
