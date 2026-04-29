"""Blackboard data sources — wire 黑板 5 endpoint 到 events / TaskRow / RuntimeStateRow.

V2.1 wire (W5): 把 blackboard.py 的 register_data_source hook 接到真实数据库.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc, or_, select

from kun.api.blackboard import register_data_source
from kun.core.db import session_scope
from kun.core.orm import EventRow, RuntimeStateRow, TaskRow
from kun.core.state_ledger import (
    StateLedgerEntry,
    StateLedgerHistory,
    StateLedgerHistoryEvent,
    StateLedgerTrail,
    get_state_ledger,
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
    ledger_entries: list[dict[str, Any]] = []
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(RuntimeStateRow.status, RuntimeStateRow.accumulated_cost_usd_actual)
                .join(TaskRow, TaskRow.task_id == RuntimeStateRow.task_ref)
                .where(TaskRow.tenant_id == tenant_id)
                .order_by(desc(TaskRow.created_at))
                .limit(200)
            )
            for status, cost in (await session.execute(stmt)).all():
                if status == "running":
                    running += 1
                elif status == "queued":
                    queued += 1
                cost_today += float(cost or 0.0)
    except Exception:
        logger.exception("blackboard.state_source failed")
    ledger_data = await _state_ledger_source_async(tenant_id=tenant_id, task_id=None)
    if isinstance(ledger_data, list):
        ledger_entries = ledger_data

    health: str = "healthy"
    if running > 10:
        health = "warn"
    if cost_today > 100.0:
        health = "warn"

    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task_count_running": running,
        "task_count_queued": queued,
        "total_cost_today_usd": round(cost_today, 4),
        "total_cost_remaining_budget_usd": 0.0,  # M3.3 接 BudgetTracker
        "health_indicator": health,
        "urgent_alert_count": 0,  # M3.3 接 IncidentResponseEngine.history
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
    return await _state_ledger_source_async(tenant_id=tenant_id, task_id=task_id)


async def _state_ledger_source_async(
    *,
    tenant_id: str,
    task_id: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any] | None:
    try:
        ledger = get_state_ledger()
        if task_id is not None:
            entry = ledger.snapshot(task_id)
            if entry is None or entry.tenant_id != tenant_id:
                return await _runtime_state_ledger_source(tenant_id=tenant_id, task_id=task_id)
            return entry.model_dump(mode="json")
        hot_entries = [
            entry.model_dump(mode="json") for entry in ledger.active_snapshots(tenant_id=tenant_id)
        ]
        if hot_entries:
            return hot_entries
        fallback = await _runtime_state_ledger_source(tenant_id=tenant_id, task_id=None)
        return fallback if isinstance(fallback, list) else []
    except Exception:
        logger.exception("blackboard.state_ledger_source failed")
        return None if task_id is not None else []


async def _state_ledger_history_source(
    *,
    tenant_id: str,
    user_id: str,
    task_id: str | None = None,
    mission_id: str | None = None,
    limit: int = 100,
    **_: Any,
) -> dict[str, Any]:
    return (
        await _state_ledger_history_source_async(
            tenant_id=tenant_id,
            task_id=task_id,
            mission_id=mission_id,
            limit=limit,
        )
    ).model_dump(mode="json")


async def _state_ledger_history_source_async(
    *,
    tenant_id: str,
    task_id: str | None = None,
    mission_id: str | None = None,
    limit: int = 100,
) -> StateLedgerHistory:
    """Replay a durable ledger slice from append-only EventRow records."""

    rows: list[EventRow] = []
    try:
        async with session_scope(tenant_id=tenant_id) as session:
            stmt = (
                select(EventRow)
                .where(EventRow.tenant_id == tenant_id)
                .order_by(desc(EventRow.occurred_at))
                .limit(limit)
            )
            if task_id is not None:
                stmt = stmt.where(
                    or_(
                        EventRow.task_ref == task_id,
                        EventRow.payload.op("->>")("task_id") == task_id,
                        EventRow.payload.op("->>")("task_ref") == task_id,
                    )
                )
            if mission_id is not None:
                stmt = stmt.where(EventRow.payload.op("->>")("mission_id") == mission_id)
            rows = list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.exception("blackboard.state_ledger_history_source failed")
    return _history_from_event_rows(
        tenant_id=tenant_id,
        rows=rows,
        task_id=task_id,
        mission_id=mission_id,
    )


async def _runtime_state_ledger_source(
    *,
    tenant_id: str,
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
        rows = (await session.execute(stmt)).all()

    entries = [
        _entry_from_runtime_rows(task, runtime).model_dump(mode="json") for task, runtime in rows
    ]
    if task_id is not None:
        return entries[0] if entries else None
    return entries


def _history_from_event_rows(
    *,
    tenant_id: str,
    rows: list[EventRow],
    task_id: str | None = None,
    mission_id: str | None = None,
) -> StateLedgerHistory:
    events: list[StateLedgerHistoryEvent] = []
    status_counts: Counter[str] = Counter()
    recent_reasons: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    total_actual = 0.0
    total_equivalent = 0.0

    for row in sorted(rows, key=lambda item: item.occurred_at):
        payload = dict(row.payload or {})
        event_task_id = _first_str(payload, "task_id", "task_ref") or row.task_ref
        event_mission_id = _first_str(payload, "mission_id")
        status = _first_str(payload, "status", "final_status", "runtime_status")
        reason = _first_str(payload, "reason", "decision_reason", "blocked_by")
        cost_actual = _first_float(payload, "cost_usd_actual", "cost_actual_usd")
        cost_equivalent = _first_float(payload, "cost_usd_equivalent", "cost_equivalent_usd")
        model = _first_str(payload, "model", "current_model", "selected_model")
        skill = _first_str(payload, "skill", "skill_used", "current_skill")
        checkpoint = _checkpoint_from_payload(payload)
        if status:
            status_counts[status] += 1
        if reason:
            recent_reasons.append(reason)
            recent_reasons = recent_reasons[-10:]
        if checkpoint:
            checkpoints.append(
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "occurred_at": row.occurred_at.isoformat(),
                    "checkpoint": checkpoint,
                }
            )
            checkpoints = checkpoints[-20:]
        total_actual += cost_actual
        total_equivalent += cost_equivalent
        events.append(
            StateLedgerHistoryEvent(
                event_id=row.event_id,
                event_type=row.event_type,
                subject=row.subject,
                occurred_at=row.occurred_at,
                task_id=event_task_id,
                mission_id=event_mission_id,
                status=status,
                reason=reason,
                cost_usd_actual=cost_actual,
                cost_usd_equivalent=cost_equivalent,
                model=model,
                skill=skill,
                checkpoint=checkpoint,
                payload=payload,
            )
        )

    return StateLedgerHistory(
        tenant_id=tenant_id,
        task_id=task_id,
        mission_id=mission_id,
        event_count=len(events),
        first_event_at=events[0].occurred_at if events else None,
        last_event_at=events[-1].occurred_at if events else None,
        total_cost_usd_actual=round(total_actual, 6),
        total_cost_usd_equivalent=round(total_equivalent, 6),
        status_counts=dict(status_counts),
        recent_reasons=recent_reasons,
        checkpoints=checkpoints,
        events=events,
    )


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


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_key in ("outcome", "result", "usage", "meta"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            value = _first_str(nested, *keys)
            if value:
                return value
    return None


def _first_float(payload: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    for container_key in ("outcome", "result", "usage", "cost"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            value = _first_float(nested, *keys)
            if value:
                return value
    return 0.0


def _checkpoint_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("checkpoint", "checkpoint_json", "last_blocked", "last_reaper"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    for container_key in ("outcome", "result", "runtime", "mission_reaper", "mission_blocked"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            checkpoint = _checkpoint_from_payload(nested)
            if checkpoint:
                return checkpoint
    return {}


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
        try:
            tenant_id = current_tenant().tenant_id
        except Exception:
            tenant_id = "u-sylvan"

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
