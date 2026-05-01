"""Blackboard data sources — wire 黑板 5 endpoint 到 events / TaskRow / RuntimeStateRow.

V2.1 wire (W5): 把 blackboard.py 的 register_data_source hook 接到真实数据库.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc, or_, select

from kun.api.blackboard import register_data_source
from kun.core.db import session_scope
from kun.core.orm import EventRow, RuntimeStateRow, StateLedgerEntryRow, TaskRow
from kun.core.state_ledger import (
    StateLedgerEntry,
    StateLedgerTrail,
    get_state_ledger,
    replay_state_ledger_story,
)

logger = logging.getLogger(__name__)


def install_blackboard_data_sources() -> None:
    """注册黑板 5 endpoint 的数据源 hook.

    应在 lifespan startup 调用一次.
    """
    register_data_source("tasks", _tasks_source)
    register_data_source("events", _events_source)
    register_data_source("state", _state_source)
    register_data_source("state_ledger", _state_ledger_source)
    register_data_source("state_ledger_history", _state_ledger_history_source)
    register_data_source("state_ledger_story", _state_ledger_story_source)
    register_data_source("state_ledger_audit", _state_ledger_audit_source)
    register_data_source("workspace", _workspace_source)
    register_data_source("assets", _assets_source)


async def _tasks_source(
    *,
    tenant_id: str,
    user_id: str,
    status: str | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """任务看板. 从 TaskRow + RuntimeStateRow join 取."""
    return await _tasks_source_async(tenant_id, user_id, status)


async def _tasks_source_async(
    tenant_id: str,
    user_id: str,
    status: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(TaskRow, RuntimeStateRow)
                .join(
                    RuntimeStateRow,
                    RuntimeStateRow.task_ref == TaskRow.task_id,
                    isouter=True,
                )
                .where(TaskRow.tenant_id == tenant_id)
                .order_by(desc(TaskRow.created_at))
                .limit(50)
            )
            if user_id and user_id != "u-anon":
                stmt = stmt.where(TaskRow.user_id == user_id)
            if status:
                stmt = stmt.where(RuntimeStateRow.status == status)
            rows = (await session.execute(stmt)).all()
            for task, rt in rows:
                progress = 0.0
                cost = float(task.estimated_cost_usd or 0.0)
                if rt is not None and rt.total_planned_steps:
                    progress = (rt.current_step or 0) / max(rt.total_planned_steps, 1)
                    cost = float(rt.accumulated_cost_usd_actual or 0.0)
                out.append(
                    {
                        "task_id": task.task_id,
                        "title": task.success_criteria_short[:120],
                        "status": (rt.status if rt else "queued"),
                        "progress": round(progress, 2),
                        "cost_so_far_usd": round(cost, 4),
                        "started_at": task.created_at.isoformat(),
                        "estimated_eta_sec": int(task.estimated_duration_sec or 0),
                    }
                )
    except Exception:
        logger.exception("blackboard.tasks_source failed (returning empty)")
    return out


async def _events_source(
    *,
    tenant_id: str,
    user_id: str,
    limit: int = 50,
    **_: Any,
) -> list[dict[str, Any]]:
    return await _events_source_async(tenant_id, limit)


async def _events_source_async(
    tenant_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(EventRow)
                .where(EventRow.tenant_id == tenant_id)
                .order_by(desc(EventRow.occurred_at))
                .limit(limit)
            )
            for row in (await session.execute(stmt)).scalars():
                severity = "info"
                if "error" in row.event_type or "failed" in row.event_type:
                    severity = "error"
                elif "warn" in row.event_type or "alert" in row.event_type:
                    severity = "warn"
                elif "critical" in row.event_type or "incident" in row.event_type:
                    severity = "critical"
                out.append(
                    {
                        "event_id": row.event_id,
                        "event_type": row.event_type,
                        "occurred_at": row.occurred_at.isoformat(),
                        "summary": row.subject[:200],
                        "severity": severity,
                    }
                )
    except Exception:
        logger.exception("blackboard.events_source failed")
    return out


async def _state_source(
    *,
    tenant_id: str,
    user_id: str,
    **_: Any,
) -> dict[str, Any]:
    return await _state_source_async(tenant_id, user_id)


async def _state_source_async(tenant_id: str, user_id: str) -> dict[str, Any]:
    running = 0
    queued = 0
    cost_today = 0.0
    remaining_budget = 0.0
    ledger_entries: list[dict[str, Any]] = []
    system_findings: list[dict[str, Any]] = []
    urgent_alert_count = 0
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(
                    RuntimeStateRow.status,
                    RuntimeStateRow.accumulated_cost_usd_actual,
                    RuntimeStateRow.accumulated_cost_usd_equivalent,
                    TaskRow.estimated_cost_usd,
                )
                .join(TaskRow, TaskRow.task_id == RuntimeStateRow.task_ref)
                .where(TaskRow.tenant_id == tenant_id)
                .order_by(desc(TaskRow.created_at))
                .limit(200)
            )
            for status, actual_cost, equivalent_cost, estimated_cost in (
                await session.execute(stmt)
            ).all():
                if status == "running":
                    running += 1
                elif status == "queued":
                    queued += 1
                cost_today += float(actual_cost or 0.0)
                if status in {"queued", "running", "paused"}:
                    remaining_budget += max(
                        0.0,
                        float(estimated_cost or 0.0) - float(equivalent_cost or 0.0),
                    )
    except Exception:
        logger.exception("blackboard.state_source failed")
    ledger_data = await _state_ledger_source_async(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=None,
    )
    if isinstance(ledger_data, list):
        ledger_entries = ledger_data

    health: str = "healthy"
    if running > 10:
        health = "warn"
    if cost_today > 100.0:
        health = "warn"
    try:
        from kun.engineering.nuo_system_health import collect_system_health_report

        report = await collect_system_health_report(tenant_id=tenant_id)
        system_findings = [
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "subsystem": finding.subsystem,
                "title": finding.title,
                "detail": finding.detail,
                "suggested_action": finding.suggested_action,
            }
            for finding in report.findings
            if finding.severity in {"warn", "error", "critical"}
        ][:5]
        urgent_alert_count = sum(
            1 for finding in report.findings if finding.severity in {"error", "critical"}
        )
        if report.worst_severity == "critical":
            health = "critical"
        elif urgent_alert_count > 0 or report.worst_severity in {"warn", "error"}:
            health = "warn"
    except Exception:
        logger.exception("blackboard.state_source health report failed")

    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task_count_running": running,
        "task_count_queued": queued,
        "total_cost_today_usd": round(cost_today, 4),
        "total_cost_remaining_budget_usd": round(remaining_budget, 4),
        "health_indicator": health,
        "urgent_alert_count": urgent_alert_count,
        "system_findings": system_findings,
        "active_state_ledger": ledger_entries,
        "last_update": datetime.now(UTC).isoformat(),
    }


async def _state_ledger_source(
    *,
    tenant_id: str,
    user_id: str,
    task_id: str | None = None,
    **_: Any,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    return await _state_ledger_source_async(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
    )


async def _state_ledger_source_async(
    *,
    tenant_id: str,
    user_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    try:
        ledger = get_state_ledger()
        if task_id is not None:
            persisted_entry = await _persistent_state_ledger_source(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
            entry = ledger.snapshot(task_id, tenant_id=tenant_id)
            if entry is not None and _matches_user(entry.user_id, user_id):
                return entry.model_dump(mode="json")
            if persisted_entry is not None:
                return persisted_entry
            runtime_entry = await _runtime_state_ledger_source(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
            if runtime_entry is not None:
                return runtime_entry
            return await _replayed_state_ledger_source(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
            )
        persisted_entries = await _persistent_state_ledger_source(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=None,
        )
        if not isinstance(persisted_entries, list):
            persisted_entries = []
        merged_entries: dict[str, dict[str, Any]] = {
            str(item.get("task_id") or ""): item
            for item in persisted_entries
            if isinstance(item, dict) and item.get("task_id")
        }
        hot_entries = [
            entry.model_dump(mode="json")
            for entry in ledger.active_snapshots(tenant_id=tenant_id)
            if _matches_user(entry.user_id, user_id)
        ]
        for item in hot_entries:
            merged_entries[str(item.get("task_id") or "")] = item
        if merged_entries:
            entries = list(merged_entries.values())
            entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return entries[:50]
        fallback = await _runtime_state_ledger_source(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=None,
        )
        if isinstance(fallback, list) and fallback:
            return fallback
        replayed = await _replayed_state_ledger_source(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=None,
        )
        return replayed if isinstance(replayed, list) else []
    except Exception:
        logger.exception("blackboard.state_ledger_source failed")
        return None if task_id is not None else []


async def _persistent_state_ledger_source(
    *,
    tenant_id: str,
    user_id: str | None,
    task_id: str | None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(StateLedgerEntryRow)
                .where(StateLedgerEntryRow.tenant_id == tenant_id)
                .order_by(desc(StateLedgerEntryRow.updated_at))
            )
            if task_id is not None:
                stmt = stmt.where(StateLedgerEntryRow.task_id == task_id).limit(1)
            else:
                stmt = stmt.where(StateLedgerEntryRow.status.in_(("queued", "running", "paused")))
                stmt = stmt.limit(50)
            if user_id and user_id != "u-anon":
                stmt = stmt.where(StateLedgerEntryRow.user_id == user_id)
            rows = (await session.execute(stmt)).scalars().all()

        entries = [_state_ledger_entry_from_row(row).model_dump(mode="json") for row in rows]
        if task_id is not None:
            return entries[0] if entries else None
        return entries
    except Exception:
        logger.exception("blackboard.state_ledger_persistent_source failed")
        return None if task_id is not None else []


async def _state_ledger_history_source(
    *,
    tenant_id: str,
    user_id: str,
    task_id: str | None = None,
    limit: int = 100,
    **_: Any,
) -> list[dict[str, Any]]:
    return await _state_ledger_history_source_async(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
        limit=limit,
    )


async def _state_ledger_history_source_async(
    *,
    tenant_id: str,
    user_id: str | None = None,
    task_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Replay durable task history from EventRow.

    Hot StateLedger answers “现在怎样”。这个接口回答“过去发生过什么”。
    """
    out: list[dict[str, Any]] = []
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(EventRow)
                .outerjoin(
                    TaskRow,
                    (TaskRow.tenant_id == EventRow.tenant_id)
                    & (TaskRow.task_id == EventRow.task_ref),
                )
                .where(EventRow.tenant_id == tenant_id)
                .order_by(desc(EventRow.occurred_at))
                .limit(limit)
            )
            if task_id is not None:
                stmt = stmt.where(EventRow.task_ref == task_id)
            if user_id and user_id != "u-anon":
                stmt = stmt.where(or_(EventRow.task_ref.is_(None), TaskRow.user_id == user_id))
            rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            out.append(_state_ledger_history_item_from_event(row))
    except Exception:
        logger.exception("blackboard.state_ledger_history_source failed")
    return out


async def _state_ledger_story_source(
    *,
    tenant_id: str,
    user_id: str,
    task_id: str,
    limit: int = 100,
    **_: Any,
) -> dict[str, Any]:
    history = await _state_ledger_history_source_async(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
        limit=limit,
    )
    return replay_state_ledger_story(
        task_id,
        history,
        timeline_limit=20,
        history_limit_reached=len(history) >= limit,
    )


async def _state_ledger_audit_source(
    *,
    tenant_id: str,
    user_id: str,
    task_id: str,
    limit: int = 100,
    **_: Any,
) -> dict[str, Any]:
    """Compare the current snapshot with the EventRow replay story."""

    snapshot, source = await _state_ledger_snapshot_for_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
    )
    story = await _state_ledger_story_source(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
        limit=limit,
    )
    return _state_ledger_audit_from_snapshot_and_story(
        task_id=task_id,
        tenant_id=tenant_id,
        snapshot=snapshot,
        story=story,
        snapshot_source=source,
    )


async def _state_ledger_snapshot_for_audit(
    *,
    tenant_id: str,
    user_id: str | None,
    task_id: str,
) -> tuple[dict[str, Any] | None, str]:
    ledger = get_state_ledger()
    hot = ledger.snapshot(task_id, tenant_id=tenant_id)
    if hot is not None and _matches_user(hot.user_id, user_id):
        return hot.model_dump(mode="json"), "hot"

    persisted = await _persistent_state_ledger_source(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
    )
    if isinstance(persisted, dict):
        return persisted, "persistent"

    runtime = await _runtime_state_ledger_source(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
    )
    if isinstance(runtime, dict):
        return runtime, "runtime"

    replayed = await _replayed_state_ledger_source(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=task_id,
    )
    if isinstance(replayed, dict):
        return replayed, "events_replay"

    return None, "missing"


def _state_ledger_audit_from_snapshot_and_story(
    *,
    task_id: str,
    tenant_id: str,
    snapshot: dict[str, Any] | None,
    story: dict[str, Any],
    snapshot_source: str,
) -> dict[str, Any]:
    snapshot = snapshot or {}
    snapshot_found = bool(snapshot)
    event_count = _int_value(story.get("event_count"))
    replay_found = event_count > 0
    snapshot_status = str(snapshot.get("status") or "missing")
    replay_status = str(story.get("status") or "unknown")
    status_matches = snapshot_found and replay_found and snapshot_status == replay_status
    snapshot_cost = _float_value(snapshot.get("cost_so_far_usd"))
    replay_cost = _float_value(story.get("total_cost_usd"))
    cost_delta = round(snapshot_cost - replay_cost, 6)
    issues: list[str] = []
    if not snapshot_found:
        issues.append("missing_current_snapshot")
    if not replay_found:
        issues.append("missing_durable_history")
    if snapshot_found and replay_found and not status_matches:
        issues.append("status_drift")
    if abs(cost_delta) > 0.01:
        issues.append("cost_drift")
    gaps = story.get("gaps")
    if isinstance(gaps, list):
        issues.extend(str(item) for item in gaps if item)

    return {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "snapshot_source": snapshot_source if snapshot_found else "missing",
        "snapshot_found": snapshot_found,
        "replay_found": replay_found,
        "snapshot_status": snapshot_status,
        "replay_status": replay_status,
        "status_matches": status_matches,
        "snapshot_updated_at": _optional_str(snapshot.get("updated_at")),
        "replay_last_seen_at": _optional_str(story.get("last_seen_at")),
        "event_count": event_count,
        "decision_count": _int_value(story.get("decision_count")),
        "snapshot_cost_usd": snapshot_cost,
        "replay_cost_usd": replay_cost,
        "cost_delta_usd": cost_delta,
        "reconstruction_confidence": _float_value(story.get("reconstruction_confidence")),
        "drift_detected": any(item.endswith("_drift") for item in issues),
        "issues": issues,
    }


async def _runtime_state_ledger_source(
    *,
    tenant_id: str,
    user_id: str | None,
    task_id: str | None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Hydrate a readable ledger view from durable task/runtime snapshots.

    Hot StateLedger stays in-memory so orchestration does not wait on I/O. This
    fallback makes blackboard useful after API restarts by reading the durable
    `runtime_states` table that the orchestrator already writes.
    """
    async with session_scope(tenant_id=tenant_id) as session:
        stmt = (
            select(TaskRow, RuntimeStateRow)
            .join(RuntimeStateRow, RuntimeStateRow.task_ref == TaskRow.task_id)
            .where(
                TaskRow.tenant_id == tenant_id,
                RuntimeStateRow.tenant_id == tenant_id,
            )
            .order_by(desc(RuntimeStateRow.last_updated))
        )
        if task_id is not None:
            stmt = stmt.where(TaskRow.task_id == task_id).limit(1)
        else:
            stmt = stmt.where(RuntimeStateRow.status.in_(("queued", "running", "paused"))).limit(50)
        if user_id and user_id != "u-anon":
            stmt = stmt.where(TaskRow.user_id == user_id)
        rows = (await session.execute(stmt)).all()

    entries = [
        _entry_from_runtime_rows(task, runtime).model_dump(mode="json") for task, runtime in rows
    ]
    if task_id is not None:
        return entries[0] if entries else None
    return entries


async def _replayed_state_ledger_source(
    *,
    tenant_id: str,
    user_id: str | None,
    task_id: str | None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """Recover the ledger main view from durable EventRow history."""

    histories: dict[str, list[dict[str, Any]]] = {}
    tasks: dict[str, TaskRow] = {}
    async with session_scope(tenant_id=tenant_id) as session:
        stmt = (
            select(EventRow, TaskRow)
            .outerjoin(
                TaskRow,
                (TaskRow.tenant_id == EventRow.tenant_id) & (TaskRow.task_id == EventRow.task_ref),
            )
            .where(EventRow.tenant_id == tenant_id, EventRow.task_ref.is_not(None))
            .order_by(desc(EventRow.occurred_at))
            .limit(500 if task_id is None else 200)
        )
        if task_id is not None:
            stmt = stmt.where(EventRow.task_ref == task_id)
        if user_id and user_id != "u-anon":
            stmt = stmt.where(TaskRow.user_id == user_id)
        rows = (await session.execute(stmt)).all()

    for raw in rows:
        event, task = _event_task_pair(raw)
        if event is None or not event.task_ref:
            continue
        histories.setdefault(event.task_ref, []).append(
            _state_ledger_history_item_from_event(event)
        )
        if task is not None:
            tasks[event.task_ref] = task

    entries: list[dict[str, Any]] = []
    for replay_task_id, history in histories.items():
        story = replay_state_ledger_story(
            replay_task_id,
            history,
            timeline_limit=20,
            history_limit_reached=len(history) >= (200 if task_id else 500),
        )
        entry = _entry_from_replayed_story(
            story,
            tenant_id=tenant_id,
            user_id=user_id,
            task=tasks.get(replay_task_id),
        )
        if task_id is None and entry.status not in {"queued", "running", "paused"}:
            continue
        entries.append(entry.model_dump(mode="json"))

    entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    if task_id is not None:
        return entries[0] if entries else None
    return entries[:50]


def _state_ledger_entry_from_row(row: StateLedgerEntryRow) -> StateLedgerEntry:
    data = dict(row.snapshot_json or {})
    data.setdefault("tenant_id", row.tenant_id)
    data.setdefault("task_id", row.task_id)
    data.setdefault("user_id", row.user_id)
    data.setdefault("project_id", row.project_id)
    data.setdefault("status", row.status)
    data.setdefault("updated_at", row.updated_at)
    return StateLedgerEntry.model_validate(data)


def _entry_from_replayed_story(
    story: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str | None,
    task: TaskRow | None,
) -> StateLedgerEntry:
    task_user_id = task.user_id if task is not None else user_id
    title = task.success_criteria_short if task is not None else ""
    status = str(story.get("status") or "queued")
    if status not in {"queued", "running", "paused", "done", "failed", "cancelled"}:
        status = "queued"
    first_seen = _parse_dt(story.get("first_seen_at")) or datetime.now(UTC)
    last_seen = _parse_dt(story.get("last_seen_at")) or first_seen
    entry = StateLedgerEntry(
        task_id=str(story.get("task_id") or ""),
        tenant_id=tenant_id,
        user_id=task_user_id,
        project_id=task.project_id if task is not None else None,
        task_type=task.task_type if task is not None else "",
        title=title,
        current_goal=_goal_from_task(task) if task is not None else title,
        status=cast(Any, status),
        current_action=str(story.get("current_action") or ""),
        current_risk=task.risk_level if task is not None else "low",
        complexity_score=task.complexity_score if task is not None else 0.0,
        budget_estimated_usd=task.estimated_cost_usd if task is not None else 0.0,
        cost_so_far_usd=float(story.get("total_cost_usd") or 0.0),
        pending_confirmations=[
            str(item) for item in story.get("pending_confirmations", []) if item
        ],
        pending_reason=str(story.get("latest_reason") or ""),
        alert_flags=[str(item) for item in story.get("risk_flags", []) if item],
        decision_ticket_ids=[str(item) for item in story.get("decision_ticket_ids", []) if item],
        context_asset_ids=[str(item) for item in story.get("context_asset_ids", []) if item],
        skill_hints=[str(item) for item in story.get("skill_refs", []) if item],
        credit_assignment_count=int(story.get("credit_assignment_count") or 0),
        credit_assignment_summary=(
            dict(story["credit_assignment_summary"])
            if isinstance(story.get("credit_assignment_summary"), dict)
            else None
        ),
        resource_credit_summaries=[
            dict(item)
            for item in story.get("resource_credit_summaries", [])
            if isinstance(item, dict)
        ],
        top_credit_resource_kinds=[
            str(item) for item in story.get("top_credit_resource_kinds", []) if item
        ],
        top_credit_resources=[str(item) for item in story.get("top_credit_resources", []) if item],
        critical_path_step_ids=[
            int(item) for item in story.get("critical_path_step_ids", []) if isinstance(item, int)
        ],
        started_at=first_seen,
        updated_at=last_seen,
    )
    for item in story.get("timeline", [])[-5:]:
        if not isinstance(item, dict):
            continue
        entry.recent_events.append(
            StateLedgerTrail(
                at=_parse_dt(item.get("occurred_at")) or last_seen,
                kind=str(item.get("event_type") or "event.replayed"),
                summary=str(item.get("summary") or item.get("reason") or ""),
                data={"source": "events_replay", "event_id": str(item.get("event_id") or "")},
            )
        )
    return entry


def _entry_from_runtime_rows(task: TaskRow, runtime: RuntimeStateRow) -> StateLedgerEntry:
    blob = runtime.blob if isinstance(runtime.blob, dict) else {}
    current_action = _last_step_summary(blob)
    entry = StateLedgerEntry(
        task_id=task.task_id,
        tenant_id=task.tenant_id,
        user_id=task.user_id,
        project_id=task.project_id,
        task_type=task.task_type,
        title=task.success_criteria_short,
        current_goal=_goal_from_task(task),
        status=runtime.status,
        current_step=runtime.current_step,
        total_steps=runtime.total_planned_steps,
        current_action=current_action,
        current_risk=task.risk_level,
        complexity_score=task.complexity_score,
        execution_mode=str(blob.get("execution_mode") or "FAST"),
        budget_estimated_usd=task.estimated_cost_usd,
        cost_so_far_usd=runtime.accumulated_cost_usd_equivalent,
        tokens_so_far=runtime.accumulated_tokens,
        started_at=runtime.started_at,
        updated_at=runtime.last_updated,
        finished_at=runtime.finished_at,
        recent_events=[
            StateLedgerTrail(
                at=runtime.last_updated,
                kind="state.hydrated",
                summary="从持久运行状态恢复视图",
                data={"source": "runtime_states"},
            )
        ],
    )
    return entry


def _goal_from_task(task: TaskRow) -> str:
    spec = task.spec_json if isinstance(task.spec_json, dict) else {}
    for key in ("goal_detail", "goal", "objective"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return task.success_criteria_short


def _last_step_summary(blob: dict[str, Any]) -> str:
    steps = blob.get("completed_steps")
    if not isinstance(steps, list) or not steps:
        return ""
    last = steps[-1]
    if not isinstance(last, dict):
        return ""
    for key in ("description", "summary", "skill_used"):
        value = last.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _state_ledger_history_item_from_event(row: EventRow) -> dict[str, Any]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    ticket = _decision_ticket_payload(payload)
    reason = _event_reason(payload, ticket)
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "task_id": row.task_ref,
        "summary": row.subject[:200],
        "reason": reason,
        "cost_usd": _event_cost(str(row.event_type), payload, ticket),
        "decision_ticket_id": _optional_str(ticket.get("ticket_id")),
        "decision_point": str(ticket.get("decision_point") or ""),
        "phase": str(ticket.get("phase") or ""),
        "selected_action": str(ticket.get("selected_action") or ""),
        "decision_status": str(ticket.get("status") or ""),
        "payload": payload,
    }


def _decision_ticket_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("decision_ticket")
    if isinstance(nested, dict):
        return nested
    if payload.get("ticket_id") and payload.get("decision_point"):
        return payload
    return {}


def _event_reason(payload: dict[str, Any], ticket: dict[str, Any]) -> str:
    for value in (
        ticket.get("reason"),
        payload.get("reason"),
        payload.get("message"),
        payload.get("reason_summary"),
        payload.get("error"),
    ):
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _event_cost(
    _event_type: str,
    payload: dict[str, Any],
    ticket: dict[str, Any] | None = None,
) -> float:
    # Running totals such as `accumulated_cost_usd` double-count in replay.
    # Only per-event deltas or actual costs are safe to add across history.
    for key in ("cost_delta_usd", "cost_usd", "cost_usd_actual"):
        value = payload.get(key)
        try:
            if value is not None:
                return round(float(value), 6)
        except (TypeError, ValueError):
            continue
    if ticket:
        for source in (ticket, ticket.get("metadata"), ticket.get("evidence")):
            if not isinstance(source, dict):
                continue
            for key in ("cost_delta_usd", "cost_usd", "cost_usd_actual"):
                value = source.get(key)
                try:
                    if value is not None:
                        return round(float(value), 6)
                except (TypeError, ValueError):
                    continue
    return 0.0


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return round(float(value or 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _matches_user(entry_user_id: str | None, requested_user_id: str | None) -> bool:
    return (
        not requested_user_id or requested_user_id == "u-anon" or entry_user_id == requested_user_id
    )


def _event_task_pair(raw: Any) -> tuple[EventRow | None, TaskRow | None]:
    if isinstance(raw, EventRow):
        return raw, None
    if isinstance(raw, tuple):
        event = raw[0] if raw and isinstance(raw[0], EventRow) else None
        task = raw[1] if len(raw) > 1 and isinstance(raw[1], TaskRow) else None
        return event, task
    try:
        event = raw[0] if isinstance(raw[0], EventRow) else None
        task = raw[1] if isinstance(raw[1], TaskRow) else None
        return event, task
    except (IndexError, KeyError, TypeError):
        pass
    event = getattr(raw, "EventRow", None)
    task = getattr(raw, "TaskRow", None)
    return (
        event if isinstance(event, EventRow) else None,
        task if isinstance(task, TaskRow) else None,
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _workspace_source(
    *,
    task_id: str,
    user_id: str,
    **_: Any,
) -> dict[str, Any] | None:
    """共享工作区. M3.2 简版: 返 RuntimeState completed_steps 作为 artifacts."""
    return await _workspace_source_async(task_id)


async def _workspace_source_async(task_id: str) -> dict[str, Any] | None:
    try:
        async with session_scope() as session:
            stmt = select(RuntimeStateRow).where(RuntimeStateRow.task_ref == task_id).limit(1)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            # completed_steps 存在 blob JSON 字段里
            blob = row.blob or {}
            steps = blob.get("completed_steps", []) if isinstance(blob, dict) else []
            return {
                "task_id": task_id,
                "artifacts": [
                    {
                        "step_id": s.get("step_id"),
                        "skill_used": s.get("skill_used"),
                        "output_ref": s.get("output_ref"),
                        "cost_usd": s.get("cost_usd", 0),
                    }
                    for s in steps
                ],
                "handoff_packets": [],  # M4 接交接协议 L1-L4
                "last_update": (
                    row.last_updated or row.started_at or datetime.now(UTC)
                ).isoformat(),
            }
    except Exception:
        logger.exception("blackboard.workspace_source failed")
        return None


async def _assets_source(
    *,
    task_id: str,
    user_id: str,
    **_: Any,
) -> dict[str, Any]:
    """资产池活跃切片.

    V2.2 M4 wire: 拉该 tenant 下最近活跃的 top assets, 按 kind 分组.
    严格"task 关联"待 M5 加 task_assets 关联表.
    """
    return await _assets_source_async(task_id, user_id)


async def _assets_source_async(task_id: str, user_id: str) -> dict[str, Any]:
    """V2.2 wire: 从 AttentionAnchor 拉用户 pin + AssetStore 拉 active 资产分组."""
    pinned: list[str] = []
    semantic: list[str] = []
    methodology: list[str] = []
    capability_refs: list[str] = []

    try:
        from kun.context.storage import get_store
        from kun.core.attention_anchor import get_manager
        from kun.core.tenancy import current_tenant

        # 1. 用户 pin (AttentionAnchor)
        try:
            mgr = get_manager()
            for anchor in mgr.list_for_user(user_id=user_id):
                if anchor.target_asset_ref:
                    pinned.append(anchor.target_asset_ref)
        except Exception:
            logger.debug("attention_anchor list failed (non-fatal)")

        # 2. AssetStore 拉 active 资产 (按 kind 分组, top 5/类)
        tenant_id = current_tenant().tenant_id

        store = get_store()
        # capability_card 不在 AssetStore 的合法 kind 集合里 (单独 ORM 表), 跳过 store 拉
        # 让 capability_refs 留空, 后续接 capability_card_router 提供 (M5)
        kind_targets: list[tuple[str, list[str]]] = [
            ("memory", semantic),
            ("knowledge", semantic),
            ("methodology", methodology),
        ]
        for kind, target_list in kind_targets:
            try:
                assets = await store.list(
                    tenant_id=tenant_id,
                    asset_kind=cast(Any, kind),
                    limit=5,
                )
                for asset in assets:
                    target_list.append(asset.asset_id)
            except Exception:
                logger.debug("asset_store.list kind=%s failed (non-fatal)", kind)
    except Exception:
        logger.exception("blackboard.assets_source failed")

    return {
        "task_id": task_id,
        "pinned_assets": pinned[:10],
        "semantic_assets": semantic[:10],
        "methodology_refs": methodology[:10],
        "capability_card_refs": capability_refs[:10],
    }


__all__ = ["install_blackboard_data_sources"]
