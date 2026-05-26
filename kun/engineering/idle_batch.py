"""idle-batch 调度器 (§6.4) — 用户闲置时批处理.

当前实现状态:
  - health_report             : ✅ 真实 — 读 tasks/outbox/cost 计数器, 给 NUO 用
  - task_replay               : ⚠️  STUB — 只数失败任务, 没回放
  - consistency_test          : ⚠️  STUB — 只数低 reliability 卡片
  - methodology_distill       : ✅ L2.9 真做 — 扫 dev_logs → 输出 novel candidates
  - knowledge_conflict        : ⚠️  STUB — 只数冲突事件
  - ab_decision_roll_up       : ⚠️  STUB — 只数 promotion/rollback
  - route_rule_mining         : ⚠️  STUB — 只数 fallback 事件

STUB step 上报 status="stub"; 真实 step 上报 status="ok". run_all 会把两者
区分给 dashboard, 别误以为自演化在跑.

每项都是一个 `IdleBatchStep`, 可独立开关 (ADR "用户可关").
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger
from kun.core.tenancy import TenantContext, tenant_scope

log = get_logger("kun.engineering.idle_batch")


@dataclass
class StepReport:
    step_id: str
    started_at: datetime
    finished_at: datetime
    status: str  # ok / failed / skipped
    summary: dict[str, Any]


class IdleBatchStep(ABC):
    """A single step run during idle-batch.

    `stub=True` means the step's run() only reads counters / probes — it does
    not perform the action implied by its name. Stub steps report status="stub"
    so dashboards and tests don't mistake them for completed work.
    """

    step_id: str
    stub: bool = False

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
    with tenant_scope(TenantContext(tenant_id=tenant_id)):
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
                        status="stub" if getattr(step, "stub", False) else "ok",
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


# ============= Built-in steps ============


class TaskReplayStep(IdleBatchStep):
    """Replay recent historical tasks — STUB: only counts failed tasks, doesn't replay."""

    step_id = "task_replay"
    stub = True

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import TaskResultRow

        async with session_scope(tenant_id=tenant_id) as s:
            total = (
                await s.execute(
                    select(func.count())
                    .select_from(TaskResultRow)
                    .where(TaskResultRow.tenant_id == tenant_id)
                )
            ).scalar_one()
            failed = (
                await s.execute(
                    select(func.count())
                    .select_from(TaskResultRow)
                    .where(TaskResultRow.tenant_id == tenant_id)
                    .where(TaskResultRow.status == "failed")
                )
            ).scalar_one()
        return {
            "historical_results": int(total),
            "replay_candidates": int(failed),
            "next_action": "queue_failed_tasks_for_replay" if failed else "no_replay_needed",
        }


class ConsistencyTestStep(IdleBatchStep):
    """Triple-perturbation consistency check — STUB: only counts weak cards."""

    step_id = "consistency_test"
    stub = True

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import CapabilityCardRow

        async with session_scope(tenant_id=tenant_id) as s:
            cards = (
                await s.execute(
                    select(func.count())
                    .select_from(CapabilityCardRow)
                    .where(CapabilityCardRow.tenant_id == tenant_id)
                )
            ).scalar_one()
            weak_cards = (
                await s.execute(
                    select(func.count())
                    .select_from(CapabilityCardRow)
                    .where(CapabilityCardRow.tenant_id == tenant_id)
                    .where(CapabilityCardRow.overall_reliability < 0.65)
                )
            ).scalar_one()
        return {
            "capability_cards": int(cards),
            "consistency_candidates": int(weak_cards),
            "next_action": "run_consistency_holdout" if weak_cards else "no_consistency_action",
        }


class MethodologyDistillStep(IdleBatchStep):
    """情节记忆 → 语义方法论 蒸馏 (ADR-025).

    L2.9 实装: 扫 docs/dev_logs/*.md → 抽"关键决策" bullets → 与已有
    seeds/methodologies/*.yaml 去重 → 输出 novel candidates.
    """

    step_id = "methodology_distill"
    stub = False

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.engineering.methodology_distill import distill

        report = distill()
        return {
            "total_scanned": report.total_scanned,
            "novel_candidates": len(report.novel_candidates),
            "duplicates_skipped": report.duplicates_skipped,
            "candidate_titles": [c.title for c in report.novel_candidates[:10]],
            "sources_scanned": len(report.sources),
            "next_action": "review_novel_candidates"
            if report.novel_candidates
            else "no_distillation_action",
        }


class KnowledgeConflictStep(IdleBatchStep):
    """Resolve conflicting memories — STUB: only counts conflict events."""

    step_id = "knowledge_conflict"
    stub = True

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import EventRow

        async with session_scope(tenant_id=tenant_id) as s:
            conflicts = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.event_type.in_(["context.conflict", "knowledge.conflict"]))
                )
            ).scalar_one()
        return {
            "detected_conflicts": int(conflicts),
            "next_action": "resolve_conflicts" if conflicts else "no_conflict_action",
        }


class ABDecisionRollupStep(IdleBatchStep):
    """Collect AB-experiment results — STUB: only counts promotions/rollbacks."""

    step_id = "ab_decision_roll_up"
    stub = True

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import EventRow

        async with session_scope(tenant_id=tenant_id) as s:
            promotions = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.event_type == "promotion")
                )
            ).scalar_one()
            rollbacks = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.event_type == "rollback")
                )
            ).scalar_one()
        return {
            "promoted": int(promotions),
            "rolled_back": int(rollbacks),
            "next_action": "review_ab_guardrails"
            if promotions or rollbacks
            else "no_ab_rollup_action",
        }


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
    """Route-rule mining — STUB: only counts fallback events."""

    step_id = "route_rule_mining"
    stub = True

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from sqlalchemy import func, select

        from kun.core.db import session_scope
        from kun.core.orm import EventRow

        async with session_scope(tenant_id=tenant_id) as s:
            fallback_events = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.event_type == "llm.fallback.triggered")
                )
            ).scalar_one()
            route_events = (
                await s.execute(
                    select(func.count())
                    .select_from(EventRow)
                    .where(EventRow.tenant_id == tenant_id)
                    .where(EventRow.event_type == "llm.call.completed")
                )
            ).scalar_one()
        return {
            "route_events": int(route_events),
            "fallback_events": int(fallback_events),
            "new_patterns": int(fallback_events > 0),
            "next_action": "mine_fallback_route_patterns"
            if fallback_events
            else "no_route_rule_action",
        }


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
