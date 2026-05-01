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
import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.logging import get_logger
from kun.core.tenancy import TenantContext, tenant_scope
from kun.engineering.external_scan import fetch_github_repo_external_skill_metadata

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


class IdleBatchDbDataSource:
    """Small production data source for idle-batch learning inputs.

    This is intentionally read-only.  It turns durable task/result/runtime rows
    into the same compact dictionaries tests can inject, so Qi can learn from
    real completed and failed work without bespoke plumbing.
    """

    def __init__(self, *, history_limit: int = 30, signal_limit: int = 30) -> None:
        self.history_limit = max(1, history_limit)
        self.signal_limit = max(1, signal_limit)

    async def qi_problem_signals(self, tenant_id: str) -> list[dict[str, Any]]:
        from kun.qi.problem_queue import get_configured_qi_problem_queue

        try:
            queue = get_configured_qi_problem_queue()
            signals = await _queue_list(queue, tenant_id=tenant_id, limit=self.signal_limit)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for signal in signals:
            model_dump = getattr(signal, "model_dump", None)
            if callable(model_dump):
                out.append(model_dump(mode="json"))
            elif isinstance(signal, dict):
                out.append(signal)
        return out

    async def completed_task_history(self, tenant_id: str) -> list[dict[str, Any]]:
        from kun.core.db import session_scope
        from kun.core.orm import RuntimeStateRow, TaskResultRow, TaskRow

        try:
            async with session_scope(tenant_id=tenant_id) as session:
                result_rows = (
                    await session.execute(
                        select(TaskResultRow, TaskRow, RuntimeStateRow)
                        .join(
                            TaskRow,
                            (TaskRow.task_id == TaskResultRow.task_id)
                            & (TaskRow.tenant_id == TaskResultRow.tenant_id),
                        )
                        .outerjoin(
                            RuntimeStateRow,
                            (RuntimeStateRow.task_ref == TaskResultRow.task_id)
                            & (RuntimeStateRow.tenant_id == TaskResultRow.tenant_id),
                        )
                        .where(
                            TaskResultRow.tenant_id == tenant_id,
                            TaskResultRow.status.in_(("done", "failed", "cancelled")),
                        )
                        .order_by(TaskResultRow.updated_at.desc())
                        .limit(self.history_limit)
                    )
                ).all()
                histories = [
                    _task_history_from_db_rows(result, task, runtime)
                    for result, task, runtime in result_rows
                ]
                remaining = self.history_limit - len(histories)
                if remaining > 0:
                    runtime_rows = (
                        await session.execute(
                            select(RuntimeStateRow, TaskRow)
                            .join(
                                TaskRow,
                                (TaskRow.task_id == RuntimeStateRow.task_ref)
                                & (TaskRow.tenant_id == RuntimeStateRow.tenant_id),
                            )
                            .outerjoin(
                                TaskResultRow,
                                (TaskResultRow.task_id == RuntimeStateRow.task_ref)
                                & (TaskResultRow.tenant_id == RuntimeStateRow.tenant_id),
                            )
                            .where(
                                RuntimeStateRow.tenant_id == tenant_id,
                                RuntimeStateRow.status.in_(("failed", "cancelled")),
                                TaskResultRow.task_id.is_(None),
                            )
                            .order_by(RuntimeStateRow.last_updated.desc())
                            .limit(remaining)
                        )
                    ).all()
                    histories.extend(
                        _task_history_from_db_rows(None, task, runtime)
                        for runtime, task in runtime_rows
                    )
        except Exception:
            log.debug("idle_batch.db_completed_task_history_failed", exc_info=True)
            return []
        return histories


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

    This is the production-friendly path for NUO/Qi maintenance: run the
    anchor first, expand only while the caller's round budget allows it.
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


async def run_anchor_then_expand_once(
    tenant_id: str,
    *,
    enabled: set[str] | None = None,
    max_rounds: int = 3,
) -> list[StepReport]:
    """Collect one anchor-expand idle-batch pass into a plain list."""

    reports: list[StepReport] = []
    async for report in run_all_anchor_then_expand(
        tenant_id,
        enabled=enabled,
        max_rounds=max_rounds,
    ):
        reports.append(report)
    return reports


def _selected_step_names(enabled: set[str] | None) -> list[str]:
    names = [n for n in list_steps() if enabled is None or n in enabled]
    priority = {
        "health_report": 0,
        "world_handler_auto_quarantine": 1,
        "coordination_remediation": 1,
        "compiler_recompile": 2,
        "compiler_intake_review": 2,
        "external_skill_scout_plan": 2,
        "qi_idle_replay": 2,
        "qi_strategy_pack_review": 2,
        "qi_strategy_pack_rollout_plan": 2,
        "external_skill_candidate_review": 2,
        "knowledge_precipitation": 2,
        "incident_lessons": 2,
        "knowledge_conflict": 3,
        "methodology_distill": 4,
        "route_rule_mining": 5,
        "task_boundary_eval": 6,
        "ab_decision_roll_up": 7,
        "consistency_test": 8,
        "task_replay": 9,
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
        asset_ids = await _persist_methodology_rules(tenant_id=tenant_id, rules=rules)
        return {
            "source_narratives": len(narratives),
            "new_rules": len(rules),
            "rules": rules[:10],
            "asset_ids": asset_ids[:10],
        }


class ContextGovernanceRuleDistillStep(IdleBatchStep):
    """Repeated NUO context findings → review-only methodology drafts."""

    step_id = "context_governance_rule_distill"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.context.governance_distill import distill_context_governance_rules

        report = await distill_context_governance_rules(
            tenant_id=tenant_id,
            dry_run=False,
        )
        return report.model_dump(mode="json")


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
    """Generate periodic NUO system health report."""

    step_id = "health_report"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        sourced = await _source_dict("health_snapshot", tenant_id)
        if sourced:
            return sourced

        from kun.core.db import session_scope
        from kun.core.events import emit
        from kun.core.state_ledger import get_state_ledger
        from kun.datamodel.events import Event
        from kun.engineering.nuo_system_health import collect_system_health_report
        from kun.qi.problem_queue import (
            persist_problem_signals,
            signals_from_system_health_findings,
        )

        report = await collect_system_health_report(tenant_id=tenant_id)
        get_state_ledger().record_system_health_report(report)
        qi_problem_signals = signals_from_system_health_findings(tenant_id, report.findings)
        persisted_qi_problem_signals = await persist_problem_signals(qi_problem_signals)
        summary = {
            "total_tasks": report.total_tasks,
            "runtime_by_status": report.runtime_by_status,
            "events_outbox_lag": report.outbox_lag,
            "pending_approvals": report.pending_approvals,
            "stale_runtime_count": report.stale_runtime_count,
            "active_resource_conflicts": report.active_resource_conflicts,
            "worst_severity": report.worst_severity,
            "secret_audit_summary": report.secret_audit_summary,
            "world_handler_summary": report.world_handler_summary,
            "compiler_governance_summary": report.compiler_governance_summary,
            "context_maintenance_summary": report.context_maintenance_summary,
            "skill_health_summary": report.skill_health_summary,
            "qi_strategy_draft_summary": report.qi_strategy_draft_summary,
            "multi_lane_scheduler_summary": report.multi_lane_scheduler_summary,
            "multi_lane_scheduler_limits": report.multi_lane_scheduler_limits,
            "production_risk_summary": report.production_risk_summary,
            "findings": len(report.findings),
            "governance_recommendations": len(report.governance_recommendations),
            "qi_problem_signals": len(qi_problem_signals),
            "persisted_qi_problem_signals": persisted_qi_problem_signals,
            "top_findings": [
                {
                    "finding_id": finding.finding_id,
                    "severity": finding.severity,
                    "subsystem": finding.subsystem,
                    "title": finding.title,
                    "suggested_action": finding.suggested_action,
                }
                for finding in report.findings[:10]
            ],
            "top_governance_recommendations": [
                {
                    "recommendation_id": item.recommendation_id,
                    "finding_id": item.finding_id,
                    "subsystem": item.subsystem,
                    "risk_level": item.risk_level,
                    "default_dry_run": item.default_dry_run,
                    "can_apply": item.can_apply,
                    "requires_human_approval": item.requires_human_approval,
                    "apply_hint": item.apply_hint,
                }
                for item in report.governance_recommendations[:10]
            ],
        }
        async with session_scope(tenant_id=tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="nuo.health_report.generated",
                    payload=summary,
                ),
            )

        return summary


class WorldHandlerAutoQuarantineStep(IdleBatchStep):
    """Ask NUO to review unsafe WorldGateway handlers during idle time."""

    step_id = "world_handler_auto_quarantine"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.world.handler_auto_control import run_world_handler_auto_quarantine

        # Default to dry-run. Real quarantine can block real-world actions, so
        # overnight NUO initially reports recommendations instead of silently
        # changing controls.
        report = await run_world_handler_auto_quarantine(tenant_id=tenant_id, dry_run=True)
        return report.model_dump(mode="json")


class CoordinationRemediationStep(IdleBatchStep):
    """Let NUO consume coordination findings safely.

    Default is dry-run.  Set ``KUN_COORDINATION_REMEDIATION_MODE=auto_low_risk``
    to let NUO trigger only low-risk approved actions that already passed the
    normal approval gate.  High-risk / real external actions stay manual.
    """

    step_id = "coordination_remediation"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.engineering.coordination_remediation import run_coordination_remediation

        report = await run_coordination_remediation(tenant_id=tenant_id)
        return report.model_dump(mode="json")


class QiIdleReplayStep(IdleBatchStep):
    """Let Qi review real problems and completed tasks during idle time.

    The output is intentionally review-only.  Candidates are persisted back as
    Qi problem signals so a stronger judge / lab pipeline can inspect them
    later, but this step never promotes a route, skill, or protocol by itself.
    """

    step_id = "qi_idle_replay"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.qi.idle_replay import (
            ReplayEvaluationBudget,
            configured_local_replay_model_evaluator_from_env,
            configured_strong_replay_model_evaluator_from_env,
            evaluate_idle_replay_pool,
            generate_idle_replay_candidates,
        )
        from kun.qi.lab_replay import (
            configured_qi_lab_replay_budget_from_env,
            qi_lab_replay_enabled_from_env,
            run_qi_lab_replay_pool,
        )
        from kun.qi.problem_queue import (
            QiProblemSignal,
            get_configured_qi_problem_queue,
            mark_problem_signals_consumed,
            persist_problem_signals,
        )
        from kun.qi.replay_tree_search import (
            configured_qi_replay_tree_search_budget_from_env,
            qi_replay_tree_search_enabled_from_env,
            run_qi_replay_tree_search_pool,
        )
        from kun.qi.strategy_review_package import build_qi_strategy_review_packages

        raw_signals = await _source_list("qi_problem_signals", tenant_id)
        signals: list[QiProblemSignal] = []
        for raw in raw_signals:
            try:
                signal = QiProblemSignal.model_validate(raw)
            except Exception:
                log.debug("qi_idle_replay.invalid_signal_from_source", exc_info=True)
                continue
            if signal.source != "qi.idle_replay.candidate":
                signals.append(signal)

        if not signals:
            try:
                queue = get_configured_qi_problem_queue()
                listed = await _queue_list(queue, tenant_id=tenant_id, limit=20)
                signals.extend(
                    signal for signal in listed if signal.source != "qi.idle_replay.candidate"
                )
            except Exception:
                log.debug("qi_idle_replay.queue_list_failed", exc_info=True)

        histories = await _source_list("completed_task_history", tenant_id)
        candidates = generate_idle_replay_candidates([*signals, *histories])
        review_signals = [
            candidate.to_problem_signal(tenant_id=tenant_id) for candidate in candidates
        ]
        persisted = await persist_problem_signals(review_signals)
        drafts = [candidate.to_strategy_pack_draft() for candidate in candidates]
        local_model_evaluator = configured_local_replay_model_evaluator_from_env()
        evaluator_kind: Literal["heuristic", "local_model"] = (
            "local_model" if local_model_evaluator is not None else "heuristic"
        )
        evaluations = await evaluate_idle_replay_pool(
            drafts,
            budget=ReplayEvaluationBudget(max_items=5, max_cost_usd=0.02, max_concurrency=2),
            evaluator_kind=evaluator_kind,
            local_model_evaluator=local_model_evaluator,
        )
        qi_budget_usage: list[dict[str, Any]] = [
            _charge_qi_budget_for_evaluations(
                tenant_id,
                evaluations.records,
                reason="qi_idle_replay.base_evaluation",
            )
        ]
        strong_model_evaluator = configured_strong_replay_model_evaluator_from_env()
        strong_review_items = [draft for draft in drafts if draft.requires_strong_review]
        strong_review_pool: dict[str, Any] = {
            "enabled": strong_model_evaluator is not None,
            "evaluated": 0,
            "production_action": False,
        }
        if strong_model_evaluator is not None and strong_review_items:
            strong_review_max_cost = _float(
                os.getenv("KUN_QI_STRONG_REVIEW_MAX_COST_USD"),
                default=0.12,
            )
            qi_budget_remaining = _qi_budget_remaining(tenant_id)
            strong_reviews = await evaluate_idle_replay_pool(
                strong_review_items,
                budget=ReplayEvaluationBudget(
                    max_items=_int_env("KUN_QI_STRONG_REVIEW_MAX_ITEMS", 2),
                    max_cost_usd=min(strong_review_max_cost, qi_budget_remaining),
                    max_concurrency=1,
                ),
                evaluator_kind="strong_model",
                strong_model_evaluator=strong_model_evaluator,
            )
            strong_review_pool = strong_reviews.model_dump(mode="json")
            strong_review_pool["enabled"] = True
            strong_review_pool["qi_daily_budget_remaining_before_usd"] = round(
                qi_budget_remaining,
                6,
            )
            qi_budget_usage.append(
                _charge_qi_budget_for_evaluations(
                    tenant_id,
                    strong_reviews.records,
                    reason="qi_idle_replay.strong_review",
                )
            )
        lab_replay_pool = await run_qi_lab_replay_pool(
            drafts,
            histories,
            enabled=qi_lab_replay_enabled_from_env(),
            budget=configured_qi_lab_replay_budget_from_env(),
        )
        tree_search_pool = await run_qi_replay_tree_search_pool(
            drafts,
            enabled=qi_replay_tree_search_enabled_from_env(),
            budget=configured_qi_replay_tree_search_budget_from_env(),
        )
        strong_review_records = list(strong_review_pool.get("records", []))
        strategy_review_packages = build_qi_strategy_review_packages(
            candidates=candidates,
            drafts=drafts,
            evaluation_records=evaluations.records,
            strong_review_records=strong_review_records,
            lab_replay_records=lab_replay_pool.records,
            tree_search_records=tree_search_pool.records,
        )
        draft_asset_ids = await _persist_strategy_pack_drafts(
            tenant_id=tenant_id,
            drafts=drafts,
            evaluation_records=[
                *evaluations.records,
                *strong_review_records,
            ],
            lab_replay_records=lab_replay_pool.records,
            tree_search_records=tree_search_pool.records,
            strategy_review_packages=strategy_review_packages,
        )
        source_signal_ids = {signal.signal_id for signal in signals}
        consumed_problem_signals = await mark_problem_signals_consumed(
            tenant_id=tenant_id,
            signal_ids=[
                candidate.source_signal_id
                for candidate in candidates
                if candidate.source_signal_id in source_signal_ids
            ],
        )
        return {
            "signals": len(signals),
            "completed_task_histories": len(histories),
            "candidates": len(candidates),
            "strategy_pack_drafts": [
                draft.model_dump(mode="json")
                for draft in sorted(
                    drafts,
                    key=lambda item: (
                        not item.requires_strong_review,
                        item.status,
                        item.candidate_id,
                    ),
                )[:5]
            ],
            "requires_strong_review": sum(1 for item in candidates if item.requires_strong_review),
            "evaluation_pool": evaluations.model_dump(mode="json"),
            "evaluation_engine": evaluator_kind,
            "strong_review_pool": strong_review_pool,
            "strong_review_engine": "strong_model"
            if strong_model_evaluator is not None
            else "disabled",
            "qi_budget_usage": qi_budget_usage,
            "lab_replay_pool": lab_replay_pool.model_dump(mode="json"),
            "tree_search_pool": tree_search_pool.model_dump(mode="json"),
            "strategy_review_package_summary": _strategy_review_package_summary(
                strategy_review_packages
            ),
            "strategy_review_packages": [
                package.model_dump(mode="json")
                for package in sorted(
                    strategy_review_packages,
                    key=lambda item: (
                        item.status != "needs_strong_review",
                        item.status,
                        -item.best_local_score,
                        item.draft_id,
                    ),
                )[:5]
            ],
            "persisted_review_signals": persisted,
            "consumed_problem_signals": consumed_problem_signals,
            "persisted_strategy_pack_draft_assets": len(draft_asset_ids),
            "strategy_pack_draft_asset_ids": draft_asset_ids[:10],
            "engine": "heuristic_local",
            "production_action": False,
            "top_candidates": [
                candidate.to_lab_recipe_draft()
                for candidate in sorted(
                    candidates,
                    key=lambda item: (
                        not item.requires_strong_review,
                        item.risk,
                        item.candidate_id,
                    ),
                )[:5]
            ],
        }


class QiStrategyPackReviewStep(IdleBatchStep):
    """Classify Qi StrategyPack drafts by evidence, without promotion.

    Qi is allowed to search and generate candidates, but production routing
    should only see a clear review state: missing evidence, blocked, or ready
    for a human/strong-review approval path.
    """

    step_id = "qi_strategy_pack_review"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.qi.strategy_pack_review import review_strategy_pack_draft_assets

        report = await review_strategy_pack_draft_assets(
            tenant_id=tenant_id,
            dry_run=False,
        )
        return report.model_dump(mode="json")


class QiStrategyPackRolloutPlanStep(IdleBatchStep):
    """Create guarded rollout plans for reviewed Qi StrategyPack drafts."""

    step_id = "qi_strategy_pack_rollout_plan"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.qi.strategy_pack_rollout import (
            create_strategy_pack_shadow_experiment,
            plan_strategy_pack_rollouts,
        )

        report = await plan_strategy_pack_rollouts(
            tenant_id=tenant_id,
            dry_run=False,
        )
        payload = report.model_dump(mode="json")
        if os.getenv("KUN_QI_ROLLOUT_EXPERIMENT_CREATE_ENABLED", "0") == "1":
            experiment_reports = []
            for plan in report.plans:
                if plan.status != "shadow_plan":
                    continue
                experiment_report = await create_strategy_pack_shadow_experiment(
                    tenant_id=tenant_id,
                    draft_id=plan.draft_id,
                    dry_run=os.getenv("KUN_QI_ROLLOUT_EXPERIMENT_CREATE_DRY_RUN", "1") != "0",
                )
                experiment_reports.append(experiment_report.model_dump(mode="json"))
            payload["experiment_bridge_reports"] = experiment_reports
        return payload


class CompilerSyncSourcesStep(IdleBatchStep):
    """Run explicitly configured compiler sync sources during idle time.

    This is the scheduler bridge for the V5 compiler. It is opt-in only:
    without KUN_COMPILER_SYNC_SOURCE_FILES, the step reports skipped and does
    not read local files or fetch URLs.
    """

    step_id = "compiler_sync_sources"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        import os

        raw_sources = os.getenv("KUN_COMPILER_SYNC_SOURCE_FILES", "")
        source_files = [item.strip() for item in raw_sources.split(",") if item.strip()]
        if not source_files:
            return {
                "skipped": True,
                "reason": "KUN_COMPILER_SYNC_SOURCE_FILES not configured",
                "sources": 0,
                "synced": 0,
                "errors": 0,
            }

        from kun.compiler import CompilerSyncRunner

        config_root = os.getenv("KUN_COMPILER_SYNC_CONFIG_ROOT") or None
        runner = CompilerSyncRunner()
        reports: list[dict[str, Any]] = []
        synced = 0
        disabled = 0
        errors = 0
        for source_file in source_files:
            report = await runner.sync_source_file(
                source_file,
                config_root=config_root,
                tenant_override=tenant_id,
            )
            reports.append(report.model_dump(mode="json"))
            if report.status == "synced":
                synced += 1
            elif report.status == "skipped_disabled":
                disabled += 1
            else:
                errors += 1
        return {
            "skipped": False,
            "sources": len(source_files),
            "synced": synced,
            "disabled": disabled,
            "errors": errors,
            "reports": reports,
        }


class CompilerRecompileStep(IdleBatchStep):
    """Re-run compiler for assets NUO marked as low quality.

    Default is dry-run.  Real mutation requires
    ``KUN_COMPILER_RECOMPILE_APPLY=1``.  Even then, the recompiler stores a new
    asset and marks the original, instead of overwriting history.
    """

    step_id = "compiler_recompile"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.compiler import CompilerRecompiler

        raw_roots = os.getenv("KUN_COMPILER_RECOMPILE_ALLOWED_ROOTS", "")
        allowed_roots = [item.strip() for item in raw_roots.split(",") if item.strip()]
        report = await CompilerRecompiler().recompile_candidates(
            tenant_id=tenant_id,
            allowed_roots=allowed_roots,
            dry_run=os.getenv("KUN_COMPILER_RECOMPILE_APPLY", "0") != "1",
            max_assets=int(os.getenv("KUN_COMPILER_RECOMPILE_MAX_ASSETS", "500")),
            allow_inline_summary=(
                os.getenv("KUN_COMPILER_RECOMPILE_ALLOW_INLINE_SUMMARY", "0") == "1"
            ),
        )
        payload = report.model_dump(mode="json")
        payload["mutation_enabled"] = not report.dry_run
        payload["production_action"] = False
        return payload


class CompilerIntakeReviewStep(IdleBatchStep):
    """Review explicit compiler intake requests and queue unsafe ones.

    This is the consumer for CompilerReviewPackage.  It deliberately does not
    store compiled assets; it only audits explicit rows and sends risky/held
    materials into Qi/NUO review so compiler issues do not remain invisible.
    """

    step_id = "compiler_intake_review"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.compiler import (
            CompilerIntakeRequest,
            build_compiler_review_package,
            enqueue_compiler_review_packages,
        )

        rows = await _source_list("compiler_intake_requests", tenant_id)
        if not rows:
            return {
                "skipped": True,
                "reason": "no compiler intake requests configured",
                "requests": 0,
                "review_packages": 0,
                "queued_review_signals": 0,
                "compiled_to_asset": 0,
                "production_action": False,
            }

        packages = []
        invalid = 0
        for raw in rows[:20]:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            payload = dict(raw)
            payload["tenant_id"] = tenant_id
            try:
                request = CompilerIntakeRequest.model_validate(payload)
                packages.append(await build_compiler_review_package(request))
            except Exception:
                invalid += 1
                log.debug("compiler_intake_review.invalid_request", exc_info=True)

        queued_packages = [
            package
            for package in packages
            if package.needs_human_review
            or package.needs_recompile
            or package.decision != "compiled_to_asset"
        ]
        queued = await enqueue_compiler_review_packages(
            tenant_id=tenant_id,
            packages=queued_packages,
        )
        return {
            "skipped": False,
            "requests": len(rows),
            "invalid_requests": invalid,
            "review_packages": len(packages),
            "queued_review_signals": queued,
            "compiled_to_asset": sum(
                1 for package in packages if package.decision == "compiled_to_asset"
            ),
            "held_or_blocked": len(queued_packages),
            "top_packages": [
                package.as_review_ticket()
                for package in sorted(
                    packages,
                    key=lambda item: (
                        item.decision == "compiled_to_asset",
                        item.risk_level,
                        item.source.uri,
                    ),
                )[:5]
            ],
            "production_action": False,
        }


class ExternalEmergentScanStep(IdleBatchStep):
    """Feed explicit external strategy signals into the EmergentSolution library.

    This is intentionally not a crawler.  It consumes either an injected idle
    data source or opt-in JSON files and then uses ExternalInfoScanner's review
    and budget logic.  Real internet fetchers can be added later without
    changing the idle-batch control surface.
    """

    step_id = "external_emergent_scan"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.core.emergent_solution import get_library
        from kun.engineering.external_scan import (
            ExternalInfoScanner,
            configured_external_scan_reviewer_from_env,
        )

        rows = await _external_scan_rows(tenant_id)
        if not rows:
            return {
                "skipped": True,
                "reason": "no external scan rows configured",
                "scanned_task_types": [],
                "sources_queried": 0,
                "candidates_added": 0,
                "candidates_rejected": 0,
            }

        fetchers = _external_scan_fetchers(rows)
        task_types = _external_scan_task_types(rows)
        if not fetchers or not task_types:
            return {
                "skipped": True,
                "reason": "no valid source_kind/task_type in external scan rows",
                "scanned_task_types": [],
                "sources_queried": 0,
                "candidates_added": 0,
                "candidates_rejected": 0,
            }

        reviewer = configured_external_scan_reviewer_from_env()
        scanner = ExternalInfoScanner(
            get_library(),
            fetchers=fetchers,
            llm_reviewer=reviewer,
            user_top_task_types_lookup=lambda _tenant_id: task_types,
            user_telemetry_enabled=lambda _tenant_id: True,
            default_daily_limit=_int_env("KUN_EXTERNAL_SCAN_DAILY_LIMIT", 25),
        )
        result = await scanner.scan_for_user(tenant_id)
        return {
            "skipped": False,
            "input_rows": len(rows),
            "strong_review_enabled": reviewer is not None,
            **result.__dict__,
        }


class ExternalSkillCandidateReviewStep(IdleBatchStep):
    """Normalize external skill metadata and enqueue review-only Qi signals.

    This step is intentionally explicit-input first. It does not install skills
    or modify production skill registries. Explicit data source rows,
    KUN_EXTERNAL_SKILL_SOURCE_FILES, or opt-in KUN_EXTERNAL_SKILL_GITHUB_REPOS
    are treated as candidate evidence for Qi / human security review.
    """

    step_id = "external_skill_candidate_review"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.engineering.external_scan import scan_external_skill_candidates
        from kun.qi.external_skill_review import (
            build_external_skill_candidate_source_plan,
            enqueue_external_skill_candidate_source_plans,
            enqueue_external_skill_review_packages,
            review_external_skill_candidates,
        )
        from kun.qi.problem_queue import persist_problem_signals

        rows = await _external_skill_rows(tenant_id)
        if not rows:
            return {
                "skipped": True,
                "reason": "no external skill metadata configured",
                "input_rows": 0,
                "candidates": 0,
                "persisted_review_signals": 0,
                "production_action": False,
                "auto_install_allowed": False,
                "task_needs": 0,
                "task_fit_review_packages": 0,
                "persisted_task_fit_review_signals": 0,
                "source_plans": 0,
                "persisted_source_plan_signals": 0,
            }

        scan_result = scan_external_skill_candidates(rows)
        candidates = scan_result.candidates
        review_signals = [
            candidate.to_review_signal(tenant_id=tenant_id) for candidate in candidates
        ]
        persisted = await persist_problem_signals(review_signals)
        task_needs = await _external_skill_task_needs(tenant_id)
        packages: list[Any] = []
        for task_need in task_needs[:5]:
            packages.extend(
                package
                for package in review_external_skill_candidates(
                    task_need=task_need,
                    candidates=cast(list[Any], candidates),
                )[:3]
                if package.worth_review or package.status == "blocked"
            )
        persisted_packages = await enqueue_external_skill_review_packages(
            tenant_id=tenant_id,
            packages=packages,
        )
        source_plans = [
            build_external_skill_candidate_source_plan(
                task_need,
                source_registry=cast(list[Any], rows),
                candidates=cast(list[Any], candidates),
            )
            for task_need in task_needs[:5]
        ]
        persisted_source_plans = await enqueue_external_skill_candidate_source_plans(
            tenant_id=tenant_id,
            plans=source_plans,
        )
        summary = scan_result.model_dump()

        return {
            "skipped": False,
            "input_rows": len(rows),
            "candidates": summary["candidates"],
            "persisted_review_signals": persisted,
            "task_needs": len(task_needs),
            "task_fit_review_packages": len(packages),
            "persisted_task_fit_review_signals": persisted_packages,
            "source_plans": len(source_plans),
            "persisted_source_plan_signals": persisted_source_plans,
            "risk_counts": summary["risk_counts"],
            "sandbox_suitable": summary["sandbox_suitable"],
            "production_action": False,
            "auto_install_allowed": False,
            "promotion_allowed": False,
            "top_candidates": summary["top_candidates"],
            "top_task_fit_packages": [
                package.model_dump(mode="json")
                for package in sorted(
                    packages,
                    key=lambda item: (
                        item.status == "blocked",
                        not item.worth_review,
                        -item.confidence,
                        item.candidate_name,
                    ),
                )[:5]
            ],
            "top_source_plans": [
                plan.model_dump(mode="json")
                for plan in sorted(
                    source_plans,
                    key=lambda item: (
                        not any(review.worth_review for review in item.source_reviews),
                        not any(review.worth_review for review in item.candidate_reviews),
                        item.task_demand,
                        item.plan_id,
                    ),
                )[:3]
            ],
        }


class ExternalSkillScoutPlanStep(IdleBatchStep):
    """Build review-only external-skill scout plans from real task needs."""

    step_id = "external_skill_scout_plan"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.qi.external_skill_review import (
            build_external_skill_scout_plan,
            enqueue_external_skill_scout_plans,
        )

        task_needs = await _external_skill_task_needs(tenant_id)
        if not task_needs:
            return {
                "skipped": True,
                "reason": "no task needs available for external skill scout planning",
                "task_needs": 0,
                "plans": 0,
                "persisted_scout_signals": 0,
                "production_action": False,
                "auto_fetch_allowed": False,
                "auto_install_allowed": False,
            }

        plans = [build_external_skill_scout_plan(task_need) for task_need in task_needs[:10]]
        persisted = await enqueue_external_skill_scout_plans(
            tenant_id=tenant_id,
            plans=plans,
        )
        return {
            "skipped": False,
            "task_needs": len(task_needs),
            "plans": len(plans),
            "persisted_scout_signals": persisted,
            "production_action": False,
            "auto_fetch_allowed": False,
            "auto_install_allowed": False,
            "top_plans": [
                plan.model_dump(mode="json")
                for plan in sorted(
                    plans,
                    key=lambda item: (
                        item.task_demand == "unknown",
                        item.task_demand,
                        item.task_type,
                    ),
                )[:5]
            ],
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


class TaskBoundaryEvalStep(IdleBatchStep):
    """Weekly OffTopicEval-compatible benchmark for TaskBoundaryGuard."""

    step_id = "task_boundary_eval"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        from kun.security.task_boundary_benchmark import (
            BoundaryBenchmarkCase,
            load_default_dataset,
            run_benchmark,
        )
        from kun.security.task_boundary_guard import TaskBoundaryGuard

        raw_cases = await _source_list("task_boundary_cases", tenant_id)
        if raw_cases:
            cases = [BoundaryBenchmarkCase.model_validate(item) for item in raw_cases]
            dataset_name = "custom"
        else:
            bundle = load_default_dataset()
            cases = bundle.cases
            dataset_name = bundle.name

        report = await run_benchmark(
            TaskBoundaryGuard(),
            cases,
            dataset_name=dataset_name,
        )
        return report.model_dump(exclude={"results"})


class IncidentLessonDistillStep(IdleBatchStep):
    """Distill IncidentResponse history into lessons for NUO/watchtower."""

    step_id = "incident_lessons"

    def __init__(self, incident_provider: Callable[[], Any] | None = None) -> None:
        self._incident_provider = incident_provider

    async def run(self, tenant_id: str) -> dict[str, Any]:
        if self._incident_provider is None:
            return {"incidents": 0, "lessons": [], "note": "no_provider"}
        engine = self._incident_provider()
        if engine is None:
            return {"incidents": 0, "lessons": [], "note": "engine_none"}

        history = [
            (event, actions)
            for event, actions in engine.get_history()
            if event.affected_tenant_id in (None, tenant_id)
        ]
        lessons: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], int] = {}
        for event, actions in history:
            key = (event.category, event.severity)
            grouped[key] = grouped.get(key, 0) + 1
            failed_actions = [action.action_kind for action in actions if not action.success]
            if failed_actions:
                lessons.append(
                    {
                        "incident_id": event.incident_id,
                        "lesson_kind": "response_gap",
                        "category": event.category,
                        "severity": event.severity,
                        "failed_actions": failed_actions,
                    }
                )

        for (category, severity), count in sorted(grouped.items()):
            if count >= 2:
                lessons.append(
                    {
                        "lesson_kind": "repeat_pattern",
                        "category": category,
                        "severity": severity,
                        "count": count,
                    }
                )
        return {"incidents": len(history), "lessons": lessons[:20]}


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


class PheromoneDecayStep(IdleBatchStep):
    """V2.3 Wire 43: Pheromone 每日衰减 (蚁群遗忘机制).

    没人走的边 pheromone × 0.95 daily → ~30 天近零. 让 skill chain 自然衰退,
    防陈旧路径堆积. KUN_PHEROMONE_DECAY_ENABLED=0 关闭.
    """

    step_id = "pheromone_decay"

    async def run(self, tenant_id: str) -> dict[str, Any]:
        import os

        if os.getenv("KUN_PHEROMONE_DECAY_ENABLED", "1") != "1":
            return {"skipped": True, "reason": "KUN_PHEROMONE_DECAY_ENABLED=0"}
        try:
            from kun.qi.pheromone import PHEROMONE_DECAY_RATE, get_pheromone_storage

            storage = get_pheromone_storage()
            decay_rate = float(os.getenv("KUN_PHEROMONE_DECAY_RATE", str(PHEROMONE_DECAY_RATE)))
            affected = await storage.decay_all(decay_rate=decay_rate, tenant_id=tenant_id)
            try:
                from kun.core.metrics import pheromone_decay_step_total

                pheromone_decay_step_total.labels(tenant_id=tenant_id, outcome="ok").inc()
            except Exception:
                pass
            return {"affected": int(affected), "decay_rate": decay_rate}
        except Exception as e:
            log.exception("pheromone_decay_failed", error=str(e))
            try:
                from kun.core.metrics import pheromone_decay_step_total

                pheromone_decay_step_total.labels(tenant_id=tenant_id, outcome="error").inc()
            except Exception:
                pass
            return {"affected": 0, "error": str(e)}


def register_default_steps() -> None:
    for step in [
        TaskReplayStep(),
        ConsistencyTestStep(),
        MethodologyDistillStep(),
        ContextGovernanceRuleDistillStep(),
        KnowledgeConflictStep(),
        ABDecisionRollupStep(),
        HealthReportStep(),
        WorldHandlerAutoQuarantineStep(),
        CoordinationRemediationStep(),
        QiIdleReplayStep(),
        QiStrategyPackReviewStep(),
        QiStrategyPackRolloutPlanStep(),
        CompilerIntakeReviewStep(),
        CompilerRecompileStep(),
        CompilerSyncSourcesStep(),
        ExternalEmergentScanStep(),
        ExternalSkillScoutPlanStep(),
        ExternalSkillCandidateReviewStep(),
        RouteRuleMiningStep(),
        TaskBoundaryEvalStep(),
        PheromoneDecayStep(),
    ]:
        register_step(step)


register_default_steps()


# ============= Long-running worker ============


async def idle_batch_worker(
    *,
    interval_sec: int = 3600,
    tenant_id: str = "u-sylvan",
    enabled: set[str] | None = None,
    use_anchor_expand: bool | None = None,
    anchor_max_rounds: int | None = None,
) -> None:
    """Background worker: every `interval_sec`, run all enabled steps.

    Started from app lifespan if KUN_IDLE_BATCH_ENABLED=true.
    """
    if use_anchor_expand is None:
        use_anchor_expand = os.getenv("KUN_IDLE_BATCH_ANCHOR_EXPAND_ENABLED", "1") == "1"
    if anchor_max_rounds is None:
        anchor_max_rounds = max(1, int(os.getenv("KUN_IDLE_BATCH_ANCHOR_EXPAND_MAX_ROUNDS", "3")))
    mode = "anchor_expand" if use_anchor_expand else "all"
    log.info(
        "idle_batch.worker.start",
        interval_sec=interval_sec,
        tenant_id=tenant_id,
        mode=mode,
        anchor_max_rounds=anchor_max_rounds,
    )
    while True:
        try:
            if use_anchor_expand:
                await run_anchor_then_expand_once(
                    tenant_id,
                    enabled=enabled,
                    max_rounds=anchor_max_rounds,
                )
            else:
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
    strategy: Literal["all", "anchor_expand"] = "all",
    max_rounds: int = 3,
) -> list[StepReport]:
    """Run one pass of all steps. Used by CLI + tests."""
    if strategy == "anchor_expand":
        reports = await run_anchor_then_expand_once(
            tenant_id,
            enabled=enabled,
            max_rounds=max_rounds,
        )
    else:
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


async def _queue_list(queue: Any, *, tenant_id: str, limit: int) -> list[Any]:
    method = getattr(queue, "list", None)
    if method is None:
        return []
    result = method(tenant_id, limit=limit)
    if asyncio.iscoroutine(result):
        result = await result
    return list(result) if isinstance(result, list) else []


async def _call_data_source(method_name: str, tenant_id: str) -> Any:
    data_source = _data_source
    if data_source is None and method_name in {
        "completed_task_history",
        "qi_problem_signals",
    }:
        data_source = IdleBatchDbDataSource()
    if data_source is None:
        return None
    method = getattr(data_source, method_name, None)
    if method is None:
        return None
    result = method(tenant_id)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _task_history_from_db_rows(
    result: Any | None, task: Any, runtime: Any | None
) -> dict[str, Any]:
    result_json = dict(getattr(result, "result_json", None) or {})
    runtime_blob = dict(getattr(runtime, "blob", None) or {}) if runtime is not None else {}
    result_status = str(getattr(result, "status", "") or "")
    runtime_status = str(getattr(runtime, "status", "") or "")
    status = result_status or runtime_status or "done"
    verification_status = str(
        result_json.get("validation_outcome")
        or result_json.get("verification_status")
        or result_json.get("status")
        or runtime_blob.get("verification_status")
        or runtime_blob.get("last_error")
        or runtime_blob.get("blocked_reason")
        or status
    )
    task_type = str(getattr(task, "task_type", "") or result_json.get("task_type") or "general")
    success_criteria = str(getattr(task, "success_criteria_short", "") or "").strip()
    answer = str(getattr(result, "answer", "") or "").strip()
    summary = success_criteria or answer or f"{task_type} task {status}"
    if len(summary) > 240:
        summary = f"{summary[:237]}..."
    updated_at = (
        getattr(result, "updated_at", None)
        or getattr(result, "created_at", None)
        or getattr(runtime, "last_updated", None)
        or getattr(runtime, "finished_at", None)
    )
    cost_usd = _float(getattr(result, "cost_usd_equivalent", None), default=0.0)
    if result is None:
        cost_usd = _float(getattr(runtime, "accumulated_cost_usd_equivalent", None), default=0.0)
    tokens_in = int(getattr(result, "tokens_in", 0) or 0)
    tokens_out = int(getattr(result, "tokens_out", 0) or 0)
    runtime_tokens = int(getattr(runtime, "accumulated_tokens", 0) or 0) if runtime else 0
    failure_evidence = _runtime_failure_evidence(runtime_blob)
    return {
        "history_id": str(
            getattr(result, "task_id", "")
            or getattr(runtime, "task_ref", "")
            or getattr(task, "task_id", "")
        ),
        "tenant_id": str(getattr(task, "tenant_id", "") or getattr(runtime, "tenant_id", "")),
        "task_type": task_type,
        "summary": summary,
        "outcome": _history_outcome_from_status(status, verification_status),
        "risk": _normalize_idle_history_risk(getattr(task, "risk_level", None)),
        "verification_status": verification_status,
        "cost_usd": cost_usd,
        "completed_at": updated_at.isoformat() if isinstance(updated_at, datetime) else None,
        "evidence": {
            "source": "idle_batch.db.completed_task_history",
            "tenant_id": str(getattr(task, "tenant_id", "") or getattr(runtime, "tenant_id", "")),
            "task_id": str(
                getattr(task, "task_id", "")
                or getattr(result, "task_id", "")
                or getattr(runtime, "task_ref", "")
            ),
            "result_status": result_status,
            "runtime_status": runtime_status,
            "runtime_step": int(getattr(runtime, "current_step", 0) or 0)
            if runtime is not None
            else 0,
            "execution_mode": result_json.get("execution_mode")
            or runtime_blob.get("execution_mode"),
            "strategy_pack": result_json.get("strategy_pack") or runtime_blob.get("strategy_pack"),
            "failure_evidence": failure_evidence,
            "answer_preview": answer[:400],
            "surprise_score": _float(getattr(result, "surprise_score", None), default=0.0),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "runtime_tokens": runtime_tokens,
        },
    }


def _runtime_failure_evidence(runtime_blob: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "error",
        "last_error",
        "failure_reason",
        "blocked_reason",
        "exception",
        "rollback_hint",
        "validation_outcome",
        "verification_status",
    )
    return {
        key: str(runtime_blob.get(key))[:500]
        for key in keys
        if runtime_blob.get(key) not in {None, ""}
    }


def _history_outcome_from_status(status: str, verification_status: str) -> str:
    normalized = status.lower()
    verification = verification_status.lower()
    if normalized == "done" and not any(token in verification for token in ("fail", "error")):
        return "completed"
    if normalized == "done":
        return "completed_with_verification_issue"
    if normalized:
        return f"{normalized}_task"
    return "completed"


def _normalize_idle_history_risk(value: Any) -> Literal["low", "medium", "high", "critical"]:
    normalized = str(value or "low").strip().lower()
    if normalized in {"low", "medium", "high", "critical"}:
        return cast(Literal["low", "medium", "high", "critical"], normalized)
    return "low"


async def _external_scan_rows(tenant_id: str) -> list[dict[str, Any]]:
    rows = await _source_list("external_scan_items", tenant_id)
    rows.extend(_external_scan_rows_from_env(tenant_id))
    return [
        row
        for row in rows
        if str(row.get("task_type") or "").strip() and _external_scan_source_kind(row) is not None
    ]


def _external_scan_rows_from_env(tenant_id: str) -> list[dict[str, Any]]:
    raw_files = os.getenv("KUN_EXTERNAL_SCAN_SOURCE_FILES", "")
    source_files = [item.strip() for item in raw_files.split(",") if item.strip()]
    if not source_files:
        return []

    config_root_raw = os.getenv("KUN_EXTERNAL_SCAN_CONFIG_ROOT") or None
    config_root = Path(config_root_raw).expanduser().resolve() if config_root_raw else None
    rows: list[dict[str, Any]] = []
    for source_file in source_files:
        payload = _read_external_scan_payload(source_file, config_root=config_root)
        if isinstance(payload, list):
            items = payload
            payload_tenant = tenant_id
        else:
            items = payload.get("items", [])
            payload_tenant = str(payload.get("tenant_id") or tenant_id)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            row_tenant = str(item.get("tenant_id") or payload_tenant or tenant_id)
            if row_tenant != tenant_id:
                continue
            rows.append({**item, "tenant_id": row_tenant})
    return rows


def _read_external_scan_payload(
    source_file: str,
    *,
    config_root: Path | None,
) -> dict[str, Any] | list[Any]:
    raw_path = Path(source_file).expanduser()
    path = raw_path if raw_path.is_absolute() else (config_root or Path.cwd()) / raw_path
    resolved = path.resolve(strict=False)
    if config_root is not None:
        try:
            resolved.relative_to(config_root)
        except ValueError:
            log.warning("external_scan.source_outside_config_root", source_file=source_file)
            return {}
    try:
        return cast(dict[str, Any] | list[Any], json.loads(resolved.read_text(encoding="utf-8")))
    except Exception as exc:
        log.warning("external_scan.source_read_failed", source_file=source_file, error=str(exc))
        return {}


_EXTERNAL_SOURCE_KINDS = {
    "github_issue",
    "arxiv",
    "reddit",
    "hackernews",
    "internal_history",
    "llm_judgment",
    "competitor_changelog",
}


def _external_scan_source_kind(row: dict[str, Any]) -> str | None:
    source_kind = str(row.get("source_kind") or row.get("kind") or "").strip()
    return source_kind if source_kind in _EXTERNAL_SOURCE_KINDS else None


def _external_scan_task_types(rows: list[dict[str, Any]]) -> list[str]:
    task_types: list[str] = []
    seen: set[str] = set()
    for row in rows:
        task_type = str(row.get("task_type") or "").strip()
        if task_type and task_type not in seen:
            task_types.append(task_type)
            seen.add(task_type)
    return task_types


def _external_scan_fetchers(
    rows: list[dict[str, Any]],
) -> dict[Any, Callable[[str], Awaitable[list[dict[str, Any]]]]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_kind = _external_scan_source_kind(row)
        if source_kind is None:
            continue
        by_source.setdefault(source_kind, []).append(row)

    def make_fetcher(
        source_rows: list[dict[str, Any]],
    ) -> Callable[[str], Awaitable[list[dict[str, Any]]]]:
        async def fetcher(task_type: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for row in source_rows:
                if str(row.get("task_type") or "").strip() != task_type:
                    continue
                out.append(
                    {
                        "url": str(row.get("url") or row.get("source_url") or ""),
                        "snippet": str(row.get("snippet") or row.get("summary") or "")[:1000],
                        "estimated_outcome_delta": _float(
                            row.get("estimated_outcome_delta"),
                            default=0.0,
                        ),
                        "estimated_cost_delta": _float(
                            row.get("estimated_cost_delta"),
                            default=0.0,
                        ),
                    }
                )
            return out

        return fetcher

    return {
        source_kind: make_fetcher(source_rows) for source_kind, source_rows in by_source.items()
    }


async def _external_skill_rows(tenant_id: str) -> list[dict[str, Any]]:
    rows = await _source_list("external_skill_candidates", tenant_id)
    rows.extend(_external_skill_rows_from_env(tenant_id))
    rows.extend(await _external_skill_github_repo_rows_from_env(tenant_id))
    return [
        {**row, "tenant_id": str(row.get("tenant_id") or tenant_id)}
        for row in rows
        if _external_skill_row_matches_tenant(row, tenant_id)
    ]


async def _external_skill_task_needs(tenant_id: str) -> list[dict[str, Any]]:
    """Build small task-demand cards for external skill review.

    This is the missing bridge between "we found an external skill" and
    "does KUN currently need it?".  It stays review-only and uses existing Qi
    problem signals / completed task history, so no external discovery work
    blocks user requests.
    """

    needs: list[dict[str, Any]] = []
    for raw in await _source_list("qi_problem_signals", tenant_id):
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or "").strip()
        task_type = str(raw.get("task_type") or raw.get("category") or "").strip()
        if summary or task_type:
            needs.append(
                {
                    "source": "qi_problem_signal",
                    "task_type": task_type,
                    "summary": summary,
                    "description": " ".join(
                        part
                        for part in [
                            task_type,
                            summary,
                            json.dumps(raw.get("evidence") or {}, ensure_ascii=False, default=str),
                        ]
                        if part
                    ),
                }
            )
    for raw in await _source_list("completed_task_history", tenant_id):
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or "").strip()
        task_type = str(raw.get("task_type") or "").strip()
        if summary or task_type:
            needs.append(
                {
                    "source": "completed_task_history",
                    "task_type": task_type,
                    "summary": summary,
                    "description": " ".join(
                        part
                        for part in [
                            task_type,
                            summary,
                            str(raw.get("outcome") or ""),
                            str(raw.get("risk") or ""),
                        ]
                        if part
                    ),
                }
            )
    return _dedupe_task_need_cards(needs)[:10]


def _dedupe_task_need_cards(needs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for need in needs:
        key = hashlib.sha256(
            json.dumps(
                {
                    "task_type": need.get("task_type"),
                    "summary": need.get("summary"),
                    "description": need.get("description"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        out.append(need)
    return out


def _external_skill_rows_from_env(tenant_id: str) -> list[dict[str, Any]]:
    raw_files = os.getenv("KUN_EXTERNAL_SKILL_SOURCE_FILES", "")
    source_files = [item.strip() for item in raw_files.split(",") if item.strip()]
    if not source_files:
        return []

    config_root_raw = os.getenv("KUN_EXTERNAL_SKILL_CONFIG_ROOT") or None
    config_root = Path(config_root_raw).expanduser().resolve() if config_root_raw else None
    rows: list[dict[str, Any]] = []
    for source_file in source_files:
        payload = _read_external_scan_payload(source_file, config_root=config_root)
        if isinstance(payload, list):
            items = payload
            payload_tenant = tenant_id
        else:
            items = payload.get("items", [])
            payload_tenant = str(payload.get("tenant_id") or tenant_id)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            row_tenant = str(item.get("tenant_id") or payload_tenant or tenant_id)
            if row_tenant != tenant_id:
                continue
            rows.append({**item, "tenant_id": row_tenant})
    return rows


async def _external_skill_github_repo_rows_from_env(tenant_id: str) -> list[dict[str, Any]]:
    """Fetch opt-in GitHub skill repos for review-only Qi intake.

    This is intentionally explicit. KUN will not crawl marketplaces by default;
    operators must set KUN_EXTERNAL_SKILL_GITHUB_REPOS to comma-separated
    owner/name or https://github.com/owner/name refs.
    """

    raw_refs = os.getenv("KUN_EXTERNAL_SKILL_GITHUB_REPOS", "")
    repo_refs = [item.strip() for item in raw_refs.split(",") if item.strip()]
    if not repo_refs:
        return []

    max_repos = max(1, min(_int_env("KUN_EXTERNAL_SKILL_GITHUB_MAX_REPOS", 5), 20))
    rows: list[dict[str, Any]] = []
    for repo_ref in repo_refs[:max_repos]:
        try:
            metadata = await fetch_github_repo_external_skill_metadata(repo_ref)
        except Exception as exc:
            log.warning(
                "external_skill.github_repo_fetch_failed",
                tenant_id=tenant_id,
                repo_ref=repo_ref,
                error=str(exc),
            )
            continue
        rows.append(
            {
                **metadata,
                "tenant_id": tenant_id,
                "source_config": "KUN_EXTERNAL_SKILL_GITHUB_REPOS",
            }
        )
    return rows


def _external_skill_row_matches_tenant(row: dict[str, Any], tenant_id: str) -> bool:
    row_tenant = str(row.get("tenant_id") or tenant_id)
    return row_tenant == tenant_id


async def _persist_methodology_rules(*, tenant_id: str, rules: list[str]) -> list[str]:
    """Persist distilled rules as methodology assets for future ContextPacker reuse."""

    if not rules:
        return []
    from kun.context.assets import AssetLayer, LayeredAsset
    from kun.context.storage import get_store

    store = get_store()
    existing = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=1000)
    seen_hashes = {
        str(asset.l1_metadata.get("rule_hash"))
        for asset in existing
        if asset.l1_metadata.get("source") == "idle_batch.methodology_distill"
    }
    created: list[str] = []
    for rule in rules:
        rule_hash = hashlib.sha256(rule.encode("utf-8")).hexdigest()[:16]
        if rule_hash in seen_hashes:
            continue
        asset = LayeredAsset.build(
            "methodology",
            tenant_id,
            metadata={
                "source": "idle_batch.methodology_distill",
                "rule_hash": rule_hash,
                "reuse_scope": AssetLayer.L2_PROJECT.value,
            },
            summary=rule,
            layer=AssetLayer.L2_PROJECT,
            tags=["methodology", "distilled", "idle_batch"],
        )
        await store.put(asset)
        created.append(asset.asset_id)
        seen_hashes.add(rule_hash)
    return created


async def _persist_strategy_pack_drafts(
    *,
    tenant_id: str,
    drafts: list[Any],
    evaluation_records: list[Any] | None = None,
    lab_replay_records: list[Any] | None = None,
    tree_search_records: list[Any] | None = None,
    strategy_review_packages: list[Any] | None = None,
) -> list[str]:
    """Persist Qi strategy-pack drafts as review-only methodology assets.

    These assets are deliberately *not* Watchtower packs.  They are context
    material for NUO / human / strong-model review, so Qi's idle exploration can
    be inspected without silently changing production routing.
    """

    if not drafts:
        return []
    from kun.context.assets import AssetLayer, LayeredAsset
    from kun.context.storage import get_store
    from kun.datamodel.decision_ticket import ticket_from_qi_experiment

    store = get_store()
    existing = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=1000)
    existing_by_draft_id = {
        str(asset.l1_metadata.get("draft_id")): asset
        for asset in existing
        if asset.l1_metadata.get("source") == "qi.idle_replay.strategy_pack_draft"
        and asset.l1_metadata.get("draft_id")
    }
    seen_draft_ids = set(existing_by_draft_id)
    created: list[str] = []
    evaluations_by_target: dict[str, list[dict[str, Any]]] = {}
    for record in evaluation_records or []:
        if hasattr(record, "model_dump"):
            payload = record.model_dump(mode="json")
        elif isinstance(record, dict):
            payload = record
        else:
            continue
        target_id = str(payload.get("target_id") or "")
        if target_id:
            evaluations_by_target.setdefault(target_id, []).append(payload)
    replays_by_draft: dict[str, list[dict[str, Any]]] = {}
    for record in lab_replay_records or []:
        if hasattr(record, "model_dump"):
            payload = record.model_dump(mode="json")
        elif isinstance(record, dict):
            payload = record
        else:
            continue
        draft_id = str(payload.get("draft_id") or "")
        if draft_id:
            replays_by_draft.setdefault(draft_id, []).append(payload)
    tree_records_by_target: dict[str, list[dict[str, Any]]] = {}
    for record in tree_search_records or []:
        if hasattr(record, "model_dump"):
            payload = record.model_dump(mode="json")
        elif isinstance(record, dict):
            payload = record
        else:
            continue
        target_id = str(payload.get("target_id") or "")
        if target_id:
            tree_records_by_target.setdefault(target_id, []).append(payload)
    review_package_by_draft: dict[str, dict[str, Any]] = {}
    for package in strategy_review_packages or []:
        if hasattr(package, "model_dump"):
            payload = package.model_dump(mode="json")
        elif isinstance(package, dict):
            payload = package
        else:
            continue
        draft_id = str(payload.get("draft_id") or "")
        if draft_id:
            review_package_by_draft[draft_id] = payload
    for draft in drafts:
        draft_id = str(getattr(draft, "draft_id", ""))
        if not draft_id:
            continue
        if draft_id in seen_draft_ids:
            existing_asset = existing_by_draft_id.get(draft_id)
            if existing_asset is not None:
                changed = _merge_strategy_pack_review_records(
                    existing_asset,
                    evaluation_records=evaluations_by_target.get(draft_id, []),
                    lab_replay_records=replays_by_draft.get(draft_id, []),
                    tree_search_records=tree_records_by_target.get(draft_id, []),
                    strategy_review_package=review_package_by_draft.get(draft_id),
                )
                if changed:
                    await store.put(existing_asset)
                    created.append(existing_asset.asset_id)
            continue
        status = str(getattr(draft, "status", "draft"))
        proposed_pack_id = str(getattr(draft, "proposed_pack_id", "unknown"))
        task_type_patterns = list(getattr(draft, "task_type_patterns", []) or [])
        requires_strong_review = bool(getattr(draft, "requires_strong_review", False))
        qi_ticket = ticket_from_qi_experiment(
            tenant_id=tenant_id,
            target_id=draft_id,
            target_kind="strategy_pack_draft",
            experiment=draft,
            risk_level="high" if requires_strong_review else "medium",
        )
        asset = LayeredAsset.build(
            "methodology",
            tenant_id,
            metadata={
                "source": "qi.idle_replay.strategy_pack_draft",
                "memory_layer": "methodology",
                "draft_id": draft_id,
                "candidate_id": str(getattr(draft, "candidate_id", "")),
                "source_signal_id": str(getattr(draft, "source_signal_id", "")),
                "proposed_pack_id": proposed_pack_id,
                "status": status,
                "requires_human_review": True,
                "requires_strong_review": requires_strong_review,
                "production_action": False,
                "promotion_blocked_until_review": True,
                "decision_ticket": qi_ticket.event_payload(),
                "evaluation_records": evaluations_by_target.get(draft_id, []),
                "lab_replay_records": replays_by_draft.get(draft_id, []),
                "tree_search_records": tree_records_by_target.get(draft_id, []),
                "strategy_review_package": review_package_by_draft.get(draft_id),
                "strategy_pack_draft": draft.model_dump(mode="json")
                if hasattr(draft, "model_dump")
                else {},
            },
            summary=_strategy_pack_draft_summary(draft),
            layer=AssetLayer.L2_PROJECT,
            tags=sorted(
                {
                    "qi",
                    "strategy_pack_draft",
                    "review_only",
                    f"status:{status}",
                    f"proposed_pack:{proposed_pack_id}",
                    *[f"task_type:{pattern}" for pattern in task_type_patterns[:3]],
                    *(["strong_review_required"] if requires_strong_review else []),
                }
            ),
        )
        await store.put(asset)
        created.append(asset.asset_id)
        seen_draft_ids.add(draft_id)
    return created


def _merge_strategy_pack_review_records(
    asset: Any,
    *,
    evaluation_records: list[dict[str, Any]],
    lab_replay_records: list[dict[str, Any]],
    tree_search_records: list[dict[str, Any]],
    strategy_review_package: dict[str, Any] | None = None,
) -> bool:
    """Merge new review evidence into an existing draft asset.

    Qi can generate the same draft first, then later gather strong-model or lab
    replay evidence.  Dropping that later evidence would turn the asset pool
    into a stale candidate graveyard.
    """

    changed = False
    if evaluation_records:
        merged = _merge_records_by_key(
            list(asset.l1_metadata.get("evaluation_records") or []),
            evaluation_records,
            key="evaluation_id",
        )
        if merged != asset.l1_metadata.get("evaluation_records"):
            asset.l1_metadata["evaluation_records"] = merged
            changed = True
    if lab_replay_records:
        merged = _merge_records_by_key(
            list(asset.l1_metadata.get("lab_replay_records") or []),
            lab_replay_records,
            key="experiment_id",
        )
        if merged != asset.l1_metadata.get("lab_replay_records"):
            asset.l1_metadata["lab_replay_records"] = merged
            changed = True
    if tree_search_records:
        merged = _merge_records_by_key(
            list(asset.l1_metadata.get("tree_search_records") or []),
            tree_search_records,
            key="evaluation_id",
        )
        if merged != asset.l1_metadata.get("tree_search_records"):
            asset.l1_metadata["tree_search_records"] = merged
            changed = True
    if strategy_review_package:
        current_package = asset.l1_metadata.get("strategy_review_package")
        if current_package != strategy_review_package:
            asset.l1_metadata["strategy_review_package"] = strategy_review_package
            changed = True
    return changed


def _strategy_review_package_summary(packages: list[Any]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    strong_missing = 0
    for package in packages:
        status = str(getattr(package, "status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        gate = getattr(package, "strong_review_gate", None)
        if getattr(gate, "status", "") == "missing":
            strong_missing += 1
    return {
        "packages": len(packages),
        "by_status": by_status,
        "strong_review_missing": strong_missing,
        "production_action": False,
        "promotion_allowed": False,
    }


def _merge_records_by_key(
    current: list[Any],
    incoming: list[dict[str, Any]],
    *,
    key: str,
) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in [*current, *incoming]:
        if not isinstance(record, dict):
            continue
        record_key = str(record.get(key) or "")
        if not record_key:
            record_key = hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
        out[record_key] = record
    return list(out.values())


def _strategy_pack_draft_summary(draft: Any) -> str:
    proposed_pack_id = str(getattr(draft, "proposed_pack_id", "unknown"))
    display_name = str(getattr(draft, "display_name", proposed_pack_id))
    status = str(getattr(draft, "status", "draft"))
    mode = str(getattr(draft, "default_execution_mode", "SMART"))
    metrics = ", ".join(list(getattr(draft, "metric_dimensions", []) or [])[:4])
    risks = ", ".join(list(getattr(draft, "risk_watch", []) or [])[:4])
    return (
        f"Review-only Qi StrategyPack draft {proposed_pack_id} ({display_name}); "
        f"status={status}; default_mode={mode}; metrics={metrics or 'n/a'}; "
        f"risk_watch={risks or 'n/a'}; production_action=false."
    )


def _float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _qi_budget_remaining(tenant_id: str) -> float:
    try:
        from kun.qi import get_qi_budget

        return get_qi_budget().remaining_budget(tenant_id)
    except Exception:
        log.debug("qi_budget.remaining_failed", tenant_id=tenant_id, exc_info=True)
        return 0.0


def _charge_qi_budget_for_evaluations(
    tenant_id: str,
    records: list[Any],
    *,
    reason: str,
) -> dict[str, Any]:
    chargeable = [
        _float(_record_value(record, "cost_estimate_usd"))
        for record in records
        if _record_should_charge_qi_budget(record)
    ]
    cost_usd = round(sum(cost for cost in chargeable if cost > 0), 6)
    if cost_usd <= 0:
        return {
            "reason": reason,
            "charged_usd": 0.0,
            "charged_records": 0,
            "status": "no_charge",
        }
    try:
        from kun.qi import QiBudgetExhaustedError, get_qi_budget

        spent = get_qi_budget().add_cost(tenant_id, cost_usd)
        return {
            "reason": reason,
            "charged_usd": cost_usd,
            "charged_records": len(chargeable),
            "today_spent_usd": round(spent, 6),
            "status": "charged",
        }
    except QiBudgetExhaustedError as exc:
        log.warning(
            "qi_budget.charge_exhausted",
            tenant_id=tenant_id,
            reason=reason,
            cost_usd=cost_usd,
            error=str(exc),
        )
        return {
            "reason": reason,
            "charged_usd": 0.0,
            "attempted_charge_usd": cost_usd,
            "charged_records": len(chargeable),
            "status": "budget_exhausted",
            "error": str(exc),
        }
    except Exception as exc:
        log.warning(
            "qi_budget.charge_failed",
            tenant_id=tenant_id,
            reason=reason,
            cost_usd=cost_usd,
            error=str(exc),
        )
        return {
            "reason": reason,
            "charged_usd": 0.0,
            "attempted_charge_usd": cost_usd,
            "charged_records": len(chargeable),
            "status": "charge_failed",
            "error": str(exc),
        }


def _record_should_charge_qi_budget(record: Any) -> bool:
    if str(_record_value(record, "status") or "") != "evaluated":
        return False
    kind = str(_record_value(record, "evaluator_kind") or "")
    if kind == "strong_model":
        return True
    if kind != "local_model":
        return False
    evidence = _record_value(record, "evidence")
    if isinstance(evidence, dict) and evidence.get("cheap_router_provider"):
        return True
    notes = _record_value(record, "notes")
    return isinstance(notes, list) and "cheap_router_model" in {str(item) for item in notes}


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _spread(values: Any) -> float:
    if not isinstance(values, list) or not values:
        return 0.0
    nums = [_float(value) for value in values]
    return max(nums) - min(nums)
