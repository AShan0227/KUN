"""AI Scientist v2 tree search (Wire 51).

DarwinGodelLoop 现在是线性多轮探索: 一轮接一轮微调 strategy.
这一层补"树搜索": 同一轮可以展开多个候选 strategy, 用 beam search 保留
最有希望的分支, 防止启一直沿着一条不够好的路走下去。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


TreeRunner = Callable[[str, dict[str, Any]], Awaitable[tuple[float, float]]]
CandidateGenerator = Callable[["ScientistTreeNode"], list[dict[str, Any]]]


@dataclass(frozen=True)
class ScientistTreeNode:
    """One explored strategy node."""

    node_id: str
    parent_id: str | None
    depth: int
    strategy: dict[str, Any]
    score: float = 0.0
    cost_usd: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    notes: str = ""


@dataclass
class ScientistTreeSearchResult:
    """Tree-search summary."""

    started_at: datetime
    finished_at: datetime
    nodes: list[ScientistTreeNode] = field(default_factory=list)
    best_node_id: str = ""
    best_strategy: dict[str, Any] = field(default_factory=dict)
    best_score: float = 0.0
    total_cost_usd: float = 0.0
    stopped_reason: str = ""

    def path_to_best(self) -> list[ScientistTreeNode]:
        by_id = {n.node_id: n for n in self.nodes}
        node = by_id.get(self.best_node_id)
        path: list[ScientistTreeNode] = []
        while node is not None:
            path.append(node)
            node = by_id.get(node.parent_id) if node.parent_id else None
        return list(reversed(path))


class AIScientistTreeSearch:
    """Budget-limited beam search over execution strategies."""

    def __init__(
        self,
        runner: TreeRunner,
        *,
        beam_width: int = 3,
        max_depth: int = 3,
        total_budget_usd: float = 2.0,
        total_time_sec: float = 300.0,
        candidate_generator: CandidateGenerator | None = None,
    ) -> None:
        if beam_width < 1:
            raise ValueError("beam_width must be >= 1")
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        self._runner = runner
        self._beam_width = beam_width
        self._max_depth = max_depth
        self._total_budget_usd = total_budget_usd
        self._total_time_sec = total_time_sec
        self._candidate_generator = candidate_generator or _default_candidate_generator

    async def search(
        self,
        prompt: str,
        *,
        root_strategy: dict[str, Any] | None = None,
    ) -> ScientistTreeSearchResult:
        """Run beam search and return the best strategy found."""

        started_at = datetime.now(UTC)
        result = ScientistTreeSearchResult(started_at=started_at, finished_at=started_at)
        t_start = time.perf_counter()
        root = ScientistTreeNode(
            node_id="root",
            parent_id=None,
            depth=0,
            strategy=dict(root_strategy or {"strategy": "baseline", "temperature": 0.3}),
        )
        frontier = [root]

        for depth in range(self._max_depth + 1):
            if time.perf_counter() - t_start > self._total_time_sec:
                result.stopped_reason = "time_exhausted"
                break
            if result.total_cost_usd >= self._total_budget_usd:
                result.stopped_reason = "budget_exhausted"
                break

            evaluated: list[ScientistTreeNode] = []
            for node in frontier:
                if result.total_cost_usd >= self._total_budget_usd:
                    result.stopped_reason = "budget_exhausted"
                    break
                try:
                    score, cost = await self._runner(prompt, node.strategy)
                except Exception as exc:
                    logger.exception("ai_scientist.node_failed node_id=%s", node.node_id)
                    score, cost = 0.0, 0.0
                    node = ScientistTreeNode(
                        node_id=node.node_id,
                        parent_id=node.parent_id,
                        depth=node.depth,
                        strategy=node.strategy,
                        score=score,
                        cost_usd=cost,
                        notes=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    node = ScientistTreeNode(
                        node_id=node.node_id,
                        parent_id=node.parent_id,
                        depth=node.depth,
                        strategy=node.strategy,
                        score=float(score),
                        cost_usd=float(cost),
                    )
                result.nodes.append(node)
                evaluated.append(node)
                result.total_cost_usd += node.cost_usd
                if node.score > result.best_score or not result.best_node_id:
                    result.best_score = node.score
                    result.best_node_id = node.node_id
                    result.best_strategy = node.strategy

            if depth >= self._max_depth:
                result.stopped_reason = result.stopped_reason or "depth_max"
                break

            beam = sorted(evaluated, key=lambda n: n.score, reverse=True)[: self._beam_width]
            next_frontier: list[ScientistTreeNode] = []
            for parent in beam:
                for idx, strategy in enumerate(self._candidate_generator(parent)):
                    next_frontier.append(
                        ScientistTreeNode(
                            node_id=f"{parent.node_id}.{idx}",
                            parent_id=parent.node_id,
                            depth=parent.depth + 1,
                            strategy=strategy,
                        )
                    )
            frontier = next_frontier[: self._beam_width]
            if not frontier:
                result.stopped_reason = result.stopped_reason or "no_candidates"
                break
        else:
            result.stopped_reason = result.stopped_reason or "depth_max"

        result.finished_at = datetime.now(UTC)
        return result


def _default_candidate_generator(parent: ScientistTreeNode) -> list[dict[str, Any]]:
    """Small deterministic strategy mutations."""

    strategy = dict(parent.strategy)
    base_temp = float(strategy.get("temperature", 0.3))
    variants: list[dict[str, Any]] = []
    for delta, label in [(-0.1, "conservative"), (0.1, "exploratory"), (0.0, "wider")]:
        next_strategy = dict(strategy)
        next_strategy["temperature"] = max(0.0, min(1.0, base_temp + delta))
        next_strategy["mutation"] = label
        if label == "wider":
            next_strategy["n_paths"] = int(next_strategy.get("n_paths", 3)) + 1
        next_strategy["parent_score"] = parent.score
        variants.append(next_strategy)
    return variants


__all__ = [
    "AIScientistTreeSearch",
    "CandidateGenerator",
    "ScientistTreeNode",
    "ScientistTreeSearchResult",
    "TreeRunner",
]
