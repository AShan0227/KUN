"""傩 · 系统健康面板."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from kun.core.db import session_scope
from kun.core.orm import EventRow, TaskRow
from kun.core.tenancy import current_tenant

router = APIRouter()


@router.get("/summary")
async def health_summary() -> dict:
    """High-level system health snapshot."""
    tenant = current_tenant()
    async with session_scope() as s:
        # Task counts by status via RuntimeState blob status column
        from kun.core.orm import RuntimeStateRow

        stmt = (
            select(RuntimeStateRow.status, func.count())
            .where(RuntimeStateRow.tenant_id == tenant.tenant_id)
            .group_by(RuntimeStateRow.status)
        )
        rows = (await s.execute(stmt)).all()
        task_by_status = {status: count for status, count in rows}

        # Outbox lag
        lag_stmt = select(func.count()).select_from(EventRow).where(EventRow.published_at.is_(None))
        lag = (await s.execute(lag_stmt)).scalar_one()

        total_tasks_stmt = (
            select(func.count()).select_from(TaskRow).where(TaskRow.tenant_id == tenant.tenant_id)
        )
        total_tasks = (await s.execute(total_tasks_stmt)).scalar_one()

    return {
        "tenant_id": tenant.tenant_id,
        "total_tasks": int(total_tasks),
        "tasks_by_status": task_by_status,
        "events_outbox_lag": int(lag),
    }
