"""Knowledge graph relationship API (C38).

Small admin-facing surface for entity_relationships:
- inspect graph neighbors
- add / edit / delete relationship edges
- explore via WebSocket for node-graph UIs
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import delete, update

from kun.context.graph_traversal import GraphTraversal
from kun.core.db import session_scope
from kun.core.ids import new_id
from kun.core.orm import EntityRelationshipRow
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    current_tenant,
    resolve_tenant_id,
    tenant_scope,
)

router = APIRouter(prefix="/api/graph", tags=["graph"])

RelationType = Literal[
    "depends_on",
    "mentions",
    "verifies",
    "contradicts",
    "similar_to",
    "co_occurs",
    "produced_by",
    "transfer_confidence",
]


class RelationshipView(BaseModel):
    relation_id: str
    tenant_id: str
    source_entity_kind: str
    source_entity_id: str
    target_entity_kind: str
    target_entity_id: str
    relation_type: RelationType
    confidence: float
    evidence_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    last_reinforced_at: datetime


class NeighborView(BaseModel):
    entity_kind: str
    entity_id: str
    relation_type: str
    confidence: float
    hops: int
    score: float
    via_path: list[tuple[str, str]]


class RelationshipCreateRequest(BaseModel):
    source_entity_kind: str = Field(min_length=1, max_length=64)
    source_entity_id: str = Field(min_length=1, max_length=128)
    target_entity_kind: str = Field(min_length=1, max_length=64)
    target_entity_id: str = Field(min_length=1, max_length=128)
    relation_type: RelationType
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_count: int = Field(default=1, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationshipPatchRequest(BaseModel):
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_count: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] | None = None


def _view(row: EntityRelationshipRow) -> RelationshipView:
    return RelationshipView(
        relation_id=row.relation_id,
        tenant_id=row.tenant_id,
        source_entity_kind=row.source_entity_kind,
        source_entity_id=row.source_entity_id,
        target_entity_kind=row.target_entity_kind,
        target_entity_id=row.target_entity_id,
        relation_type=cast(RelationType, row.relation_type),
        confidence=row.confidence,
        evidence_count=row.evidence_count,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
        last_reinforced_at=row.last_reinforced_at,
    )


@router.get("/relationships", response_model=list[NeighborView])
async def list_relationship_neighbors(
    source_kind: str = Query(..., min_length=1, max_length=64),
    source_id: str = Query(..., min_length=1, max_length=128),
    hops: int = Query(1, ge=1, le=3),
    limit_per_hop: int = Query(20, ge=1, le=100),
) -> list[NeighborView]:
    neighbors = await GraphTraversal().neighbors(
        source_kind,
        source_id,
        hops=hops,
        limit_per_hop=limit_per_hop,
    )
    return [
        NeighborView(
            entity_kind=n.entity_kind,
            entity_id=n.entity_id,
            relation_type=n.relation_type,
            confidence=n.confidence,
            hops=n.hops,
            score=n.score,
            via_path=list(n.via_path),
        )
        for n in neighbors
    ]


@router.get("/relationships/{relation_id}", response_model=RelationshipView)
async def get_relationship(relation_id: str) -> RelationshipView:
    tenant_id = current_tenant().tenant_id
    async with session_scope() as session:
        row = await session.get(EntityRelationshipRow, (relation_id, tenant_id))
        if row is None:
            raise HTTPException(404, "relationship not found")
        return _view(row)


@router.post("/relationships", response_model=RelationshipView, status_code=201)
async def create_relationship(body: RelationshipCreateRequest) -> RelationshipView:
    tenant_id = current_tenant().tenant_id
    now = datetime.now(UTC)
    row = EntityRelationshipRow(
        relation_id=new_id("relationship"),
        tenant_id=tenant_id,
        source_entity_kind=body.source_entity_kind,
        source_entity_id=body.source_entity_id,
        target_entity_kind=body.target_entity_kind,
        target_entity_id=body.target_entity_id,
        relation_type=body.relation_type,
        confidence=body.confidence,
        evidence_count=body.evidence_count,
        metadata_json=body.metadata,
        created_at=now,
        last_reinforced_at=now,
    )
    async with session_scope() as session:
        session.add(row)
        await session.flush()
        return _view(row)


@router.patch("/relationships/{relation_id}", response_model=RelationshipView)
async def patch_relationship(
    relation_id: str,
    body: RelationshipPatchRequest,
) -> RelationshipView:
    tenant_id = current_tenant().tenant_id
    values: dict[str, Any] = {"last_reinforced_at": datetime.now(UTC)}
    if body.confidence is not None:
        values["confidence"] = body.confidence
    if body.evidence_count is not None:
        values["evidence_count"] = body.evidence_count
    if body.metadata is not None:
        values["metadata_json"] = body.metadata
    if len(values) == 1:
        return await get_relationship(relation_id)

    async with session_scope() as session:
        result = await session.execute(
            update(EntityRelationshipRow)
            .where(EntityRelationshipRow.tenant_id == tenant_id)
            .where(EntityRelationshipRow.relation_id == relation_id)
            .values(**values)
            .returning(EntityRelationshipRow)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "relationship not found")
        return _view(row)


@router.delete("/relationships/{relation_id}", status_code=204)
async def delete_relationship(relation_id: str) -> None:
    tenant_id = current_tenant().tenant_id
    async with session_scope() as session:
        result = await session.execute(
            delete(EntityRelationshipRow)
            .where(EntityRelationshipRow.tenant_id == tenant_id)
            .where(EntityRelationshipRow.relation_id == relation_id)
            .returning(EntityRelationshipRow.relation_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(404, "relationship not found")


@router.websocket("/explore")
async def graph_explore(ws: WebSocket) -> None:
    try:
        tenant_id = resolve_tenant_id(ws.query_params.get("tenant_id"))
    except MissingTenantContextError:
        await ws.close(code=1008, reason="tenant_id required")
        return

    await ws.accept()
    user_id = ws.query_params.get("user_id")
    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id)
    try:
        with tenant_scope(ctx):
            while True:
                msg = await ws.receive_json()
                source_kind = str(msg.get("source_kind") or msg.get("kind") or "")
                source_id = str(msg.get("source_id") or msg.get("entity_id") or "")
                if not source_kind or not source_id:
                    await ws.send_json(
                        {"type": "error", "message": "source_kind and source_id required"}
                    )
                    continue
                hops = int(msg.get("hops") or 1)
                neighbors = await list_relationship_neighbors(
                    source_kind=source_kind,
                    source_id=source_id,
                    hops=max(1, min(3, hops)),
                )
                await ws.send_json(
                    {
                        "type": "graph_neighbors",
                        "source": {"kind": source_kind, "id": source_id},
                        "neighbors": [n.model_dump(mode="json") for n in neighbors],
                    }
                )
    except WebSocketDisconnect:
        return
