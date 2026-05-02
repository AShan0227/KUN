"""V2.3 metrics_collector — gauge tick set 全套."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kun.qi.metrics_collector import _qi_window_active, collect_once


def _app_with_state() -> SimpleNamespace:
    from starlette.datastructures import State

    return SimpleNamespace(state=State())


def test_qi_window_active_force_true() -> None:
    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        assert _qi_window_active(app) is True


def test_qi_window_active_force_disable() -> None:
    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_FORCE_DISABLE": "1"}, clear=False):
        assert _qi_window_active(app) is False


def test_qi_window_active_no_config_returns_false() -> None:
    app = _app_with_state()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
        os.environ.pop("KUN_QI_FORCE_DISABLE", None)
        assert _qi_window_active(app) is False


@pytest.mark.asyncio
async def test_collect_once_no_state_doesnt_crash() -> None:
    """app.state 完全空 → collect_once 不抛."""
    app = _app_with_state()
    await collect_once(app, "u-test")


@pytest.mark.asyncio
async def test_collect_once_with_pheromone_storage() -> None:
    from kun.qi.pheromone import InMemoryPheromoneStorage

    app = _app_with_state()
    storage = InMemoryPheromoneStorage()
    await storage.reinforce(
        "u-test",
        source_kind="skill",
        source_id="a",
        target_kind="skill",
        target_id="b",
        relation_type="follows",
    )
    app.state.pheromone_storage = storage
    await collect_once(app, "u-test")

    from kun.core.metrics import pheromone_total_strength

    val = pheromone_total_strength.labels(tenant_id="u-test")._value.get()
    assert val > 0  # reinforce 产生强度


@pytest.mark.asyncio
async def test_collect_once_sets_qi_window_gauge() -> None:
    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        await collect_once(app, "u-test")

    from kun.core.metrics import qi_window_active

    val = qi_window_active.labels(tenant_id="u-test")._value.get()
    assert val == 1.0


@pytest.mark.asyncio
async def test_collect_once_with_capability_cache_hit_rate() -> None:
    from kun.engineering.capability_cache import CapabilityCardCache

    app = _app_with_state()
    cache = CapabilityCardCache()
    # 模拟 hits/misses 让 hit_rate > 0
    cache._hits["u-test"] = 3
    cache._misses["u-test"] = 1
    app.state.capability_card_cache = cache
    await collect_once(app, "u-test")

    from kun.core.metrics import capability_card_cache_hit_rate

    val = capability_card_cache_hit_rate.labels(tenant_id="u-test")._value.get()
    assert val == pytest.approx(0.75)


def test_capability_card_cache_hit_rate_method() -> None:
    """CapabilityCardCache.hit_rate(tenant) 简单 ratio."""
    from kun.engineering.capability_cache import CapabilityCardCache

    cache = CapabilityCardCache()
    assert cache.hit_rate("none") == 0.0
    cache._hits["t1"] = 6
    cache._misses["t1"] = 4
    assert cache.hit_rate("t1") == pytest.approx(0.6)
