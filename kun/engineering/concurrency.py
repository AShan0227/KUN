"""ConcurrencySafety (ADR-018 §16.5) — 统一并发安全机制.

合并前: 分布式锁 / 幂等键 / 版本号 / 冲突检测 / 预冲突扫描五种散在各处.
合并后: 统一在事前 + 事中入口处使用.

当前实装:
  - IdempotencyKey.check_or_record (Redis)
  - ResourceGuard.acquire / release (Redis distributed lock)
  - Version check 由 SQLAlchemy 乐观并发自动处理

后续可扩展的:
  - 预冲突扫描 (pre-conflict scanner)
  - 动作前置队列 (pending-actions queue)
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis.asyncio as aioredis

from kun.core.config import settings
from kun.core.logging import get_logger

log = get_logger("kun.engineering.concurrency")


# =================== Idempotency (Redis SETNX) ===================


@dataclass(frozen=True)
class IdempotencyResult:
    first: bool
    cached_result_ref: str | None


class IdempotencyKey:
    """Redis-backed idempotency with TTL."""

    def __init__(self, redis: aioredis.Redis, ttl_sec: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl_sec

    async def check_or_record(self, key: str, result_ref: str) -> IdempotencyResult:
        """Atomic 'record if not exists'. Returns whether this is first time."""
        full_key = f"kun:idem:{key}"
        ok = await self._redis.set(full_key, result_ref, nx=True, ex=self._ttl)
        if ok:
            return IdempotencyResult(first=True, cached_result_ref=None)
        cached = await self._redis.get(full_key)
        return IdempotencyResult(first=False, cached_result_ref=cached)


# =================== Distributed lock (Redlock lite) ===================


@dataclass
class Lease:
    resource: str
    token: str
    ttl_sec: int


class ResourceGuard:
    """Redis SET NX EX + token check on release — single-node lightweight lock.

    For production-grade Redlock, upgrade to redis-py's Redlock when multi-node.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def acquire(self, resource: str, *, ttl_sec: int = 10) -> Lease | None:
        token = uuid.uuid4().hex
        full_key = f"kun:lock:{resource}"
        ok = await self._redis.set(full_key, token, nx=True, ex=ttl_sec)
        if not ok:
            return None
        return Lease(resource=resource, token=token, ttl_sec=ttl_sec)

    async def release(self, lease: Lease) -> bool:
        full_key = f"kun:lock:{lease.resource}"
        # Lua script: only delete if token matches (avoid releasing someone else's lock)
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        result = await self._redis.eval(script, 1, full_key, lease.token)
        return bool(result)


# =================== Convenience helpers ===================


_redis_pool: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(settings().redis_url, decode_responses=True)
    return _redis_pool


@asynccontextmanager
async def acquire_or_raise(
    resource: str,
    *,
    ttl_sec: int = 10,
) -> AsyncIterator[Lease]:
    """Grab a lock or raise. Auto-releases on exit."""
    redis = await _get_redis()
    guard = ResourceGuard(redis)
    lease = await guard.acquire(resource, ttl_sec=ttl_sec)
    if lease is None:
        raise ResourceBusyError(resource)
    try:
        yield lease
    finally:
        await guard.release(lease)


class ResourceBusyError(RuntimeError):
    """Raised when a resource lock can't be acquired."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"resource busy: {resource}")
        self.resource = resource


# Backwards-compatible alias
ResourceBusy = ResourceBusyError
