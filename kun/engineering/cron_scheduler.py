"""Async cron-based scheduler (V2.1 wire M4 part2).

替换 idle_batch_worker 的 fixed-interval polling. 支持真 cron 表达式
(每周一 9am / 每天 0 点 / etc) — 不再依赖 KUN_IDLE_BATCH_INTERVAL_SEC
固定间隔, 而是按"业务周期"调度.

Zero-dep 实现 (不引 APScheduler / croniter), 因为我们只需 5 字段 cron + 4
个 @preset, 不需要复杂调度.

支持表达式:
- "minute hour day_of_month month day_of_week"
- 字段值: `*` | `n` | `*/n` | `n,m,k` (列表)
- 不支持: `n-m` (范围) / @reboot 之类

@preset:
- @hourly = "0 * * * *"
- @daily  = "0 0 * * *"
- @weekly = "0 0 * * 0" (周日 0 点)
- @monthly = "0 0 1 * *"

day_of_week: cron 标准 0=Sunday, 6=Saturday (跟 Python datetime.weekday() 不同,
内部转换).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


PRESETS: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


@dataclass
class CronJob:
    name: str
    cron_expr: str
    callback: Callable[[], Awaitable[None]]
    last_run: datetime | None = None
    enabled: bool = True
    run_count: int = 0
    error_count: int = 0
    last_error: str = ""

    def disable(self) -> None:
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True


def _field_matches(field_str: str, value: int) -> bool:
    """检查 cron 单字段是否 match value."""
    if field_str == "*":
        return True
    if field_str.startswith("*/"):
        try:
            n = int(field_str[2:])
        except ValueError:
            return False
        return n > 0 and value % n == 0
    if "," in field_str:
        try:
            allowed = {int(x) for x in field_str.split(",") if x}
        except ValueError:
            return False
        return value in allowed
    try:
        return int(field_str) == value
    except ValueError:
        return False


def cron_matches(expr: str, when: datetime) -> bool:
    """检查 cron 表达式是否 match 当前时间."""
    if expr in PRESETS:
        expr = PRESETS[expr]
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    # cron: 0=Sunday, 6=Saturday; Python datetime.weekday(): 0=Monday, 6=Sunday
    cron_dow = (when.weekday() + 1) % 7  # Mon→1, Sun→0
    return (
        _field_matches(minute, when.minute)
        and _field_matches(hour, when.hour)
        and _field_matches(dom, when.day)
        and _field_matches(month, when.month)
        and _field_matches(dow, cron_dow)
    )


class CronScheduler:
    """Async cron-based scheduler. 每分钟 wake-up + match check + dispatch.

    用法:
        sched = CronScheduler()
        sched.register("idle_batch", "@hourly", lambda: run_idle_batch())
        asyncio.create_task(sched.run_forever())
    """

    def __init__(self, *, tick_sec: int = 60) -> None:
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self.tick_sec = tick_sec
        self._inflight: set[asyncio.Task[None]] = set()

    def register(
        self,
        name: str,
        cron_expr: str,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """注册 job. cron_expr 支持 5 字段 或 @preset."""
        # 验证 expr 合法
        normalized = PRESETS.get(cron_expr, cron_expr)
        if len(normalized.split()) != 5:
            raise ValueError(f"invalid cron expr: {cron_expr}")
        self._jobs[name] = CronJob(name=name, cron_expr=cron_expr, callback=callback)

    def unregister(self, name: str) -> bool:
        return self._jobs.pop(name, None) is not None

    def list_jobs(self) -> list[str]:
        return sorted(self._jobs.keys())

    def get_job(self, name: str) -> CronJob | None:
        return self._jobs.get(name)

    async def tick(self, now: datetime | None = None) -> list[str]:
        """单次 tick: 检查所有 job, fire 匹配的, 返已 fire 的 job names.

        测试用; run_forever 内部调.
        """
        now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
        fired: list[str] = []
        for job in self._jobs.values():
            if not job.enabled:
                continue
            # 同一分钟不重跑
            if job.last_run is not None and job.last_run >= now:
                continue
            if cron_matches(job.cron_expr, now):
                job.last_run = now
                fired.append(job.name)
                # 后台跑, 不阻塞 tick (避免一个 job hang 影响其他)
                task = asyncio.create_task(self._safe_run(job))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        return fired

    async def run_forever(self) -> None:
        """主 loop. 每 tick_sec 跑一次 tick."""
        self._running = True
        logger.info("cron_scheduler.started jobs=%s", self.list_jobs())
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("cron_scheduler.tick_failed")
            # sleep 到下个 tick_sec 边界
            now = datetime.now(UTC)
            sleep_sec = self.tick_sec - (now.second % self.tick_sec)
            await asyncio.sleep(max(1, sleep_sec))

    async def _safe_run(self, job: CronJob) -> None:
        try:
            await job.callback()
            job.run_count += 1
        except Exception as e:
            job.error_count += 1
            job.last_error = str(e)
            logger.exception("cron_job.%s.failed", job.name)

    def stop(self) -> None:
        self._running = False


__all__ = [
    "PRESETS",
    "CronJob",
    "CronScheduler",
    "cron_matches",
]
