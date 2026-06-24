"""MissionDirectorRunner — V7 §9.7 任务级监督周期 tick.

V7 §9.7 Mission Director 一级子系统的 daemon 入口. Phase X.B 第 5 刀.

设计 (跟 kun/external_supervisor/runner.py 风格一致, 但分层更清晰):
  - `MissionDirectorRunner` 是 service + coverage_provider 的 thin wrapper
  - 每 tick 跑一遍配置的 task_ids, 算 coverage → 调 service.review_mission()
  - service emits review 到 DB (caller 已经把 X.B.MD writer 装配进去了)
  - 不直接接 DB / 不直接做 coverage 计算 — 解耦, 易测

Coverage provider 是 Callable[[task_id], MissionCoverageInputs]:
  - 实际生产代码: 从 TaskPlan / work_items / evidence_ledger 查
  - 测试: 给 fake stub 直接返
  - Phase X.B+: 接 LongTaskOrchestrator 真状态 (V7 §9.7 任务方案对齐)

为啥不在 runner 里直接算 coverage:
  - 不同 task 类型 coverage 算法不一样 (短任务 vs 长任务 vs 内容分发 ...)
  - 算法本身要单独测
  - runner 关心的是 "周期触发 + 容错 + 关停", 不是 "怎么算"

tick 错误处理:
  - 单个 task review 失败 → log warning, 不传染其他 task
  - 整体 tick 失败 → log error, 但 loop 继续 (避免单次 DB 抖动停 runner)
  - asyncio.CancelledError → 上传 (让外层 cleanup 收尾)
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from kun.agents.mission_director.service import (
    MissionAlignmentReview,
    MissionDirectorService,
)
from kun.core.logging import get_logger

log = get_logger("kun.agents.mission_director.runner")


@dataclass(frozen=True)
class MissionCoverageInputs:
    """Coverage provider 返回的 mission tick 输入 (V7 §9.7)."""

    task_plan_version: str
    info_gap_coverage: float  # 0-1
    decomposition_coverage: float  # 0-1
    evidence_coverage: float  # 0-1
    observed_findings: list[str]


CoverageProvider = Callable[[str], Awaitable[MissionCoverageInputs | None]]
"""
Async callable: task_id → MissionCoverageInputs.
Returns None when task is already done / cancelled / not-applicable
(runner skips that task without raising).
"""


@dataclass
class _RunnerStats:
    """Mutable runner stats — V7 §13.6 frozen IO 只用在 service 输出层, runner
    内部用 mutable dataclass 算计数."""

    tick_count: int = 0
    review_count: int = 0
    skipped_count: int = 0
    error_count: int = 0


class MissionDirectorRunner:
    """V7 §9.7 任务级监督周期 tick runner.

    Usage::

        from kun.agents.mission_director import (
            MissionDirectorService,
            MissionDirectorRunner,
        )
        from kun.integration.mission_director_db import (
            make_mission_review_emitter,
            make_plan_change_proposal_emitter,
        )

        service = MissionDirectorService(
            review_emitter=make_mission_review_emitter("tenant-a"),
            proposal_emitter=make_plan_change_proposal_emitter("tenant-a"),
        )
        runner = MissionDirectorRunner(
            service=service,
            coverage_provider=my_coverage_fn,
            active_tasks_provider=my_active_tasks_fn,
            tick_interval_sec=60.0,
        )
        await runner.run_until_signal()   # or .tick() for testing
    """

    def __init__(
        self,
        *,
        service: MissionDirectorService,
        coverage_provider: CoverageProvider,
        active_tasks_provider: Callable[[], Awaitable[Iterable[str]]],
        tick_interval_sec: float = 60.0,
    ) -> None:
        if tick_interval_sec <= 0:
            raise ValueError(
                f"tick_interval_sec must be > 0, got {tick_interval_sec}"
            )
        self._service = service
        self._coverage_provider = coverage_provider
        self._active_tasks_provider = active_tasks_provider
        self._tick_interval = tick_interval_sec
        self._stats = _RunnerStats()
        self._stop_event: asyncio.Event | None = None

    @property
    def stats(self) -> dict[str, int]:
        """Snapshot of runner stats (read-only dict copy)."""
        return {
            "tick_count": self._stats.tick_count,
            "review_count": self._stats.review_count,
            "skipped_count": self._stats.skipped_count,
            "error_count": self._stats.error_count,
        }

    async def tick(self) -> list[MissionAlignmentReview]:
        """Run one tick — review every active task once.

        Returns reviews actually produced (skipped tasks excluded).
        Errors are logged & counted but don't propagate; CancelledError does.
        """
        self._stats.tick_count += 1
        produced: list[MissionAlignmentReview] = []

        try:
            task_ids = list(await self._active_tasks_provider())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(
                "mission_director.runner.tick_active_tasks_failed",
                error=f"{type(e).__name__}: {e}",
            )
            self._stats.error_count += 1
            return produced

        log.info(
            "mission_director.runner.tick_start",
            tick=self._stats.tick_count,
            n_active_tasks=len(task_ids),
        )

        for task_id in task_ids:
            try:
                inputs = await self._coverage_provider(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "mission_director.runner.coverage_failed",
                    task_id=task_id,
                    error=f"{type(e).__name__}: {e}",
                )
                self._stats.error_count += 1
                continue

            if inputs is None:
                self._stats.skipped_count += 1
                log.info(
                    "mission_director.runner.skipped_task",
                    task_id=task_id,
                    reason="coverage_provider_returned_none",
                )
                continue

            try:
                review = await self._service.review_mission(
                    task_id=task_id,
                    task_plan_version=inputs.task_plan_version,
                    info_gap_coverage=inputs.info_gap_coverage,
                    decomposition_coverage=inputs.decomposition_coverage,
                    evidence_coverage=inputs.evidence_coverage,
                    observed_findings=list(inputs.observed_findings),
                )
                produced.append(review)
                self._stats.review_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "mission_director.runner.review_failed",
                    task_id=task_id,
                    error=f"{type(e).__name__}: {e}",
                )
                self._stats.error_count += 1

        log.info(
            "mission_director.runner.tick_end",
            tick=self._stats.tick_count,
            n_reviews=len(produced),
            stats=self.stats,
        )
        return produced

    async def run_forever(self) -> None:
        """Loop tick() forever, sleeping tick_interval_sec between ticks.

        Stops on CancelledError or when stop() is called.
        """
        self._stop_event = asyncio.Event()
        try:
            while not self._stop_event.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._tick_interval
                    )
                    # stop_event was set during the sleep
                    break
                except TimeoutError:
                    continue  # normal tick interval elapsed
        except asyncio.CancelledError:
            log.info("mission_director.runner.cancelled", stats=self.stats)
            raise

    def stop(self) -> None:
        """Signal run_forever() to exit at next interval boundary."""
        if self._stop_event is not None:
            self._stop_event.set()


# ============================================================
# Factory — wire service + DB emitters + default tick interval
# ============================================================


def build_default_runner(
    *,
    tenant_id: str,
    coverage_provider: CoverageProvider,
    active_tasks_provider: Callable[[], Awaitable[Iterable[str]]],
    tick_interval_sec: float = 60.0,
    alignment_drift_threshold: float = 0.7,
    alignment_off_anchor_threshold: float = 0.4,
    alignment_needs_human_threshold: float = 0.2,
) -> MissionDirectorRunner:
    """Build a Runner with DB-backed emitters wired up.

    Caller still provides coverage_provider + active_tasks_provider (these need
    application-domain logic — TaskPlan parse, work_items query, etc.).

    Example wiring in app startup::

        async def _coverage(task_id: str) -> MissionCoverageInputs | None:
            # ... query TaskPlan + work_items + evidence_ledger ...
            return MissionCoverageInputs(...)

        async def _active_tasks() -> list[str]:
            # ... query tasks table where status='active' ...
            return [...]

        runner = build_default_runner(
            tenant_id="tenant-prod",
            coverage_provider=_coverage,
            active_tasks_provider=_active_tasks,
        )
        await runner.run_forever()
    """
    from kun.integration.mission_director_db import (
        make_mission_review_emitter,
        make_plan_change_proposal_emitter,
    )

    service = MissionDirectorService(
        review_emitter=make_mission_review_emitter(tenant_id),
        proposal_emitter=make_plan_change_proposal_emitter(tenant_id),
        alignment_drift_threshold=alignment_drift_threshold,
        alignment_off_anchor_threshold=alignment_off_anchor_threshold,
        alignment_needs_human_threshold=alignment_needs_human_threshold,
    )
    return MissionDirectorRunner(
        service=service,
        coverage_provider=coverage_provider,
        active_tasks_provider=active_tasks_provider,
        tick_interval_sec=tick_interval_sec,
    )


# ============================================================
# Standalone entrypoint — `python -m kun.agents.mission_director.runner`
# ============================================================


async def _signal_aware_loop(runner: MissionDirectorRunner) -> None:  # pragma: no cover
    """Production-only main loop with SIGTERM/SIGINT graceful shutdown."""
    import signal

    loop = asyncio.get_event_loop()

    def _shutdown(signame: str) -> None:
        log.info("mission_director.runner.shutdown_signal", signal=signame)
        runner.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _shutdown, sig.name)

    await runner.run_forever()


__all__ = [
    "CoverageProvider",
    "MissionCoverageInputs",
    "MissionDirectorRunner",
    "build_default_runner",
]
