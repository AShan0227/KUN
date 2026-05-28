"""V7 §9.7 — MissionDirectorRunner 单测 (Phase X.B.MDR).

测试 daemon 入口的 tick / run_forever 行为, 用 fake service + fake providers.

Coverage:
  - tick() 跑配置 task list, 每个 task 产 1 个 review
  - coverage_provider 返 None → skipped (不报错)
  - coverage_provider 抛 → error_count++, 不传染其他 task
  - active_tasks_provider 抛 → error_count++, 但不停 runner
  - service.review_mission 抛 → error_count++, 继续后续 task
  - run_forever() 在 stop() 后停
  - run_forever() 在 CancelledError 上传时清理 stats
  - tick_interval_sec ≤ 0 → ValueError
  - build_default_runner factory 装配 DB emitter
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pytest
from kun.agents.mission_director.runner import (
    MissionCoverageInputs,
    MissionDirectorRunner,
    build_default_runner,
)
from kun.agents.mission_director.service import (
    AlignmentVerdict,
    MissionAlignmentReview,
    MissionDirectorService,
)

# ============================================================
# Fake service for tick testing
# ============================================================


class _FakeService(MissionDirectorService):
    """MissionDirectorService 替身 — 记录调用, 可注入异常."""

    def __init__(self, *, raise_on_task: str | None = None) -> None:
        super().__init__()  # no emitter
        self.calls: list[dict[str, Any]] = []
        self._raise_on_task = raise_on_task

    async def review_mission(  # type: ignore[override]
        self,
        *,
        task_id: str,
        task_plan_version: str,
        info_gap_coverage: float,
        decomposition_coverage: float,
        evidence_coverage: float,
        observed_findings: list[str] | None = None,
    ) -> MissionAlignmentReview:
        self.calls.append(
            {
                "task_id": task_id,
                "task_plan_version": task_plan_version,
                "info_gap_coverage": info_gap_coverage,
                "decomposition_coverage": decomposition_coverage,
                "evidence_coverage": evidence_coverage,
                "observed_findings": list(observed_findings or []),
            }
        )
        if self._raise_on_task is not None and task_id == self._raise_on_task:
            raise RuntimeError(f"injected failure for {task_id}")
        return MissionAlignmentReview(
            review_id=f"mar-fake-{task_id}",
            task_id=task_id,
            task_plan_version=task_plan_version,
            reviewed_at=datetime.now(UTC),
            verdict=AlignmentVerdict.OK,
            alignment_score=0.9,
            findings=[],
            info_gap_coverage=info_gap_coverage,
            decomposition_coverage=decomposition_coverage,
            evidence_coverage=evidence_coverage,
        )


def _make_inputs(
    *,
    task_plan_version: str = "v1",
    info_gap: float = 0.9,
    decomp: float = 0.85,
    evidence: float = 0.7,
    findings: list[str] | None = None,
) -> MissionCoverageInputs:
    return MissionCoverageInputs(
        task_plan_version=task_plan_version,
        info_gap_coverage=info_gap,
        decomposition_coverage=decomp,
        evidence_coverage=evidence,
        observed_findings=list(findings or []),
    )


# ============================================================
# Constructor invariants
# ============================================================


def test_runner_rejects_non_positive_interval() -> None:
    service = _FakeService()

    async def _empty_provider(_t: str) -> MissionCoverageInputs | None:  # pragma: no cover
        return None

    async def _empty_tasks() -> Iterable[str]:  # pragma: no cover
        return []

    with pytest.raises(ValueError, match="tick_interval_sec"):
        MissionDirectorRunner(
            service=service,
            coverage_provider=_empty_provider,
            active_tasks_provider=_empty_tasks,
            tick_interval_sec=0,
        )
    with pytest.raises(ValueError, match="tick_interval_sec"):
        MissionDirectorRunner(
            service=service,
            coverage_provider=_empty_provider,
            active_tasks_provider=_empty_tasks,
            tick_interval_sec=-1.0,
        )


def test_runner_initial_stats_are_zero() -> None:
    runner = MissionDirectorRunner(
        service=_FakeService(),
        coverage_provider=lambda t: _make_inputs(),  # type: ignore[arg-type]
        active_tasks_provider=lambda: [],  # type: ignore[arg-type]
        tick_interval_sec=1.0,
    )
    assert runner.stats == {
        "tick_count": 0,
        "review_count": 0,
        "skipped_count": 0,
        "error_count": 0,
    }


# ============================================================
# tick() — happy path
# ============================================================


async def test_tick_runs_review_for_each_active_task() -> None:
    service = _FakeService()

    async def _coverage(task_id: str) -> MissionCoverageInputs | None:
        return _make_inputs(findings=[f"finding-{task_id}"])

    async def _active_tasks() -> list[str]:
        return ["tk-1", "tk-2", "tk-3"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()

    assert len(reviews) == 3
    assert [r.task_id for r in reviews] == ["tk-1", "tk-2", "tk-3"]
    assert [c["task_id"] for c in service.calls] == ["tk-1", "tk-2", "tk-3"]
    assert service.calls[0]["observed_findings"] == ["finding-tk-1"]
    assert runner.stats == {
        "tick_count": 1,
        "review_count": 3,
        "skipped_count": 0,
        "error_count": 0,
    }


# ============================================================
# tick() — coverage_provider edge cases
# ============================================================


async def test_tick_skips_task_when_coverage_returns_none() -> None:
    service = _FakeService()

    async def _coverage(task_id: str) -> MissionCoverageInputs | None:
        if task_id == "tk-cancelled":
            return None
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        return ["tk-1", "tk-cancelled", "tk-3"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()

    assert len(reviews) == 2  # tk-cancelled skipped
    assert [c["task_id"] for c in service.calls] == ["tk-1", "tk-3"]
    assert runner.stats == {
        "tick_count": 1,
        "review_count": 2,
        "skipped_count": 1,
        "error_count": 0,
    }


async def test_tick_coverage_provider_raises_isolated_to_task() -> None:
    """One task's coverage failure doesn't poison the rest of the tick."""
    service = _FakeService()

    async def _coverage(task_id: str) -> MissionCoverageInputs | None:
        if task_id == "tk-boom":
            raise RuntimeError("DB error reading TaskPlan")
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        return ["tk-1", "tk-boom", "tk-3"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()

    assert len(reviews) == 2
    assert [c["task_id"] for c in service.calls] == ["tk-1", "tk-3"]
    assert runner.stats["error_count"] == 1
    assert runner.stats["review_count"] == 2


async def test_tick_active_tasks_provider_raises_returns_empty() -> None:
    """active_tasks_provider failure doesn't stop runner — log + count error."""
    service = _FakeService()

    async def _coverage(_t: str) -> MissionCoverageInputs | None:  # pragma: no cover
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        raise RuntimeError("DB connection refused")

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()

    assert reviews == []
    assert service.calls == []
    assert runner.stats["error_count"] == 1
    assert runner.stats["tick_count"] == 1


async def test_tick_service_raise_isolated_to_task() -> None:
    """One task's service.review_mission failure doesn't poison the rest."""
    service = _FakeService(raise_on_task="tk-boom")

    async def _coverage(_t: str) -> MissionCoverageInputs | None:
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        return ["tk-1", "tk-boom", "tk-3"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=60.0,
    )
    reviews = await runner.tick()

    assert len(reviews) == 2  # tk-boom failed but tk-1 + tk-3 succeeded
    assert {r.task_id for r in reviews} == {"tk-1", "tk-3"}
    assert runner.stats["error_count"] == 1


# ============================================================
# run_forever / stop
# ============================================================


async def test_run_forever_stops_on_stop_signal() -> None:
    service = _FakeService()
    call_count = {"n": 0}

    async def _coverage(_t: str) -> MissionCoverageInputs | None:
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        call_count["n"] += 1
        return ["tk-1"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=0.05,  # 50ms
    )

    async def _stop_later() -> None:
        await asyncio.sleep(0.15)  # 让 runner 跑 ~3 ticks
        runner.stop()

    await asyncio.gather(runner.run_forever(), _stop_later())

    # 至少跑了 1 个 tick, 收到 stop 后退出
    assert runner.stats["tick_count"] >= 1
    assert runner.stats["review_count"] >= 1


async def test_run_forever_propagates_cancellation() -> None:
    service = _FakeService()

    async def _coverage(_t: str) -> MissionCoverageInputs | None:
        return _make_inputs()

    async def _active_tasks() -> list[str]:
        return ["tk-1"]

    runner = MissionDirectorRunner(
        service=service,
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=0.05,
    )

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.08)  # let runner do 1+ ticks
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ============================================================
# build_default_runner factory
# ============================================================


def test_build_default_runner_returns_configured_runner() -> None:
    async def _coverage(_t: str) -> MissionCoverageInputs | None:  # pragma: no cover
        return None

    async def _active_tasks() -> list[str]:  # pragma: no cover
        return []

    runner = build_default_runner(
        tenant_id="tenant-test",
        coverage_provider=_coverage,
        active_tasks_provider=_active_tasks,
        tick_interval_sec=120.0,
        alignment_drift_threshold=0.8,
    )
    assert isinstance(runner, MissionDirectorRunner)
    # Threshold passed through to service
    assert runner._service._drift_threshold == 0.8  # type: ignore[attr-defined]
    assert runner._tick_interval == 120.0  # type: ignore[attr-defined]
    # service has both emitters wired
    assert runner._service._review_emitter is not None  # type: ignore[attr-defined]
    assert runner._service._proposal_emitter is not None  # type: ignore[attr-defined]
