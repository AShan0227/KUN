"""傩 · 成本 + 预算面板 (ADR-008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.orm import RuntimeStateRow
from kun.core.tenancy import current_tenant

router = APIRouter()


@router.get("/summary")
async def budget_summary() -> dict[str, Any]:
    """Cost tally (equivalent vs actual — ADR-008)."""
    tenant = current_tenant()
    cfg = settings()
    now = datetime.now(UTC)
    day_start = now - timedelta(days=1)
    month_start = now - timedelta(days=30)

    async with session_scope() as s:
        stmt = (
            select(
                func.coalesce(func.sum(RuntimeStateRow.accumulated_cost_usd_actual), 0.0),
                func.coalesce(func.sum(RuntimeStateRow.accumulated_cost_usd_equivalent), 0.0),
            )
            .where(RuntimeStateRow.tenant_id == tenant.tenant_id)
            .where(RuntimeStateRow.started_at >= day_start)
        )
        day_actual, day_equiv = (await s.execute(stmt)).one()

        stmt2 = (
            select(
                func.coalesce(func.sum(RuntimeStateRow.accumulated_cost_usd_actual), 0.0),
                func.coalesce(func.sum(RuntimeStateRow.accumulated_cost_usd_equivalent), 0.0),
            )
            .where(RuntimeStateRow.tenant_id == tenant.tenant_id)
            .where(RuntimeStateRow.started_at >= month_start)
        )
        month_actual, month_equiv = (await s.execute(stmt2)).one()

    return {
        "tenant_id": tenant.tenant_id,
        "budget_daily_usd": cfg.budget_daily_usd,
        "budget_monthly_usd": cfg.budget_monthly_usd,
        "day_actual_usd": float(day_actual or 0),
        "day_equivalent_usd": float(day_equiv or 0),
        "month_actual_usd": float(month_actual or 0),
        "month_equivalent_usd": float(month_equiv or 0),
    }
