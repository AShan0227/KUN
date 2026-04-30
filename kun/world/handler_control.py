"""Persistent NUO controls for WorldGateway handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.orm import WorldHandlerControlRow

WorldHandlerControlStatus = Literal["enabled", "quarantined", "disabled"]


class WorldHandlerControl(BaseModel):
    """Tenant-scoped control state consumed by handler health and execution."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    action_type: str
    status: WorldHandlerControlStatus = "enabled"
    reason: str = ""
    source: str = "nuo"
    updated_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


async def load_world_handler_controls(
    session: AsyncSession,
    *,
    tenant_id: str,
) -> dict[str, WorldHandlerControl]:
    result = await session.execute(
        select(WorldHandlerControlRow).where(WorldHandlerControlRow.tenant_id == tenant_id)
    )
    rows = list(result.scalars().all())
    return {row.action_type: _row_to_control(row) for row in rows}


async def set_world_handler_control(
    session: AsyncSession,
    *,
    tenant_id: str,
    action_type: str,
    status: WorldHandlerControlStatus,
    reason: str = "",
    source: str = "nuo",
    updated_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> WorldHandlerControl:
    cleaned_action = action_type.strip()
    if not cleaned_action:
        raise ValueError("action_type is required")
    now = datetime.now(UTC)
    values = {
        "tenant_id": tenant_id,
        "action_type": cleaned_action,
        "status": status,
        "reason": reason.strip(),
        "source": source.strip() or "nuo",
        "updated_by": updated_by,
        "metadata_json": metadata or {},
        "updated_at": now,
    }
    stmt = (
        pg_insert(WorldHandlerControlRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                WorldHandlerControlRow.tenant_id,
                WorldHandlerControlRow.action_type,
            ],
            set_={key: value for key, value in values.items() if key != "created_at"},
        )
        .returning(WorldHandlerControlRow)
    )
    result = await session.execute(stmt)
    row = result.scalar_one()
    return _row_to_control(row)


def _row_to_control(row: WorldHandlerControlRow) -> WorldHandlerControl:
    return WorldHandlerControl(
        tenant_id=row.tenant_id,
        action_type=row.action_type,
        status=_coerce_status(row.status),
        reason=row.reason,
        source=row.source,
        updated_by=row.updated_by,
        metadata=row.metadata_json,
        updated_at=row.updated_at,
    )


def _coerce_status(value: str) -> WorldHandlerControlStatus:
    if value == "quarantined":
        return "quarantined"
    if value == "disabled":
        return "disabled"
    return "enabled"


__all__ = [
    "WorldHandlerControl",
    "WorldHandlerControlStatus",
    "load_world_handler_controls",
    "set_world_handler_control",
]
