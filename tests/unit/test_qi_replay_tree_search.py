from __future__ import annotations

import pytest
from kun.qi.idle_replay import StrategyCandidate
from kun.qi.replay_tree_search import (
    QiReplayTreeSearchBudget,
    run_qi_replay_tree_search_pool,
)


def _candidate(candidate_id: str = "cand-1") -> StrategyCandidate:
    return StrategyCandidate(
        candidate_id=candidate_id,
        source_signal_id="signal-1",
        task_type="marketing.ad",
        summary="Marketing task could use a tighter sparse memory path",
        proposed_change="Use sparse_credit_guided memory and bounded branching",
        expected_benefit="Higher quality with lower wasted context",
        risk="medium",
        requires_strong_review=False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_replay_tree_search_disabled_is_honest() -> None:
    result = await run_qi_replay_tree_search_pool([_candidate()], enabled=False)

    assert result.enabled is False
    assert result.skipped == 1
    assert result.production_action is False
    assert result.promotion_allowed is False
    assert result.records[0].status == "skipped_disabled"
    assert result.records[0].production_action is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_replay_tree_search_evaluates_with_injected_runner() -> None:
    async def runner(_item: StrategyCandidate, strategy: dict[str, object]) -> tuple[float, float]:
        if strategy.get("risk_gate") == "human_approval":
            return 0.91, 0.002
        if strategy.get("memory_depth") == "deep":
            return 0.82, 0.002
        return 0.55, 0.002

    result = await run_qi_replay_tree_search_pool(
        [_candidate()],
        enabled=True,
        budget=QiReplayTreeSearchBudget(max_items=1, max_cost_usd=0.03, beam_width=2, max_depth=2),
        runner=runner,
    )

    assert result.enabled is True
    assert result.evaluated == 1
    assert result.errors == 0
    assert result.budget_used_usd > 0
    record = result.records[0]
    assert record.status == "evaluated"
    assert record.evaluator_kind == "tree_search"
    assert record.score == record.best_score
    assert record.best_score >= 0.82
    assert record.evaluation_id.startswith("qits_")
    assert record.best_path
    assert record.production_action is False
    assert record.promotion_allowed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_replay_tree_search_respects_item_budget() -> None:
    result = await run_qi_replay_tree_search_pool(
        [_candidate("cand-1"), _candidate("cand-2")],
        enabled=True,
        budget=QiReplayTreeSearchBudget(max_items=1, max_cost_usd=0.01, beam_width=1, max_depth=0),
    )

    assert len(result.records) == 2
    assert result.evaluated == 1
    assert result.skipped == 1
    assert result.records[1].status == "skipped_budget_exhausted"
