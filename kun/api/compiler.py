"""Compiler ingestion API.

This is the HTTP hot path for external/RAG material entering KUN.  It reuses
the same conservative compiler gates as the CLI: path reads need an explicit
allowed root, URL fetch needs the compiler allowlist, and rejected/placeholder
materials do not enter normal context retrieval.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kun.compiler import (
    CompilerBatchIngestor,
    CompilerBatchItem,
    CompilerBatchReport,
)
from kun.context.assets import AssetLayer
from kun.core.config import settings
from kun.core.tenancy import current_tenant, require_scope

router = APIRouter(prefix="/api/compiler", tags=["compiler"])


class CompilerHotIngestRequest(BaseModel):
    """Tenant-scoped material ingestion request."""

    model_config = ConfigDict(extra="forbid")

    layer: AssetLayer = AssetLayer.L1_TASK
    allowed_root: str | None = None
    items: list[CompilerBatchItem] = Field(default_factory=list, min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/ingest-manifest", response_model=CompilerBatchReport)
async def ingest_manifest(req: CompilerHotIngestRequest) -> CompilerBatchReport:
    """Compile and store supported material for the current tenant.

    Client-supplied item tenant IDs are ignored.  The active TenantContext is
    the single source of truth so this endpoint cannot be used to write into
    another tenant's AssetStore.
    """

    tenant = current_tenant()
    _require_scope_when_enforced("context:write")
    if any(item.type == "path" for item in req.items) and not req.allowed_root:
        raise HTTPException(status_code=422, detail="path items require allowed_root")
    sanitized_items = [item.model_copy(update={"tenant_id": None}) for item in req.items]
    return await CompilerBatchIngestor().ingest_items(
        sanitized_items,
        tenant_id=tenant.tenant_id,
        default_layer=req.layer,
        default_allowed_root=req.allowed_root,
        batch_metadata={
            "api": "compiler.ingest_manifest",
            "requested_by": tenant.user_id or tenant.tenant_id,
            **req.metadata,
        },
    )


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


__all__ = ["CompilerHotIngestRequest", "router"]
