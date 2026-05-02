"""IdleBatchStep that runs KnowledgePrecipitation scheduled queues.

V2.1 wire W7: KnowledgePrecipitation hourly/daily/weekly 入队的事件
在 idle_batch_worker 周期跑 → 触发实际进化.
"""

from __future__ import annotations

import logging
from typing import Any

from kun.engineering.precipitation import (
    KnowledgePrecipitation,
    NarrativeDistillStep,
    RuleEmergeStep,
    StatsWritebackStep,
    WeightTuneStep,
)

logger = logging.getLogger(__name__)


# 全局 KnowledgePrecipitation 单例 (与 idle_batch 共享)
_kp: KnowledgePrecipitation | None = None


def get_kp() -> KnowledgePrecipitation:
    """获取全局 KP 单例 (idle_batch + orchestrator 共享)."""
    global _kp
    if _kp is None:
        _kp = KnowledgePrecipitation()
        # 默认 4 个 step
        _kp.register_step(StatsWritebackStep())
        _kp.register_step(WeightTuneStep())
        _kp.register_step(RuleEmergeStep())
        _kp.register_step(NarrativeDistillStep())
    return _kp


def reset_kp() -> None:
    """测试用."""
    global _kp
    _kp = None


class PrecipitationDailyStep:
    """idle_batch 用的 step: 每跑一次 KP 的 daily queue."""

    step_id = "precipitation_daily"
    interval_sec = 86400  # 实际由 idle_batch_worker 调度, 这是 hint

    async def run(self, tenant_id: str) -> dict[str, Any]:
        kp = get_kp()
        updates = await kp.run_scheduled("daily")
        logger.info("precipitation.daily.ran count=%d", len(updates))
        return {
            "step_id": self.step_id,
            "tenant_id": tenant_id,
            "updates_count": len(updates),
            "asset_kinds": [u.asset_kind for u in updates],
        }


class PrecipitationWeeklyStep:
    """idle_batch 用的 step: 每周跑 KP 的 weekly queue."""

    step_id = "precipitation_weekly"
    interval_sec = 86400 * 7  # hint

    async def run(self, tenant_id: str) -> dict[str, Any]:
        kp = get_kp()
        updates = await kp.run_scheduled("weekly")
        logger.info("precipitation.weekly.ran count=%d", len(updates))
        return {
            "step_id": self.step_id,
            "tenant_id": tenant_id,
            "updates_count": len(updates),
            "asset_kinds": [u.asset_kind for u in updates],
        }


def install_precipitation_steps() -> None:
    """注册 PrecipitationStep 到 idle_batch 全局 registry.

    应在 lifespan startup 调用一次, 在 idle_batch_worker 启动前.
    """
    from kun.engineering.idle_batch import list_steps, register_step

    if "precipitation_daily" not in list_steps():
        register_step(PrecipitationDailyStep())  # type: ignore[arg-type]
    if "precipitation_weekly" not in list_steps():
        register_step(PrecipitationWeeklyStep())  # type: ignore[arg-type]


__all__ = [
    "PrecipitationDailyStep",
    "PrecipitationWeeklyStep",
    "get_kp",
    "install_precipitation_steps",
    "reset_kp",
]
