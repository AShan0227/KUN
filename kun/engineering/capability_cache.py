"""Realtime capability-card cache (V2.3 Wire 49).

The DB row remains the source of truth. This cache is intentionally tiny and
short-lived so hot runtime paths can read the newest capability card without
waiting for idle-batch aggregation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import CapabilityCardRow
from kun.datamodel.capability import Capability, CapabilityCard, EntityType

log = get_logger("kun.engineering.capability_cache")


@dataclass(frozen=True)
class CachedCapabilityCard:
    card: CapabilityCard
    expires_at: float


class CapabilityCardCache:
    """Small TTL cache keyed by tenant + entity."""

    def __init__(self, *, ttl_sec: float = 30.0) -> None:
        self._ttl_sec = ttl_sec
        self._cache: dict[tuple[str, EntityType, str], CachedCapabilityCard] = {}

    def invalidate(
        self,
        *,
        tenant_id: str | None = None,
        entity_type: EntityType | None = None,
        entity_id: str | None = None,
    ) -> None:
        if tenant_id is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            key_tenant, key_type, key_id = key
            if key_tenant != tenant_id:
                continue
            if entity_type is not None and key_type != entity_type:
                continue
            if entity_id is not None and key_id != entity_id:
                continue
            self._cache.pop(key, None)

    async def get_card(
        self,
        *,
        tenant_id: str,
        entity_type: EntityType,
        entity_id: str,
    ) -> CapabilityCard | None:
        key = (tenant_id, entity_type, entity_id)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.card

        try:
            async with session_scope(tenant_id=tenant_id) as session:
                row = (
                    await session.execute(
                        select(CapabilityCardRow).where(
                            CapabilityCardRow.tenant_id == tenant_id,
                            CapabilityCardRow.entity_type == entity_type,
                            CapabilityCardRow.entity_id == entity_id,
                        )
                    )
                ).scalar_one_or_none()
        except Exception as exc:
            log.debug("capability_cache.fetch_failed", entity_id=entity_id, error=str(exc))
            return None
        if row is None:
            return None

        data = dict(row.card_json or {})
        data.setdefault("entity_ref", {"entity_type": row.entity_type, "entity_id": row.entity_id})
        data["version"] = row.version
        data["maturity"] = row.maturity
        data["overall_reliability"] = row.overall_reliability
        card = CapabilityCard.model_validate(data)
        self._cache[key] = CachedCapabilityCard(card=card, expires_at=now + self._ttl_sec)
        return card

    async def best_capability(
        self,
        *,
        tenant_id: str,
        entity_type: EntityType,
        entity_id: str,
        task_type: str,
    ) -> Capability | None:
        card = await self.get_card(
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if card is None:
            return None
        return card.find_best_match(task_type)


_cache_singleton: CapabilityCardCache | None = None


def get_capability_card_cache() -> CapabilityCardCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = CapabilityCardCache()
    return _cache_singleton


def set_capability_card_cache(cache: CapabilityCardCache) -> None:
    global _cache_singleton
    _cache_singleton = cache


def reset_capability_card_cache() -> None:
    global _cache_singleton
    _cache_singleton = None


__all__ = [
    "CapabilityCardCache",
    "get_capability_card_cache",
    "reset_capability_card_cache",
    "set_capability_card_cache",
]
