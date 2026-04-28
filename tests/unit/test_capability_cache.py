"""V2.3 Wire 49: Capability cache."""

from __future__ import annotations

import time

import pytest
from kun.engineering.capability_cache import (
    CAPABILITY_CACHE_TTL_SEC,
    CapabilityCache,
    get_capability_cache,
    reset_capability_cache,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_capability_cache()
    yield
    reset_capability_cache()


def test_cache_miss_returns_none() -> None:
    cache = CapabilityCache()
    assert cache.get("u-test", "model", "claude", "writing") is None


def test_cache_put_then_hit() -> None:
    cache = CapabilityCache()
    cache.put("u-test", "model", "claude", "writing", {"score": 0.9})
    result = cache.get("u-test", "model", "claude", "writing")
    assert result == {"score": 0.9}


def test_cache_stats_track_hits_misses() -> None:
    cache = CapabilityCache()
    cache.get("u-test", "model", "claude", "writing")  # miss
    cache.put("u-test", "model", "claude", "writing", {"x": 1})
    cache.get("u-test", "model", "claude", "writing")  # hit
    cache.get("u-test", "model", "claude", "writing")  # hit
    stats = cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 2
    assert stats["writes"] == 1


def test_cache_hit_rate() -> None:
    cache = CapabilityCache()
    cache.get("u-test", "m", "x", "t")  # miss
    cache.put("u-test", "m", "x", "t", {})
    cache.get("u-test", "m", "x", "t")  # hit
    cache.get("u-test", "m", "x", "t")  # hit
    cache.get("u-test", "m", "x", "t")  # hit
    assert cache.hit_rate() == pytest.approx(0.75)  # 3/4


def test_cache_ttl_expires() -> None:
    cache = CapabilityCache(ttl_sec=1)
    cache.put("u-test", "m", "x", "t", {})
    assert cache.get("u-test", "m", "x", "t") is not None
    time.sleep(1.1)
    assert cache.get("u-test", "m", "x", "t") is None
    assert cache.stats()["stale_evictions"] == 1


def test_cache_per_tenant_isolation() -> None:
    cache = CapabilityCache()
    cache.put("u-A", "m", "x", "t", {"v": "A"})
    cache.put("u-B", "m", "x", "t", {"v": "B"})
    assert cache.get("u-A", "m", "x", "t") == {"v": "A"}
    assert cache.get("u-B", "m", "x", "t") == {"v": "B"}


def test_cache_invalidate_specific() -> None:
    cache = CapabilityCache()
    cache.put("u-test", "model", "x", "t1", {})
    cache.put("u-test", "model", "x", "t2", {})
    cache.put("u-test", "model", "y", "t1", {})
    removed = cache.invalidate("u-test", entity_type="model", entity_id="x")
    assert removed == 2
    assert cache.get("u-test", "model", "x", "t1") is None
    assert cache.get("u-test", "model", "y", "t1") is not None


def test_cache_invalidate_all_for_tenant() -> None:
    cache = CapabilityCache()
    cache.put("u-test", "m", "x", "t", {})
    cache.put("u-test", "m", "y", "t", {})
    cache.put("u-other", "m", "x", "t", {})
    removed = cache.invalidate("u-test")
    assert removed == 2
    assert cache.get("u-test", "m", "x", "t") is None
    assert cache.get("u-other", "m", "x", "t") is not None


def test_cache_singleton() -> None:
    a = get_capability_cache()
    b = get_capability_cache()
    assert a is b


def test_cache_default_ttl_5min() -> None:
    assert CAPABILITY_CACHE_TTL_SEC == 300
