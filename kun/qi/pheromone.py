"""V2.3 Wire 43 — Pheromone 涌现 (V2.3 §6).

生物群体 swarm 启发: 不是"显式记规则", 是从行为里自然涌现.
- 蚂蚁找路: 走过留信息素, 后蚁跟信息素强的走
- KUN: 多 task 走过的 (skill_X → skill_Y) 路径 → pheromone +0.05
- 每日衰减: pheromone × 0.95 (没人走的慢慢遗忘)
- GraphTraversal 选邻居: confidence × (0.5 + pheromone) (基础 0.5 + 加成)

跟 entity_relationships table (V2.2 §20 已有) 联动. 加 2 列 (Wire 43 alembic 0016):
- pheromone_strength: 0.0-1.0
- last_reinforced_at: 最近一次走这条边的时间

写入 hook: orchestrator step 完成后调 reinforce_pheromone(prior_step.skill, step.skill)
衰减 cron: idle_batch daily step 调 decay_all_pheromone(decay_rate=0.95)
消费: GraphTraversal 选 neighbor 时用 score = confidence × (0.5 + pheromone)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


PHEROMONE_REINFORCE_INCREMENT = 0.05  # 每次走过 +0.05
PHEROMONE_DECAY_RATE = 0.95  # 每日 ×0.95
PHEROMONE_MAX = 1.0  # 上限
PHEROMONE_BASE_FACTOR = 0.5  # GraphTraversal 评分时基础 (无 pheromone 也有 0.5)


SessionFactory = Callable[..., Any]


class PheromoneStorage:
    """Pheromone 写入 / 衰减 / 查询 — SQL 后端 (entity_relationships 表).

    没接 DB 时 (e.g. 单元测试): 用 InMemoryPheromoneStorage 替代.
    """

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory

    def _open(self, tenant_id: str) -> Any:
        if self._session_factory is not None:
            return self._session_factory(tenant_id=tenant_id)
        from kun.core.db import session_scope

        return session_scope(tenant_id=tenant_id)

    async def reinforce(
        self,
        tenant_id: str,
        *,
        source_kind: str,
        source_id: str,
        target_kind: str,
        target_id: str,
        relation_type: str = "follows",
        increment: float = PHEROMONE_REINFORCE_INCREMENT,
    ) -> None:
        """加强一条边的 pheromone (走过 → +increment, max 1.0).

        如果边不存在, 创建它 (confidence=0.3 起步).
        """
        from sqlalchemy import text

        from kun.core.ids import new_id

        async with self._open(tenant_id) as session:
            # 先查
            result = await session.execute(
                text(
                    "SELECT relation_id, pheromone_strength FROM entity_relationships "
                    "WHERE tenant_id=:t AND source_entity_kind=:sk AND source_entity_id=:sid "
                    "AND target_entity_kind=:tk AND target_entity_id=:tid AND relation_type=:rt"
                ),
                {
                    "t": tenant_id,
                    "sk": source_kind,
                    "sid": source_id,
                    "tk": target_kind,
                    "tid": target_id,
                    "rt": relation_type,
                },
            )
            row = result.first()
            now = datetime.now(UTC)
            if row is None:
                # 新建边
                await session.execute(
                    text(
                        "INSERT INTO entity_relationships ("
                        "relation_id, tenant_id, source_entity_kind, source_entity_id, "
                        "target_entity_kind, target_entity_id, relation_type, "
                        "confidence, evidence_count, pheromone_strength, "
                        "last_reinforced_at, last_reinforced_at) "
                        "VALUES (:rid, :t, :sk, :sid, :tk, :tid, :rt, "
                        ":conf, 1, :pheromone, :now, :now)"
                    ),
                    {
                        "rid": new_id("memory"),
                        "t": tenant_id,
                        "sk": source_kind,
                        "sid": source_id,
                        "tk": target_kind,
                        "tid": target_id,
                        "rt": relation_type,
                        "conf": 0.3,
                        "pheromone": min(PHEROMONE_MAX, increment),
                        "now": now,
                    },
                )
            else:
                relation_id, current = row
                new_pheromone = min(PHEROMONE_MAX, current + increment)
                await session.execute(
                    text(
                        "UPDATE entity_relationships SET pheromone_strength=:p, "
                        "last_reinforced_at=:now WHERE relation_id=:rid AND tenant_id=:t"
                    ),
                    {
                        "p": new_pheromone,
                        "now": now,
                        "rid": relation_id,
                        "t": tenant_id,
                    },
                )

    async def decay_all(
        self,
        *,
        decay_rate: float = PHEROMONE_DECAY_RATE,
        tenant_id: str | None = None,
    ) -> int:
        """衰减所有边 pheromone × decay_rate. 返影响行数.

        cron 用 (idle_batch daily). 没人走的边慢慢遗忘.
        """
        from sqlalchemy import text

        async with self._open(tenant_id or "u-sylvan") as session:
            stmt = "UPDATE entity_relationships SET pheromone_strength = pheromone_strength * :rate WHERE pheromone_strength > 0.001"
            params = {"rate": decay_rate}
            if tenant_id is not None:
                stmt += " AND tenant_id = :t"
                params["t"] = tenant_id
            result = await session.execute(text(stmt), params)
            return int(getattr(result, "rowcount", 0) or 0)


class InMemoryPheromoneStorage:
    """单元测试用. dict 模拟 entity_relationships 表 (只保 pheromone 相关字段)."""

    def __init__(self) -> None:
        # (tenant, src_kind, src_id, tgt_kind, tgt_id, relation_type) → pheromone_strength
        self._edges: dict[tuple[str, str, str, str, str, str], float] = {}
        self._last_reinforced: dict[tuple[str, ...], datetime] = {}

    async def reinforce(
        self,
        tenant_id: str,
        *,
        source_kind: str,
        source_id: str,
        target_kind: str,
        target_id: str,
        relation_type: str = "follows",
        increment: float = PHEROMONE_REINFORCE_INCREMENT,
    ) -> None:
        key = (tenant_id, source_kind, source_id, target_kind, target_id, relation_type)
        current = self._edges.get(key, 0.0)
        self._edges[key] = min(PHEROMONE_MAX, current + increment)
        self._last_reinforced[key] = datetime.now(UTC)

    async def decay_all(
        self,
        *,
        decay_rate: float = PHEROMONE_DECAY_RATE,
        tenant_id: str | None = None,
    ) -> int:
        affected = 0
        for key, value in list(self._edges.items()):
            if tenant_id is not None and key[0] != tenant_id:
                continue
            if value > 0.001:
                self._edges[key] = value * decay_rate
                affected += 1
        return affected

    def get_pheromone(
        self,
        tenant_id: str,
        source_kind: str,
        source_id: str,
        target_kind: str,
        target_id: str,
        relation_type: str = "follows",
    ) -> float:
        return self._edges.get(
            (tenant_id, source_kind, source_id, target_kind, target_id, relation_type),
            0.0,
        )

    def reset(self) -> None:
        self._edges.clear()
        self._last_reinforced.clear()


_storage_singleton: PheromoneStorage | InMemoryPheromoneStorage | None = None


def get_pheromone_storage() -> PheromoneStorage | InMemoryPheromoneStorage:
    """单例. 默认 InMemory (生产 install_runtime 时换成 SQL)."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = InMemoryPheromoneStorage()
    return _storage_singleton


def set_pheromone_storage(storage: PheromoneStorage | InMemoryPheromoneStorage) -> None:
    """生产用 — install_runtime 调一次, 装 SQL storage."""
    global _storage_singleton
    _storage_singleton = storage


def reset_pheromone_storage() -> None:
    """测试用."""
    global _storage_singleton
    _storage_singleton = None


def neighbor_pheromone_score(confidence: float, pheromone: float) -> float:
    """GraphTraversal 用. 把 confidence + pheromone 合成一个分.

    score = confidence × (BASE + pheromone)
    BASE 0.5 → 即使 pheromone=0 仍有 0.5 confidence
    pheromone=1.0 → 1.5 倍 confidence (强信号)
    """
    return confidence * (PHEROMONE_BASE_FACTOR + pheromone)


__all__ = [
    "PHEROMONE_BASE_FACTOR",
    "PHEROMONE_DECAY_RATE",
    "PHEROMONE_MAX",
    "PHEROMONE_REINFORCE_INCREMENT",
    "InMemoryPheromoneStorage",
    "PheromoneStorage",
    "get_pheromone_storage",
    "neighbor_pheromone_score",
    "reset_pheromone_storage",
    "set_pheromone_storage",
]


# Awaitable stub
_ = Awaitable
