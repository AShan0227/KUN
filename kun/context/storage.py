"""Context storage backends.

Implementations of AssetStore:
  - InMemoryAssetStore: dev / testing
  - RedisAssetStore: simple hash-based persistence for short/medium term
  - (Postgres + Qdrant backends are added incrementally as needed)

All stores are async and tenant-scoped.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import redis.asyncio as aioredis

from kun.context.assets import AssetKind, LayeredAsset
from kun.core.config import settings
from kun.core.logging import get_logger

log = get_logger("kun.context.storage")


class AssetStore(Protocol):
    async def put(self, asset: LayeredAsset) -> None: ...
    async def get(self, asset_id: str, *, tenant_id: str) -> LayeredAsset | None: ...
    async def list(
        self,
        *,
        tenant_id: str,
        asset_kind: AssetKind | None = None,
        limit: int = 100,
    ) -> list[LayeredAsset]: ...
    async def delete(self, asset_id: str, *, tenant_id: str) -> bool: ...


# =================== In-memory (testing / scratch) ===================


class InMemoryAssetStore:
    """Process-local store. Usable in tests & local dev without Redis."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], LayeredAsset] = {}

    async def put(self, asset: LayeredAsset) -> None:
        self._data[(asset.tenant_id, asset.asset_id)] = asset
        log.debug("asset.mem.put", id=asset.asset_id, kind=asset.asset_kind)

    async def get(self, asset_id: str, *, tenant_id: str) -> LayeredAsset | None:
        asset = self._data.get((tenant_id, asset_id))
        if asset is not None:
            asset.touch()
        return asset

    async def list(
        self,
        *,
        tenant_id: str,
        asset_kind: AssetKind | None = None,
        limit: int = 100,
    ) -> list[LayeredAsset]:
        out: list[LayeredAsset] = []
        for (t, _aid), asset in self._data.items():
            if t != tenant_id:
                continue
            if asset_kind is not None and asset.asset_kind != asset_kind:
                continue
            out.append(asset)
            if len(out) >= limit:
                break
        return out

    async def delete(self, asset_id: str, *, tenant_id: str) -> bool:
        return self._data.pop((tenant_id, asset_id), None) is not None


# =================== Redis-backed ===================


class RedisAssetStore:
    """Redis HSET per (tenant_id, asset_kind). Values are JSON-serialized LayeredAsset."""

    def __init__(self, redis: Any) -> None:
        self._r = redis

    async def ping(self) -> None:
        await self._r.ping()

    @staticmethod
    def _key(tenant_id: str, asset_kind: str) -> str:
        return f"kun:assets:{tenant_id}:{asset_kind}"

    @staticmethod
    def _index_key(tenant_id: str) -> str:
        return f"kun:assets:{tenant_id}:index"

    async def put(self, asset: LayeredAsset) -> None:
        key = self._key(asset.tenant_id, asset.asset_kind)
        await self._r.hset(
            key,
            asset.asset_id,
            asset.model_dump_json(),
        )
        await self._r.sadd(self._index_key(asset.tenant_id), f"{asset.asset_kind}:{asset.asset_id}")

    async def get(self, asset_id: str, *, tenant_id: str) -> LayeredAsset | None:
        # Since asset_id doesn't encode kind, we must look up index first.
        index = await self._r.smembers(self._index_key(tenant_id))
        hit_kind: str | None = None
        for entry in index:
            kind, _, aid = entry.partition(":")
            if aid == asset_id:
                hit_kind = kind
                break
        if hit_kind is None:
            return None
        raw = await self._r.hget(self._key(tenant_id, hit_kind), asset_id)
        if raw is None:
            return None
        asset = LayeredAsset.model_validate_json(raw)
        asset.touch()
        # Persist touch (best effort)
        await self._r.hset(self._key(tenant_id, hit_kind), asset_id, asset.model_dump_json())
        return asset

    async def list(
        self,
        *,
        tenant_id: str,
        asset_kind: AssetKind | None = None,
        limit: int = 100,
    ) -> list[LayeredAsset]:
        kinds: list[str]
        if asset_kind is not None:
            kinds = [asset_kind]
        else:
            # Enumerate distinct kinds from the index
            index = await self._r.smembers(self._index_key(tenant_id))
            kinds = sorted({entry.partition(":")[0] for entry in index})

        out: list[LayeredAsset] = []
        for kind in kinds:
            items = await self._r.hvals(self._key(tenant_id, kind))
            for raw in items:
                out.append(LayeredAsset.model_validate_json(raw))
                if len(out) >= limit:
                    return out
        return out

    async def delete(self, asset_id: str, *, tenant_id: str) -> bool:
        index = await self._r.smembers(self._index_key(tenant_id))
        for entry in index:
            kind, _, aid = entry.partition(":")
            if aid == asset_id:
                deleted = await self._r.hdel(self._key(tenant_id, kind), asset_id)
                await self._r.srem(self._index_key(tenant_id), entry)
                return bool(deleted)
        return False


# =================== Factory ===================


_store: AssetStore | None = None


def get_store() -> AssetStore:
    """Return the process-level store.

    Defaults to InMemoryAssetStore if Redis URL isn't reachable / not wanted.
    """
    global _store
    if _store is None:
        _store = InMemoryAssetStore()
    return _store


def set_store(store: AssetStore) -> None:
    """Override the cached store (tests / startup)."""
    global _store
    _store = store


def reset_store() -> None:
    global _store
    _store = None


async def build_redis_store() -> RedisAssetStore:
    """Helper to construct a Redis-backed store from settings."""
    client = aioredis.from_url(settings().redis_url, decode_responses=True)
    return RedisAssetStore(client)


async def configure_store_from_settings() -> str:
    """Install the configured process-level asset store.

    `auto` tries Redis and falls back to memory. This keeps local/dev startup
    usable while making the memory store's durability boundary explicit.
    """

    backend = os.getenv("KUN_CONTEXT_STORE_BACKEND", "auto").strip().lower()
    if backend in {"memory", "inmemory", "in-memory"}:
        set_store(InMemoryAssetStore())
        return "memory"
    if backend not in {"auto", "redis"}:
        log.warning("asset_store.unknown_backend", backend=backend)
        set_store(InMemoryAssetStore())
        return "memory"
    try:
        store = await build_redis_store()
        await store.ping()
        set_store(store)
        log.info("asset_store.redis.ready", url=settings().redis_url)
        return "redis"
    except Exception as exc:
        if backend == "redis":
            log.error("asset_store.redis_unavailable", error=str(exc))
        else:
            log.warning("asset_store.redis_fallback_memory", error=str(exc))
        set_store(InMemoryAssetStore())
        return "memory"


def _noop_use() -> None:
    # Silence unused-import warnings without side effects.
    _ = json
