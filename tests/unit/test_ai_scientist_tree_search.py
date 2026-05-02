from __future__ import annotations

import pytest
from kun.qi import AIScientistTreeSearch, ScientistTreeNode


@pytest.mark.asyncio
async def test_ai_scientist_tree_search_tracks_best_path() -> None:
    async def runner(_prompt: str, strategy: dict) -> tuple[float, float]:
        temp = float(strategy.get("temperature", 0.0))
        score = 1.0 - abs(temp - 0.4)
        return score, 0.01

    search = AIScientistTreeSearch(runner, beam_width=2, max_depth=2, total_budget_usd=10.0)
    result = await search.search("find strategy", root_strategy={"temperature": 0.3})

    assert result.nodes
    assert result.best_score >= 0.9
    assert result.best_strategy
    assert result.path_to_best()[0].node_id == "root"


@pytest.mark.asyncio
async def test_ai_scientist_tree_search_budget_stops() -> None:
    async def runner(_prompt: str, _strategy: dict) -> tuple[float, float]:
        return 0.5, 2.0

    search = AIScientistTreeSearch(runner, beam_width=2, max_depth=4, total_budget_usd=1.0)
    result = await search.search("expensive")

    assert result.stopped_reason == "budget_exhausted"
    assert result.total_cost_usd >= 2.0


def test_ai_scientist_tree_node_path_empty_before_best() -> None:
    from kun.qi.ai_scientist import ScientistTreeSearchResult

    result = ScientistTreeSearchResult(
        started_at=ScientistTreeNode("root", None, 0, {}).created_at,
        finished_at=ScientistTreeNode("root", None, 0, {}).created_at,
    )
    assert result.path_to_best() == []
