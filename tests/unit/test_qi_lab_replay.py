from __future__ import annotations

import pytest
from kun.qi.idle_replay import IdleReplayGenerator, TaskHistorySummary
from kun.qi.lab_replay import (
    QiLabReplayBudget,
    QiLabReplayRecord,
    run_qi_lab_replay_pool,
)


def _draft(task_type: str = "marketing.ad"):
    history = TaskHistorySummary(
        history_id="task-1",
        task_type=task_type,
        summary="Historical task used a reusable strategy",
        outcome="completed",
    )
    return IdleReplayGenerator().generate_from_history(history).to_strategy_pack_draft()


async def _fake_runner(draft, history, budget):
    _ = budget
    return QiLabReplayRecord(
        draft_id=draft.draft_id,
        history_id=history.history_id,
        task_type=history.task_type,
        status="evaluated",
        score=0.7,
        cost_usd=0.04,
        experiment_id="exp-fake",
        replay_winning_strategy="tier_strong_mid_temp",
        notes=["fake_lab_replay"],
    )


@pytest.mark.asyncio
async def test_qi_lab_replay_disabled_is_honest() -> None:
    draft = _draft()

    result = await run_qi_lab_replay_pool(
        [draft],
        [TaskHistorySummary(history_id="task-1", task_type="marketing.ad", summary="done")],
        enabled=False,
    )

    assert result.enabled is False
    assert result.evaluated == 0
    assert result.skipped == 1
    assert result.records[0].status == "skipped_disabled"
    assert result.production_action is False


@pytest.mark.asyncio
async def test_qi_lab_replay_runs_matching_history_with_injected_runner() -> None:
    draft = _draft()
    history = TaskHistorySummary(
        history_id="task-1",
        task_type="marketing.ad",
        summary="Historical ad task",
    )

    result = await run_qi_lab_replay_pool(
        [draft],
        [history],
        enabled=True,
        budget=QiLabReplayBudget(max_items=1, max_cost_usd=1.0, paths=2),
        runner=_fake_runner,
    )

    assert result.enabled is True
    assert result.evaluated == 1
    assert result.budget_used_usd == 0.04
    record = result.records[0]
    assert record.status == "evaluated"
    assert record.draft_id == draft.draft_id
    assert record.history_id == "task-1"
    assert record.promotion_allowed is False


@pytest.mark.asyncio
async def test_qi_lab_replay_skips_when_no_task_type_match() -> None:
    draft = _draft("coding.review")
    history = TaskHistorySummary(
        history_id="task-2",
        task_type="marketing.ad",
        summary="Different task family",
    )

    result = await run_qi_lab_replay_pool(
        [draft],
        [history],
        enabled=True,
        budget=QiLabReplayBudget(max_items=1, max_cost_usd=1.0, paths=2),
        runner=_fake_runner,
    )

    assert result.evaluated == 0
    assert result.skipped == 1
    assert result.records[0].status == "skipped_no_match"
