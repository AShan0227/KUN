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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kun.core.anchor_expand import AnchorExpandIterator
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
    """A single step run during idle-batch."""

    step_id: str

    @abstractmethod
    async def run(self, tenant_id: str) -> dict[str, Any]: ...


# ============= Registry ============


_steps: dict[str, IdleBatchStep] = {}
_data_source: Any | None = None


def register_step(step: IdleBatchStep) -> None:
    _steps[step.step_id] = step


def list_steps() -> list[str]:
    return sorted(_steps)


def get_step(step_id: str) -> IdleBatchStep | None:
    return _steps.get(step_id)


def set_idle_batch_data_source(data_source: Any) -> None:
    """Inject a data source for idle-batch steps (tests / future DB adapter)."""

    global _data_source
    _data_source = data_source


def reset_idle_batch_data_source() -> None:
    global _data_source
    _data_source = None


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


async def run_all_anchor_then_expand(
    tenant_id: str,
    *,
    enabled: set[str] | None = None,
    max_rounds: int = 3,
) -> AsyncIterator[StepReport]:
    """按需运行 idle-batch step.

    老的 ``run_all`` 会一次性跑完所有启用 step. 新接口先跑最高优先级 step,
    调用方需要更多离线工作时再继续 expand.

    # TODO: wire by Claude in V2.2
    """
    names = _selected_step_names(enabled)
    if not names:
        return

    log.info("idle_batch.run_all_anchor.start", tenant_id=tenant_id, steps=names)

    async def anchor_fn() -> StepReport:
        return await _run_one_step(tenant_id, names[0])

    async def expand_fn(_anchor: StepReport, prior: list[StepReport]) -> StepReport | None:
        idx = len(prior)
        if idx >= len(names):
            return None
        return await _run_one_step(tenant_id, names[idx])

    with tenant_scope(TenantContext(tenant_id=tenant_id)):
        async for report in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield report


def _selected_step_names(enabled: set[str] | None) -> list[str]:
    names = [n for n in list_steps() if enabled is None or n in enabled]
    priority = {
        "health_report": 0,
        "knowledge_precipitation": 1,
        "knowledge_conflict": 2,
        "methodology_distill": 3,
        "route_rule_mining": 4,
        "ab_decision_roll_up": 5,
        "consistency_test": 6,
        "task_replay": 7,
    }
    return sorted(names, key=lambda name: (priority.get(name, 99), name))


async def _run_one_step(tenant_id: str, name: str) -> StepReport:
    step = _steps[name]
    started = datetime.now(UTC)
    try:
        summary = await step.run(tenant_id)
        return StepReport(
            step_id=name,
            started_at=started,
            finished_at=datetime.now(UTC),
            status="ok",
            summary=summary,
        )
    except Exception as e:
        log.exception("idle_batch.step_failed", step=name, error=str(e))
        return StepReport(
            step_id=name,
            started_at=started,
            finished_at=datetime.now(UTC),
            status="failed",
            summary={"error": str(e)},
        )


# ============= Built-in steps (placeholders) ============


class TaskReplayStep(IdleBatchStep):
    """Replay recent historical tasks with the current router + skills, compare outputs."""

    step_id = "task_replay"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        tasks = await _source_list("recent_tasks", tenant_id)
        replayed = len(tasks)
        treatment_wins = sum(
            1 for task in tasks if _float(task.get("new_score")) > _float(task.get("old_score"))
        )
        control_wins = sum(
            1 for task in tasks if _float(task.get("old_score")) > _float(task.get("new_score"))
        )
        return {
            "replayed": replayed,
            "treatment_wins": treatment_wins,
            "control_wins": control_wins,
            "win_rate": treatment_wins / replayed if replayed else 0.0,
        }


class ConsistencyTestStep(IdleBatchStep):
    """Triple-perturbation (temperature / rewording / model) consistency check."""

    step_id = "consistency_test"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        samples = await _source_list("consistency_samples", tenant_id)
        spreads = [_spread(sample.get("scores", [])) for sample in samples]
        unstable = [spread for spread in spreads if spread > 0.25]
        return {
            "samples": len(samples),
            "unstable": len(unstable),
            "avg_spread": sum(spreads) / len(spreads) if spreads else 0.0,
        }


class MethodologyDistillStep(IdleBatchStep):
    """情节记忆 → 语义方法论 蒸馏."""

    step_id = "methodology_distill"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        narratives = await _source_list("narratives", tenant_id)
        rules = sorted(
            {
                str(item.get("rule") or item.get("lesson") or "").strip()
                for item in narratives
                if str(item.get("rule") or item.get("lesson") or "").strip()
            },
        )
        return {
            "source_narratives": len(narratives),
            "new_rules": len(rules),
            "rules": rules[:10],
        }


class KnowledgeConflictStep(IdleBatchStep):
    """Resolve conflicting memories in the asset pool."""

    step_id = "knowledge_conflict"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        claims = await _source_list("memory_claims", tenant_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for claim in claims:
            grouped.setdefault(str(claim.get("key") or ""), []).append(claim)

        resolved: list[dict[str, Any]] = []
        for key, items in grouped.items():
            values = {str(item.get("value")) for item in items}
            if key and len(values) > 1:
                winner = max(items, key=lambda item: _float(item.get("confidence"), default=0.0))
                resolved.append(
                    {
                        "key": key,
                        "winner": winner.get("value"),
                        "confidence": _float(winner.get("confidence"), default=0.0),
                        "candidates": len(items),
                    }
                )
        return {"checked": len(claims), "resolved": len(resolved), "resolutions": resolved}


class ABDecisionRollupStep(IdleBatchStep):
    """Collect AB-experiment results, promote/demote based on guardrails."""

    step_id = "ab_decision_roll_up"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        experiments = await _source_list("experiments", tenant_id)
        promoted = 0
        rolled_back = 0
        observed = 0
        decisions: list[dict[str, Any]] = []
        for exp in experiments:
            if bool(exp.get("guardrail_breached")):
                decision = "rollback"
                rolled_back += 1
            elif _float(exp.get("treatment_score")) > _float(exp.get("control_score")):
                decision = "promote_shadow"
                promoted += 1
            else:
                decision = "observe"
                observed += 1
            decisions.append({"experiment_id": exp.get("experiment_id"), "decision": decision})
        return {
            "experiments": len(experiments),
            "promoted": promoted,
            "rolled_back": rolled_back,
            "observed": observed,
            "decisions": decisions,
        }


class HealthReportStep(IdleBatchStep):
    """Generate weekly / monthly health report → NUO dashboard."""

    step_id = "health_report"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        sourced = await _source_dict("health_snapshot", tenant_id)
        if sourced:
            return sourced

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
        logs = await _source_list("route_logs", tenant_id)
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in logs:
            key = (str(item.get("task_type") or "unknown"), str(item.get("model") or "unknown"))
            buckets.setdefault(key, []).append(item)

        patterns: list[dict[str, Any]] = []
        by_task: dict[str, list[dict[str, Any]]] = {}
        for (task_type, model), items in buckets.items():
            success_rate = sum(1 for item in items if bool(item.get("success"))) / len(items)
            avg_cost = sum(_float(item.get("cost_usd")) for item in items) / len(items)
            by_task.setdefault(task_type, []).append(
                {
                    "task_type": task_type,
                    "model": model,
                    "success_rate": success_rate,
                    "avg_cost_usd": avg_cost,
                    "samples": len(items),
                }
            )
        for task_type, candidates in by_task.items():
            best = max(candidates, key=lambda item: (item["success_rate"], -item["avg_cost_usd"]))
            if best["samples"] >= 2 and best["success_rate"] >= 0.8:
                patterns.append(
                    {"task_type": task_type, "recommended_model": best["model"], **best}
                )
        return {"logs": len(logs), "new_patterns": len(patterns), "patterns": patterns}


class KnowledgePrecipitationStep(IdleBatchStep):
    """V2.1 wire (§16.12): 跑 KnowledgePrecipitation hourly/daily/weekly 调度.

    每次跑 idle-batch, 按当前小时 / 日期判断要跑哪一档:
    - 每次跑都跑 hourly
    - 每天 0 点附近跑 daily
    - 每周一 0 点附近跑 weekly

    M3.3 wire: idle-batch 注册这一步, KnowledgePrecipitation 单例在 install_runtime 创建.
    """

    step_id = "knowledge_precipitation"

    def __init__(self, kp_provider: Callable[[], Any] | None = None) -> None:
        self._kp_provider = kp_provider

    async def run(self, tenant_id: str) -> dict[str, Any]:
        if self._kp_provider is None:
            log.info("knowledge_precipitation.no_provider", tenant_id=tenant_id)
            return {"hourly": 0, "daily": 0, "weekly": 0, "note": "no_provider"}
        kp = self._kp_provider()
        if kp is None:
            return {"hourly": 0, "daily": 0, "weekly": 0, "note": "kp_none"}

        now = datetime.now(UTC)
        results: dict[str, Any] = {}
        hourly_updates = await kp.run_scheduled("hourly")
        results["hourly"] = len(hourly_updates)
        if now.hour == 0:
            daily_updates = await kp.run_scheduled("daily")
            results["daily"] = len(daily_updates)
        else:
            results["daily"] = 0
        if now.weekday() == 0 and now.hour == 0:
            weekly_updates = await kp.run_scheduled("weekly")
            results["weekly"] = len(weekly_updates)
        else:
            results["weekly"] = 0
        log.info(
            "knowledge_precipitation.cycle_done",
            tenant_id=tenant_id,
            hourly=results["hourly"],
            daily=results["daily"],
            weekly=results["weekly"],
        )
        return results


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


async def _source_list(method_name: str, tenant_id: str) -> list[dict[str, Any]]:
    result = await _call_data_source(method_name, tenant_id)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


async def _source_dict(method_name: str, tenant_id: str) -> dict[str, Any]:
    result = await _call_data_source(method_name, tenant_id)
    return result if isinstance(result, dict) else {}


async def _call_data_source(method_name: str, tenant_id: str) -> Any:
    if _data_source is None:
        return None
    method = getattr(_data_source, method_name, None)
    if method is None:
        return None
    result = method(tenant_id)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _spread(values: Any) -> float:
    if not isinstance(values, list) or not values:
        return 0.0
    nums = [_float(value) for value in values]
    return max(nums) - min(nums)
