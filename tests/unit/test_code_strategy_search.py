from __future__ import annotations

import pytest
from kun.skills.code_capability.strategy_search import (
    CodeStrategySearchBudget,
    CodeStrategySearchInput,
    run_code_change_strategy_tree_search,
)


def _input() -> CodeStrategySearchInput:
    return CodeStrategySearchInput(
        task_id="task-code-1",
        path="kun/foo.py",
        mode="dry_run",
        phase="done",
        checks_passed=True,
        review_ok=True,
        bytes_changed=128,
        diff_sha256="abc123",
        reason="small typed refactor",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_strategy_search_disabled_is_review_only_skip() -> None:
    record = await run_code_change_strategy_tree_search(_input(), enabled=False)

    assert record.status == "skipped_disabled"
    assert record.production_action is False
    assert record.promotion_allowed is False
    assert record.evidence["review_only"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_strategy_search_evaluates_with_budgeted_tree() -> None:
    async def runner(
        _data: CodeStrategySearchInput,
        strategy: dict[str, object],
    ) -> tuple[float, float]:
        if strategy.get("learning_output") == "draft_skill_review":
            return 0.91, 0.002
        if strategy.get("sandbox") == "strict":
            return 0.84, 0.002
        return 0.6, 0.002

    record = await run_code_change_strategy_tree_search(
        _input(),
        enabled=True,
        budget=CodeStrategySearchBudget(max_cost_usd=0.03, beam_width=2, max_depth=2),
        runner=runner,
    )

    assert record.status == "evaluated"
    assert record.evaluator_kind == "code_tree_search"
    assert record.score == record.best_score
    assert record.best_score >= 0.84
    assert record.best_path
    assert record.production_action is False
    assert record.promotion_allowed is False
