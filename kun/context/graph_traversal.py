"""GraphTraversal — knowledge graph 邻接查询 (V2.2 §20 mempalace 真闭环, Wire 30).

V2.2 §20 已经做完一半: alembic 0012 + EntityRelationshipRow ORM +
RelationshipMineStep 自动挖关系. 但 ImportanceScorer.score_anchor_then_expand
还是按 score 降序流式 yield, 没真"沿 path 走". Wire 30 补上 graph traversal:
让 anchor 之后的 expand 优先走 entity_relationships 邻接 (mempalace 精髓).

跟 §20.4 三件套联动:
    用户问 "怎么写登录接口"
        → ImportanceScorer.score_anchor_then_expand 第 1 轮 anchor=auth_service.py
        → GraphTraversal.neighbors(auth_service.py, depends_on)
            → jwt_utils.py
        → expand_fn 优先返 jwt_utils.py (而不是 score 第 2 高的 unrelated 资产)
        → marginal_roi 评估 ΔV → 继续/停

设计:
    - 不强制依赖图: 没 relationships 数据 → fallback 现有 score 降序
    - hops=1 默认 (邻居), hops=2 走两跳 (邻居的邻居, 去重)
    - relation_types 过滤 (e.g. 只走 depends_on / similar_to, 不走 contradicts)
    - 邻接按 confidence × distance_decay 排序 (近 + 高置信先)
    - 跨 tenant: 严格按 current_tenant() 隔离, 跟其他表一致
    - best-effort: DB 异常 → 返空 list, 调用方 fallback (不破 importance scorer)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# 默认放行的 relation 类型 (跟 mempalace 路径直觉一致, 排除 contradicts 防误导)
DEFAULT_TRAVERSAL_TYPES: tuple[str, ...] = (
    "depends_on",
    "mentions",
    "verifies",
    "similar_to",
    "co_occurs",
    "produced_by",
)


@dataclass(frozen=True)
class NeighborEntity:
    """一条邻接 entity (沿 relation 走过来的)."""

    entity_kind: str
    entity_id: str
    relation_type: str
    confidence: float
    hops: int  # 1 = 直接邻居, 2 = 邻居的邻居
    via_path: tuple[tuple[str, str], ...]  # (kind, id) 路径序列, 含 source
    pheromone_strength: float | None = None

    @property
    def score(self) -> float:
        """(confidence × pheromone boost) × distance_decay."""
        from kun.qi.pheromone import neighbor_pheromone_score

        decay = 1.0 / (1.0 + 0.5 * (self.hops - 1))
        if self.pheromone_strength is None:
            return self.confidence * decay
        return neighbor_pheromone_score(self.confidence, self.pheromone_strength) * decay


class GraphTraversal:
    """从 entity_relationships 表查邻接 — V2.2 §20 mempalace 路径走查.

    用法:
        traversal = GraphTraversal()
        neighbors = await traversal.neighbors(
            kind="capability_card", entity_id="cc-123", hops=1,
        )
        # neighbors[0] 是最相关的邻居 (confidence × decay 排第一)

    Args:
        session_factory: 可选注入. 默认走 kun.core.db.session_scope.
                         测试可注入 in-memory fake.
        relation_types: 默认放行的 relation 类型 (排除 contradicts).
    """

    def __init__(
        self,
        session_factory: Any = None,
        *,
        relation_types: Iterable[str] = DEFAULT_TRAVERSAL_TYPES,
    ) -> None:
        self._session_factory = session_factory
        self._allowed_types = tuple(relation_types)

    async def neighbors(
        self,
        kind: str,
        entity_id: str,
        *,
        hops: int = 1,
        relation_types: Iterable[str] | None = None,
        limit_per_hop: int = 20,
    ) -> list[NeighborEntity]:
        """BFS 邻接查询, 返按 score 降序的 NeighborEntity list.

        hops:
            1 = 直接邻居 (source==entity 的所有 target)
            2 = 邻居的邻居 (去重 + 路径不回溯)
        limit_per_hop: 每跳最多取 N 条 (防爆炸)
        """
        if hops < 1:
            return []
        types_filter = tuple(relation_types) if relation_types else self._allowed_types

        try:
            neighbors = await self._bfs(
                kind=kind,
                entity_id=entity_id,
                hops=hops,
                relation_types=types_filter,
                limit_per_hop=limit_per_hop,
            )
            try:
                from kun.core.metrics import graph_traversal_neighbors_count

                graph_traversal_neighbors_count.observe(len(neighbors))
            except Exception:
                logger.debug("graph_traversal.metric_emit_skipped", exc_info=True)
            return neighbors
        except Exception as e:
            logger.debug("graph_traversal.bfs_skipped err=%s", e)
            try:
                from kun.core.metrics import graph_traversal_neighbors_count

                graph_traversal_neighbors_count.observe(0)
            except Exception:
                logger.debug("graph_traversal.metric_emit_skipped", exc_info=True)
            return []

    async def _bfs(
        self,
        *,
        kind: str,
        entity_id: str,
        hops: int,
        relation_types: tuple[str, ...],
        limit_per_hop: int,
    ) -> list[NeighborEntity]:
        visited: set[tuple[str, str]] = {(kind, entity_id)}
        frontier: list[tuple[str, str, tuple[tuple[str, str], ...]]] = [
            (kind, entity_id, ((kind, entity_id),))
        ]
        results: list[NeighborEntity] = []

        for hop in range(1, hops + 1):
            next_frontier: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
            for src_kind, src_id, path in frontier:
                edges = await self._fetch_edges(
                    source_kind=src_kind,
                    source_id=src_id,
                    relation_types=relation_types,
                    limit=limit_per_hop,
                )
                for edge in edges:
                    target = (edge["target_kind"], edge["target_id"])
                    if target in visited:
                        continue
                    visited.add(target)
                    new_path = (*path, target)
                    results.append(
                        NeighborEntity(
                            entity_kind=edge["target_kind"],
                            entity_id=edge["target_id"],
                            relation_type=edge["relation_type"],
                            confidence=edge["confidence"],
                            pheromone_strength=edge.get("pheromone_strength", 0.0),
                            hops=hop,
                            via_path=new_path,
                        )
                    )
                    if hop < hops:
                        next_frontier.append((target[0], target[1], new_path))
            frontier = next_frontier
            if not frontier:
                break

        results.sort(key=lambda n: n.score, reverse=True)
        return results

    async def _fetch_edges(
        self,
        *,
        source_kind: str,
        source_id: str,
        relation_types: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """单次查 source_kind+source_id 的 outbound edges."""
        from sqlalchemy import select

        from kun.core.orm import EntityRelationshipRow
        from kun.core.tenancy import current_tenant

        if self._session_factory is not None:
            sess_cm = self._session_factory()
        else:
            from kun.core.db import session_scope

            sess_cm = session_scope()

        try:
            tenant = current_tenant()
            tenant_id = tenant.tenant_id
        except Exception:
            tenant_id = None

        async with sess_cm as session:
            stmt = (
                select(EntityRelationshipRow)
                .where(EntityRelationshipRow.source_entity_kind == source_kind)
                .where(EntityRelationshipRow.source_entity_id == source_id)
                .where(EntityRelationshipRow.relation_type.in_(relation_types))
            )
            if tenant_id is not None:
                stmt = stmt.where(EntityRelationshipRow.tenant_id == tenant_id)
            stmt = stmt.order_by(EntityRelationshipRow.confidence.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "target_kind": r.target_entity_kind,
                    "target_id": r.target_entity_id,
                    "relation_type": r.relation_type,
                    "confidence": r.confidence,
                    "pheromone_strength": getattr(r, "pheromone_strength", 0.0),
                }
                for r in rows
            ]


__all__ = [
    "DEFAULT_TRAVERSAL_TYPES",
    "GraphTraversal",
    "NeighborEntity",
]
