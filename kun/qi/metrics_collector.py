"""V2.3 Prometheus gauge metrics collector — 30s tick set 各 gauge.

Counter 类 metric 在事件发生时 .inc() (orchestrator/idle_batch/protocol promote
都接好了). Gauge 类 metric 需要"当前值" → 定时 set:

- kun_qi_window_active — 启窗口当前是否活跃
- kun_qi_daily_spent_usd — 启今日花费
- kun_pheromone_total_strength — Pheromone 全图总强度
- kun_capability_card_cache_hit_rate — CapabilityCardCache hit rate
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.qi.metrics_collector")


async def collect_once(app: Any, tenant_id: str) -> None:
    """一次 collection. 跑前不守门 (启窗口外也要 set 0 让 dashboard 看到关闭状态)."""
    try:
        from kun.core.metrics import (
            capability_card_cache_hit_rate,
            pheromone_total_strength,
            qi_daily_spent_usd,
            qi_window_active,
        )

        # 1. 启窗口活跃
        active = _qi_window_active(app)
        qi_window_active.labels(tenant_id=tenant_id).set(1.0 if active else 0.0)

        # 2. 启今日花费
        budget = getattr(app.state, "qi_budget", None)
        if budget is not None:
            try:
                spent = budget.get_today_spent(tenant_id)
                qi_daily_spent_usd.labels(tenant_id=tenant_id).set(spent)
            except Exception:
                pass

        # 3. Pheromone 总强度
        storage = getattr(app.state, "pheromone_storage", None)
        if storage is not None:
            try:
                total = await _pheromone_total(storage, tenant_id)
                pheromone_total_strength.labels(tenant_id=tenant_id).set(total)
            except Exception:
                log.debug("pheromone_total_collect.failed", exc_info=True)

        # 4. CapabilityCardCache hit rate
        cache = getattr(app.state, "capability_card_cache", None)
        if cache is not None:
            try:
                rate = cache.hit_rate(tenant_id)
                capability_card_cache_hit_rate.labels(tenant_id=tenant_id).set(rate)
            except Exception:
                log.debug("cache_hit_rate_collect.failed", exc_info=True)
    except Exception:
        log.exception("v23_metrics_collect.failed")


def _qi_window_active(app: Any) -> bool:
    if os.getenv("KUN_QI_FORCE_DISABLE") == "1":
        return False
    if os.getenv("KUN_QI_FORCE_ACTIVE") == "1":
        return True
    qi_window = getattr(app.state, "qi_window_config", None)
    if qi_window is None:
        return False
    try:
        from kun.qi.window import is_qi_window_active

        return is_qi_window_active(qi_window)
    except Exception:
        return False


async def _pheromone_total(storage: Any, tenant_id: str) -> float:
    """求总强度. InMemory 直接 sum dict; SQL 跑 aggregate query."""
    if hasattr(storage, "_edges"):
        return float(sum(v for (t, *_), v in storage._edges.items() if t == tenant_id))
    # SQL backend
    try:
        from sqlalchemy import text

        from kun.core.db import session_scope

        async with session_scope(tenant_id=tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT COALESCE(SUM(pheromone_strength), 0) FROM entity_relationships WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
            row = result.scalar()
            return float(row or 0.0)
    except Exception:
        return 0.0


async def start_v23_metrics_collector(
    app: Any,
    tenant_id: str,
    *,
    tick_sec: float = 30.0,
) -> None:
    """主 loop. 30s 一次 set 所有 gauge. KUN_V23_METRICS_COLLECTOR_TICK_SEC 改 tick."""
    tick = float(os.getenv("KUN_V23_METRICS_COLLECTOR_TICK_SEC", str(tick_sec)))
    log.info("v23_metrics_collector.started", tenant=tenant_id, tick_sec=tick)
    while True:
        await collect_once(app, tenant_id)
        await asyncio.sleep(tick)


__all__ = ["collect_once", "start_v23_metrics_collector"]
