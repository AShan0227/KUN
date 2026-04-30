"""傩 · 系统健康面板."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from kun.context.maintenance import ContextMaintenanceReport, run_context_maintenance
from kun.core.db import session_scope
from kun.core.orm import EventRow, PendingActionRow, TaskRow
from kun.core.tenancy import current_tenant
from kun.engineering.delivery_status import (
    delivery_status_summary,
    get_v3_delivery_status,
    validate_delivery_status,
)
from kun.engineering.nuo_system_health import collect_system_health_report

router = APIRouter()


@router.get("/summary")
async def health_summary() -> dict[str, Any]:
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
        task_by_status: dict[str, int] = {str(status): int(count) for status, count in rows}

        # Outbox lag
        lag_stmt = (
            select(func.count())
            .select_from(EventRow)
            .where(
                EventRow.tenant_id == tenant.tenant_id,
                EventRow.published_at.is_(None),
            )
        )
        lag = (await s.execute(lag_stmt)).scalar_one()

        total_tasks_stmt = (
            select(func.count()).select_from(TaskRow).where(TaskRow.tenant_id == tenant.tenant_id)
        )
        total_tasks = (await s.execute(total_tasks_stmt)).scalar_one()

        pending_actions_stmt = (
            select(func.count())
            .select_from(PendingActionRow)
            .where(
                PendingActionRow.tenant_id == tenant.tenant_id,
                PendingActionRow.status == "pending_approval",
            )
        )
        pending_actions = (await s.execute(pending_actions_stmt)).scalar_one()

    return {
        "tenant_id": tenant.tenant_id,
        "total_tasks": int(total_tasks),
        "tasks_by_status": task_by_status,
        "events_outbox_lag": int(lag),
        "pending_actions": int(pending_actions),
        "delivery_status": delivery_status_summary(),
        "delivery_status_issues": validate_delivery_status(),
    }


@router.get("/delivery-status")
async def delivery_status() -> dict[str, Any]:
    """Honest list of what KUN can and cannot claim today."""
    return {
        "items": [item.model_dump(mode="json") for item in get_v3_delivery_status()],
        "summary": delivery_status_summary(),
        "validation_issues": validate_delivery_status(),
    }


@router.get("/report")
async def system_health_report() -> dict[str, Any]:
    """Deep NUO report: real subsystem health, not just a dashboard count."""
    tenant = current_tenant()
    report = await collect_system_health_report(tenant_id=tenant.tenant_id)
    return report.model_dump(mode="json")


@router.post("/context-maintenance/run", response_model=ContextMaintenanceReport)
async def run_context_maintenance_once(
    dry_run: bool = Query(default=True),
    max_assets: int = Query(default=500, ge=1, le=5000),
) -> ContextMaintenanceReport:
    """Run NUO context/memory slimming once.

    Default is dry-run so the user can see what NUO would compress/forget before
    allowing real mutation.
    """
    tenant = current_tenant()
    return await run_context_maintenance(
        tenant_id=tenant.tenant_id,
        dry_run=dry_run,
        max_assets=max_assets,
    )
