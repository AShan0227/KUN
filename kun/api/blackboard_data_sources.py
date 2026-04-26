"""Blackboard data sources — wire 黑板 5 endpoint 到 events / TaskRow / RuntimeStateRow.

V2.1 wire (W5): 把 blackboard.py 的 register_data_source hook 接到真实数据库.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc, select

from kun.api.blackboard import register_data_source
from kun.core.db import session_scope
from kun.core.orm import EventRow, RuntimeStateRow, TaskRow

logger = logging.getLogger(__name__)


def install_blackboard_data_sources() -> None:
    """注册黑板 5 endpoint 的数据源 hook.

    应在 lifespan startup 调用一次.
    """
    register_data_source("tasks", _tasks_source)
    register_data_source("events", _events_source)
    register_data_source("state", _state_source)
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
        "last_update": datetime.now(UTC).isoformat(),
    }


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
