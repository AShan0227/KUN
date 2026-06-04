"""Context storage backends.

Implementations of AssetStore:
  - InMemoryAssetStore: dev / testing
  - RedisAssetStore: simple hash-based persistence for short/medium term
  - (Postgres + Qdrant backends are added incrementally as needed)

All stores are async and tenant-scoped.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import redis.asyncio as aioredis
from qdrant_client import QdrantClient

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


# =================== Qdrant vector client ===================


_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    """Return the process-level Qdrant client.

    The client is intentionally kept here so context retrieval, importance scoring,
    and future vector stores all share the same connection settings.
    """
    global _qdrant_client
    if _qdrant_client is None:
        cfg = settings()
        _qdrant_client = QdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key,
            timeout=int(cfg.embedding_timeout_sec),
            check_compatibility=False,
        )
    return _qdrant_client


def reset_qdrant_client() -> None:
    """Clear cached Qdrant client (tests / settings reload)."""
    global _qdrant_client
    _qdrant_client = None


def _noop_use() -> None:
    # Silence unused-import warnings without side effects.
    _ = json
