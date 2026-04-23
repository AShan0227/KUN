"""idle-batch 调度器 (§6.4) — 用户闲置时批处理.

统一承载所有离线学习 / 评估 / 进化:
  - 任务回放 (task_replay)
  - 多样本一致性测试 (consistency_test)
  - 方法论蒸馏 (methodology_distill)
  - 知识冲突解决 (knowledge_conflict)
  - AB 决策汇总 (ab_decision_roll_up)
  - 健康报告生成 (health_report)
  - 路由规律涌现发现 (route_rule_mining)

每项都是一个 `IdleBatchStep`, 可独立开关 (ADR "用户可关").

Walking skeleton: 注册 6 类 step 的占位实现, 实际逻辑由 follow-on commits 填.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.engineering.idle_batch")


@dataclass
class StepReport:
    step_id: str
    started_at: datetime
    finished_at: datetime
    status: str  # ok / failed / skipped
    summary: dict[str, Any]


class IdleBatchStep(ABC):
    """A single step run during idle-batch."""

    step_id: str

    @abstractmethod
    async def run(self, tenant_id: str) -> dict[str, Any]: ...


# ============= Registry ============


_steps: dict[str, IdleBatchStep] = {}


def register_step(step: IdleBatchStep) -> None:
    _steps[step.step_id] = step


def list_steps() -> list[str]:
    return sorted(_steps)


def get_step(step_id: str) -> IdleBatchStep | None:
    return _steps.get(step_id)


# ============= Runner ============


async def run_all(
    tenant_id: str,
    *,
    enabled: set[str] | None = None,
) -> list[StepReport]:
    """Run all registered idle-batch steps (optionally filtered)."""
    reports: list[StepReport] = []
    names = [n for n in list_steps() if enabled is None or n in enabled]
    log.info("idle_batch.run_all.start", tenant_id=tenant_id, steps=names)
    for name in names:
        step = _steps[name]
        started = datetime.now(UTC)
        try:
            summary = await step.run(tenant_id)
            reports.append(
                StepReport(
                    step_id=name,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    status="ok",
                    summary=summary,
                )
            )
        except Exception as e:
            log.exception("idle_batch.step_failed", step=name, error=str(e))
            reports.append(
                StepReport(
                    step_id=name,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    status="failed",
                    summary={"error": str(e)},
                )
            )
    log.info("idle_batch.run_all.done", tenant_id=tenant_id, n=len(reports))
    return reports


# ============= Built-in steps (placeholders) ============


class TaskReplayStep(IdleBatchStep):
    """Replay recent historical tasks with the current router + skills, compare outputs."""

    step_id = "task_replay"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        # Placeholder: in full impl we'd sample tasks, re-run with shadow config,
        # compute metric deltas, and report.
        log.info("task_replay.placeholder", tenant_id=tenant_id)
        return {"replayed": 0, "note": "placeholder"}


class ConsistencyTestStep(IdleBatchStep):
    """Triple-perturbation (temperature / rewording / model) consistency check."""

    step_id = "consistency_test"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        log.info("consistency_test.placeholder", tenant_id=tenant_id)
        return {"samples": 0, "note": "placeholder"}


class MethodologyDistillStep(IdleBatchStep):
    """情节记忆 → 语义方法论 蒸馏."""

    step_id = "methodology_distill"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        log.info("methodology_distill.placeholder", tenant_id=tenant_id)
        return {"new_rules": 0, "note": "placeholder"}


class KnowledgeConflictStep(IdleBatchStep):
    """Resolve conflicting memories in the asset pool."""

    step_id = "knowledge_conflict"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        log.info("knowledge_conflict.placeholder", tenant_id=tenant_id)
        return {"resolved": 0, "note": "placeholder"}


class ABDecisionRollupStep(IdleBatchStep):
    """Collect AB-experiment results, promote/demote based on guardrails."""

    step_id = "ab_decision_roll_up"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        log.info("ab_decision_roll_up.placeholder", tenant_id=tenant_id)
        return {"promoted": 0, "rolled_back": 0, "note": "placeholder"}


class HealthReportStep(IdleBatchStep):
    """Generate weekly / monthly health report → NUO dashboard."""

    step_id = "health_report"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        # Produce a minimal but real snapshot here (tasks count, outbox lag, cost)
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import EventRow, RuntimeStateRow, TaskRow

        async with session_scope() as s:
            total_tasks = (
                await s.execute(
                    select(func.count()).select_from(TaskRow).where(TaskRow.tenant_id == tenant_id)
                )
            ).scalar_one()
            outbox_lag = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.published_at.is_(None))
                )
            ).scalar_one()
            cost_equiv = (
                await s.execute(
                    select(
                        func.coalesce(
                            func.sum(RuntimeStateRow.accumulated_cost_usd_equivalent), 0.0
                        )
                    ).where(RuntimeStateRow.tenant_id == tenant_id)
                )
            ).scalar_one()

        return {
            "total_tasks": int(total_tasks),
            "events_outbox_lag": int(outbox_lag),
            "lifetime_cost_usd_equivalent": float(cost_equiv),
        }


class RouteRuleMiningStep(IdleBatchStep):
    """Cluster + association-rule mining over routing logs to surface new route patterns."""

    step_id = "route_rule_mining"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        log.info("route_rule_mining.placeholder", tenant_id=tenant_id)
        return {"new_patterns": 0, "note": "placeholder"}


def register_default_steps() -> None:
    for step in [
        TaskReplayStep(),
        ConsistencyTestStep(),
        MethodologyDistillStep(),
        KnowledgeConflictStep(),
        ABDecisionRollupStep(),
        HealthReportStep(),
        RouteRuleMiningStep(),
    ]:
        register_step(step)


register_default_steps()


# ============= Long-running worker ============


async def idle_batch_worker(
    *,
    interval_sec: int = 3600,
    tenant_id: str = "u-sylvan",
    enabled: set[str] | None = None,
) -> None:
    """Background worker: every `interval_sec`, run all enabled steps.

    Started from app lifespan if KUN_IDLE_BATCH_ENABLED=true.
    """
    log.info("idle_batch.worker.start", interval_sec=interval_sec, tenant_id=tenant_id)
    while True:
        try:
            await run_all(tenant_id, enabled=enabled)
        except Exception as e:
            log.exception("idle_batch.worker.cycle_failed", error=str(e))
        await asyncio.sleep(interval_sec)


# ============= CLI helper ============


RunCallback = Callable[[list[StepReport]], Awaitable[None] | None]


async def run_once(
    tenant_id: str = "u-sylvan",
    *,
    enabled: set[str] | None = None,
    on_done: RunCallback | None = None,
) -> list[StepReport]:
    """Run one pass of all steps. Used by CLI + tests."""
    reports = await run_all(tenant_id, enabled=enabled)
    if on_done is not None:
        result = on_done(reports)
        if asyncio.iscoroutine(result):
            await result
    return reports
