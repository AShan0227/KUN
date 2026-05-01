"""傩 · 系统健康面板."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from kun.context.maintenance import ContextMaintenanceReport, run_context_maintenance
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.orm import EventRow, PendingActionRow, TaskRow
from kun.core.tenancy import current_tenant, has_scope
from kun.engineering.credit_assignment import (
    ResourceCreditSummary,
    load_top_resource_credit,
)
from kun.engineering.delivery_status import (
    delivery_status_summary,
    get_v3_delivery_status,
    validate_delivery_status,
)
from kun.engineering.nuo_system_health import (
    GovernanceRecommendationApplyResult,
    apply_governance_recommendation,
    collect_system_health_report,
)
from kun.ops.readiness import ReadinessReport, run_readiness_report
from kun.ops.secret_audit import SecretAuditReport, audit_runtime_secrets
from kun.ops.secret_store import (
    SECRET_STORE_FILE_ENV,
    SecretStoreWriteResult,
    upsert_secret_store_value,
)
from kun.world.gateway import WorldGateway, set_world_gateway
from kun.world.handler_health import EXPECTED_REAL_WORLD_HANDLERS

router = APIRouter()


class SecretStoreSetRequest(BaseModel):
    """Safe NUO secret-store write request.

    The value is accepted in the request body but never returned by the API.
    This endpoint intentionally only supports KUN_WORLD_* keys because it is
    for external-action handler credentials, not general auth or database
    secrets.
    """

    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=8192)
    scope: str = Field(default="tenant", pattern="^(tenant|global)$")


class SecretStoreSetResponse(BaseModel):
    path: str
    scope: str
    tenant_id: str = ""
    name: str
    tenant_count: int = 0
    global_key_count: int = 0
    honest_limits: list[str]
    message: str


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


@router.get("/readiness", response_model=ReadinessReport)
async def readiness_report(
    include_dogfood: bool = Query(default=False),
    include_db_mission: bool = Query(default=False),
    include_db_account: bool = Query(default=False),
    include_db_state_ledger_repair: bool = Query(default=False),
    run_alembic_heads: bool = Query(default=False),
    include_db_long_horizon_drill: bool = Query(default=False),
) -> ReadinessReport:
    """NUO API view of the formal testing readiness report.

    Defaults are intentionally light for UI usage. Operators can opt into
    dogfood/database/alembic checks when they want a stricter release gate.
    """
    tenant = current_tenant()
    return await run_readiness_report(
        tenant_id=tenant.tenant_id,
        include_dogfood=include_dogfood,
        include_db_mission=include_db_mission,
        include_db_account=include_db_account,
        include_db_state_ledger_repair=include_db_state_ledger_repair,
        include_db_long_horizon_drill=include_db_long_horizon_drill,
        run_alembic_heads=run_alembic_heads,
    )


@router.get("/report")
async def system_health_report() -> dict[str, Any]:
    """Deep NUO report: real subsystem health, not just a dashboard count."""
    tenant = current_tenant()
    report = await collect_system_health_report(tenant_id=tenant.tenant_id)
    return report.model_dump(mode="json")


@router.post("/governance/apply", response_model=GovernanceRecommendationApplyResult)
async def apply_governance_recommendation_once(
    recommendation_id: str = Query(min_length=1, max_length=256),
    dry_run: bool = Query(default=True),
    max_assets: int = Query(default=500, ge=1, le=5000),
) -> GovernanceRecommendationApplyResult:
    """Explicitly dry-run/apply one current NUO governance recommendation."""
    tenant = current_tenant()
    result = await apply_governance_recommendation(
        tenant_id=tenant.tenant_id,
        recommendation_id=recommendation_id,
        dry_run=dry_run,
        max_assets=max_assets,
    )
    if result.blocked_reason == "recommendation_not_found":
        raise HTTPException(status_code=404, detail=result.model_dump(mode="json"))
    return result


@router.get("/secret-audit", response_model=SecretAuditReport)
async def secret_audit() -> SecretAuditReport:
    """NUO view of unsafe defaults, missing secrets and half-enabled handlers."""
    return audit_runtime_secrets()


@router.post("/secret-store/set", response_model=SecretStoreSetResponse)
async def set_secret_store_value(req: SecretStoreSetRequest) -> SecretStoreSetResponse:
    """Write one WorldGateway secret into the configured local secret store.

    This is a deliberately narrow bridge for dogfood and self-hosted setups:
    - requires a configured KUN_SECRET_STORE_FILE;
    - only writes KUN_WORLD_* keys;
    - does not echo the value back;
    - production requires a world:dispatch/account:admin-style operator scope.
    """
    tenant = current_tenant()
    _require_secret_write_scope()
    raw_path = os.getenv(SECRET_STORE_FILE_ENV, "").strip()
    if not raw_path:
        raise HTTPException(
            status_code=400,
            detail=(
                "KUN_SECRET_STORE_FILE is not configured; set it before writing "
                "WorldGateway secrets through NUO."
            ),
        )
    if req.scope == "tenant" and _is_global_world_enable_flag(req.name):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{req.name} is a global WorldGateway handler switch. "
                "Use scope=global; tenant scope is only for credentials and allowlists."
            ),
        )
    try:
        result = upsert_secret_store_value(
            path=Path(raw_path),
            tenant_id=tenant.tenant_id if req.scope == "tenant" else "",
            name=req.name,
            value=req.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    set_world_gateway(WorldGateway())
    return _secret_write_response(result)


@router.get("/resource-credit", response_model=list[ResourceCreditSummary])
async def resource_credit_report(
    kind: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ResourceCreditSummary]:
    """Top resources that actually earned durable MoE credit.

    This is NUO's "which strategy pieces are really helping" view.  It covers
    memory, skill, model, role_template, world_action, decision_ticket and other
    resource kinds once they are written into ``resource_credit_stats``.
    """
    tenant = current_tenant()
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        return await load_top_resource_credit(
            s,
            tenant_id=tenant.tenant_id,
            resource_kind=kind,
            limit=limit,
        )


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


def _require_secret_write_scope() -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    if has_scope("world:dispatch", ctx=tenant) or has_scope("account:admin", ctx=tenant):
        return
    raise HTTPException(status_code=403, detail="world:dispatch or account:admin scope required")


def _is_global_world_enable_flag(name: str) -> bool:
    return name in {enable for enable, _required in EXPECTED_REAL_WORLD_HANDLERS.values()}


def _secret_write_response(result: SecretStoreWriteResult) -> SecretStoreSetResponse:
    return SecretStoreSetResponse(
        path=result.path,
        scope=result.scope,
        tenant_id=result.tenant_id,
        name=result.name,
        tenant_count=result.tenant_count,
        global_key_count=result.global_key_count,
        honest_limits=list(result.honest_limits),
        message=(
            "Secret store updated. Value is hidden. This is a local JSON bridge, "
            "not cloud KMS or automatic rotation. WorldGateway registry was refreshed."
        ),
    )
