"""V2.3 Wire 49 — Capability card 实时 cache (L5 缺口补齐).

L5 缺口: capability_card writeback 已有, 但反馈到 router 慢 (idle_batch).
读取 capability_card 每次查 DB → 短期没事, 长期累积偏差.

V2.3 解决: in-memory hot cache.
- record_outcome 写完同时更新 cache
- get_cached_capability — 优先查 cache (≤5min new), miss → DB
- TTL 控制 stale data: 默认 5 分钟 (符合 V2.3 §8 "5-10 分钟生效" 目标)

跟 capability_writeback.py 的 record_outcome() 解耦 — 通过 hook 更新 cache.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


CAPABILITY_CACHE_TTL_SEC = 300  # 5 分钟


@dataclass
class _CacheEntry:
    """单条 cache."""

    payload: dict[str, Any]
    cached_at: float

    def is_fresh(self, ttl: int = CAPABILITY_CACHE_TTL_SEC) -> bool:
        return (time.time() - self.cached_at) < ttl


class CapabilityCache:
    """In-memory hot cache for capability_card lookups.

    Key: (tenant_id, entity_type, entity_id, task_type) → payload dict
    """

    def __init__(self, *, ttl_sec: int = CAPABILITY_CACHE_TTL_SEC) -> None:
        self._cache: dict[tuple[str, str, str, str], _CacheEntry] = {}
        self._ttl = ttl_sec
        self._stats = {"hits": 0, "misses": 0, "writes": 0, "stale_evictions": 0}

    def get(
        self,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        task_type: str,
    ) -> dict[str, Any] | None:
        """查 cache. None = miss / expired."""
        key = (tenant_id, entity_type, entity_id, task_type)
        entry = self._cache.get(key)
        if entry is None:
            self._stats["misses"] += 1
            return None
        if not entry.is_fresh(self._ttl):
            self._stats["stale_evictions"] += 1
            del self._cache[key]
            return None
        self._stats["hits"] += 1
        return entry.payload

    def put(
        self,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        task_type: str,
        payload: dict[str, Any],
    ) -> None:
        """写 cache. 可在 record_outcome 后立即调."""
        key = (tenant_id, entity_type, entity_id, task_type)
        self._cache[key] = _CacheEntry(payload=dict(payload), cached_at=time.time())
        self._stats["writes"] += 1

    def invalidate(
        self,
        tenant_id: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        task_type: str | None = None,
    ) -> int:
        """选择性 invalidate. 返删除条数. 全 None → 删该 tenant 所有."""
        to_remove = []
        for key in self._cache:
            if key[0] != tenant_id:
                continue
            if entity_type is not None and key[1] != entity_type:
                continue
            if entity_id is not None and key[2] != entity_id:
                continue
            if task_type is not None and key[3] != task_type:
                continue
            to_remove.append(key)
        for key in to_remove:
            del self._cache[key]
        return len(to_remove)

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def hit_rate(self) -> float:
        total = self._stats["hits"] + self._stats["misses"]
        return self._stats["hits"] / total if total > 0 else 0.0

    def reset(self) -> None:
        self._cache.clear()
        self._stats = {"hits": 0, "misses": 0, "writes": 0, "stale_evictions": 0}

    def __len__(self) -> int:
        return len(self._cache)


_cache_singleton: CapabilityCache | None = None


def get_capability_cache() -> CapabilityCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = CapabilityCache()
    return _cache_singleton


def reset_capability_cache() -> None:
    global _cache_singleton
    _cache_singleton = None


__all__ = [
    "CAPABILITY_CACHE_TTL_SEC",
    "CapabilityCache",
    "get_capability_cache",
    "reset_capability_cache",
]
