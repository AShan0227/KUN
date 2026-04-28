"""ProtocolRegistry HTTP API (V2.3 Wire 40).

This exposes KUN's protocol registry as a tenant-scoped control surface. The
registry remains in-memory by default, and ``install_runtime`` can swap in the
SQL-backed registry when explicitly enabled.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant
from kun.qi import Protocol, ProtocolRegistry, ProtocolStatus, get_protocol_registry

router = APIRouter(prefix="/api/protocols", tags=["protocols"])


class ProtocolSaveResponse(BaseModel):
    protocol_id: str
    version: str
    status: ProtocolStatus
    message: str = "saved"


class ProtocolPromoteRequest(BaseModel):
    target_status: Literal["shadow", "canary", "stable", "rolled_back"]


class ProtocolRollbackRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


class ProtocolMatchRequest(BaseModel):
    task_meta: dict[str, Any] = Field(default_factory=dict)


def _registry(request: Request) -> ProtocolRegistry:
    return getattr(request.app.state, "protocol_registry", get_protocol_registry())


def _tenant_id() -> str:
    return current_tenant().tenant_id


@router.get("", response_model=list[Protocol])
async def list_protocols(request: Request) -> list[Protocol]:
    """List all protocols visible to the current tenant."""

    return await _registry(request).list_all(_tenant_id())


@router.post("", response_model=ProtocolSaveResponse)
async def save_protocol(protocol: Protocol, request: Request) -> ProtocolSaveResponse:
    """Create or replace a protocol version for the current tenant."""

    tenant_id = _tenant_id()
    protocol = protocol.model_copy(update={"tenant_id": tenant_id})
    await _registry(request).save(protocol)
    return ProtocolSaveResponse(
        protocol_id=protocol.protocol_id,
        version=protocol.version,
        status=protocol.status,
    )


@router.get("/{protocol_id}/versions/{version}", response_model=Protocol)
async def get_protocol(protocol_id: str, version: str, request: Request) -> Protocol:
    """Fetch a specific protocol version."""

    protocol = await _registry(request).get(_tenant_id(), protocol_id, version)
    if protocol is None:
        raise HTTPException(status_code=404, detail="protocol not found")
    return protocol


@router.post("/{protocol_id}/versions/{version}/promote", response_model=ProtocolSaveResponse)
async def promote_protocol(
    protocol_id: str,
    version: str,
    req: ProtocolPromoteRequest,
    request: Request,
) -> ProtocolSaveResponse:
    """Move a protocol through experimental -> shadow -> canary -> stable."""

    try:
        await _registry(request).promote(_tenant_id(), protocol_id, version, req.target_status)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProtocolSaveResponse(
        protocol_id=protocol_id,
        version=version,
        status=req.target_status,
        message="promoted",
    )


@router.post("/{protocol_id}/versions/{version}/rollback", response_model=ProtocolSaveResponse)
async def rollback_protocol(
    protocol_id: str,
    version: str,
    req: ProtocolRollbackRequest,
    request: Request,
) -> ProtocolSaveResponse:
    """Rollback a protocol version to ``rolled_back``."""

    await _registry(request).rollback(_tenant_id(), protocol_id, version, reason=req.reason)
    return ProtocolSaveResponse(
        protocol_id=protocol_id,
        version=version,
        status="rolled_back",
        message="rolled_back",
    )


@router.post("/match", response_model=Protocol | None)
async def match_protocol(req: ProtocolMatchRequest, request: Request) -> Protocol | None:
    """Find the most specific stable protocol for task metadata."""

    return await _registry(request).find_protocol_for(req.task_meta, _tenant_id())


__all__ = ["router"]
