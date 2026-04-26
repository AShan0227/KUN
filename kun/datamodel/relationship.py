"""Knowledge graph relationship models and DB provider functions."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from ulid import ULID

type RelationType = Literal[
    "depends_on",
    "mentions",
    "verifies",
    "contradicts",
    "similar_to",
    "co_occurs",
    "produced_by",
    "transfer_confidence",
]
type EntityKey = tuple[str, str]


def _new_relation_id() -> str:
    return f"rel-{ULID()}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def confidence_for_evidence(evidence_count: int, current: float = 0.3) -> float:
    """Tier mined relationship confidence by evidence volume."""
    if evidence_count >= 10:
        return max(current, 0.9)
    if evidence_count >= 3:
        return max(current, 0.7)
    return max(current, 0.3)


class EntityRelationship(BaseModel):
    """A directed relationship edge in the tenant-scoped knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    relation_id: str = Field(default_factory=_new_relation_id)
    tenant_id: str
    source_entity_kind: str
    source_entity_id: str
    target_entity_kind: str
    target_entity_id: str
    relation_type: RelationType
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    evidence_count: int = Field(default=1, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    last_reinforced_at: datetime = Field(default_factory=_utcnow)

    @property
    def source_key(self) -> EntityKey:
        return (self.source_entity_kind, self.source_entity_id)

    @property
    def target_key(self) -> EntityKey:
        return (self.target_entity_kind, self.target_entity_id)


def _row_to_relationship(row: Any) -> EntityRelationship:
    return EntityRelationship(
        relation_id=row.relation_id,
        tenant_id=row.tenant_id,
        source_entity_kind=row.source_entity_kind,
        source_entity_id=row.source_entity_id,
        target_entity_kind=row.target_entity_kind,
        target_entity_id=row.target_entity_id,
        relation_type=row.relation_type,
        confidence=row.confidence,
        evidence_count=row.evidence_count,
        metadata=row.metadata_json or {},
        created_at=row.created_at,
        last_reinforced_at=row.last_reinforced_at,
    )


def _relationship_to_row_kwargs(rel: EntityRelationship) -> dict[str, Any]:
    return {
        "relation_id": rel.relation_id,
        "tenant_id": rel.tenant_id,
        "source_entity_kind": rel.source_entity_kind,
        "source_entity_id": rel.source_entity_id,
        "target_entity_kind": rel.target_entity_kind,
        "target_entity_id": rel.target_entity_id,
        "relation_type": rel.relation_type,
        "confidence": rel.confidence,
        "evidence_count": rel.evidence_count,
        "metadata_json": rel.metadata,
        "created_at": rel.created_at,
        "last_reinforced_at": rel.last_reinforced_at,
    }


async def add_relationship(rel: EntityRelationship) -> None:
    """Insert a relationship edge."""
    from kun.core.db import session_scope
    from kun.core.orm import EntityRelationshipRow

    async with session_scope(tenant_id=rel.tenant_id) as session:
        session.add(EntityRelationshipRow(**_relationship_to_row_kwargs(rel)))


async def get_relationships_from(
    entity_kind: str,
    entity_id: str,
    tenant_id: str,
    *,
    relation_types: list[RelationType] | None = None,
    min_confidence: float = 0.5,
) -> list[EntityRelationship]:
    """Return outgoing edges for an entity, tenant filtered."""
    from kun.core.db import session_scope
    from kun.core.orm import EntityRelationshipRow

    async with session_scope(tenant_id=tenant_id) as session:
        stmt = select(EntityRelationshipRow).where(
            EntityRelationshipRow.tenant_id == tenant_id,
            EntityRelationshipRow.source_entity_kind == entity_kind,
            EntityRelationshipRow.source_entity_id == entity_id,
            EntityRelationshipRow.confidence >= min_confidence,
        )
        if relation_types:
            stmt = stmt.where(EntityRelationshipRow.relation_type.in_(relation_types))
        rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_relationship(row) for row in rows]


async def get_relationships_to(
    entity_kind: str,
    entity_id: str,
    tenant_id: str,
    *,
    relation_types: list[RelationType] | None = None,
    min_confidence: float = 0.5,
) -> list[EntityRelationship]:
    """Return incoming edges for an entity, tenant filtered."""
    from kun.core.db import session_scope
    from kun.core.orm import EntityRelationshipRow

    async with session_scope(tenant_id=tenant_id) as session:
        stmt = select(EntityRelationshipRow).where(
            EntityRelationshipRow.tenant_id == tenant_id,
            EntityRelationshipRow.target_entity_kind == entity_kind,
            EntityRelationshipRow.target_entity_id == entity_id,
            EntityRelationshipRow.confidence >= min_confidence,
        )
        if relation_types:
            stmt = stmt.where(EntityRelationshipRow.relation_type.in_(relation_types))
        rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_relationship(row) for row in rows]


async def reinforce_relationship(
    relation_id: str,
    tenant_id: str,
    *,
    evidence_delta: int = 1,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Increase evidence and confidence for an existing relationship."""
    from kun.core.db import session_scope
    from kun.core.orm import EntityRelationshipRow

    now = _utcnow()
    async with session_scope(tenant_id=tenant_id) as session:
        stmt = select(EntityRelationshipRow).where(
            EntityRelationshipRow.tenant_id == tenant_id,
            EntityRelationshipRow.relation_id == relation_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        new_count = row.evidence_count + evidence_delta
        merged_metadata = dict(row.metadata_json or {})
        if metadata:
            merged_metadata.update(metadata)
        new_confidence = confidence_for_evidence(new_count, row.confidence)
        if confidence is not None:
            new_confidence = max(new_confidence, confidence)
        await session.execute(
            update(EntityRelationshipRow)
            .where(
                EntityRelationshipRow.tenant_id == tenant_id,
                EntityRelationshipRow.relation_id == relation_id,
            )
            .values(
                evidence_count=new_count,
                confidence=min(new_confidence, 1.0),
                metadata_json=merged_metadata,
                last_reinforced_at=now,
            )
        )


async def find_path(
    source_entity: EntityKey,
    target_entity: EntityKey,
    tenant_id: str,
    *,
    max_hops: int = 3,
    relation_types: list[RelationType] | None = None,
    min_confidence: float = 0.5,
) -> list[EntityRelationship] | None:
    """Find a directed path between two entities with bounded BFS."""
    if source_entity == target_entity:
        return []
    visited: set[EntityKey] = {source_entity}
    queue: deque[tuple[EntityKey, list[EntityRelationship]]] = deque([(source_entity, [])])
    while queue:
        current, path = queue.popleft()
        if len(path) >= max_hops:
            continue
        edges = await get_relationships_from(
            current[0],
            current[1],
            tenant_id,
            relation_types=relation_types,
            min_confidence=min_confidence,
        )
        for edge in edges:
            next_key = edge.target_key
            if next_key in visited:
                continue
            next_path = [*path, edge]
            if next_key == target_entity:
                return next_path
            visited.add(next_key)
            queue.append((next_key, next_path))
    return None


__all__ = [
    "EntityKey",
    "EntityRelationship",
    "RelationType",
    "add_relationship",
    "confidence_for_evidence",
    "find_path",
    "get_relationships_from",
    "get_relationships_to",
    "reinforce_relationship",
]
