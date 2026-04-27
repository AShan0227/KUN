"""Entity relationship provider and RelationshipMineStep tests."""

from __future__ import annotations

import operator
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.core import db as db_module
from kun.core.metrics import relationship_mine_step_throughput
from kun.core.orm import EntityRelationshipRow
from kun.datamodel.relationship import (
    EntityRelationship,
    add_relationship,
    confidence_for_evidence,
    find_path,
    get_relationships_from,
    get_relationships_to,
    reinforce_relationship,
)
from kun.engineering.precipitation import (
    KnowledgePrecipitation,
    PrecipitationEvent,
    RelationshipMineStep,
)


class _FakeScalarResult:
    def __init__(self, rows: list[EntityRelationshipRow]) -> None:
        self._rows = rows

    def all(self) -> list[EntityRelationshipRow]:
        return list(self._rows)

    def scalar_one_or_none(self) -> EntityRelationshipRow | None:
        if not self._rows:
            return None
        return self._rows[0]


class _FakeExecuteResult:
    def __init__(self, rows: list[EntityRelationshipRow]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)

    def scalar_one_or_none(self) -> EntityRelationshipRow | None:
        return _FakeScalarResult(self._rows).scalar_one_or_none()


class _RelationshipSession:
    def __init__(self) -> None:
        self.rows: list[EntityRelationshipRow] = []

    def add(self, row: EntityRelationshipRow) -> None:
        self.rows.append(row)

    async def execute(self, stmt: Any) -> _FakeExecuteResult:
        rows = [row for row in self.rows if _matches_where(row, stmt)]
        if getattr(stmt, "_values", None):
            for row in rows:
                for column, value in stmt._values.items():
                    attr = "metadata_json" if column.key == "metadata" else column.key
                    setattr(row, attr, value.value)
        return _FakeExecuteResult(rows)


def _matches_where(row: EntityRelationshipRow, stmt: Any) -> bool:
    for criterion in getattr(stmt, "_where_criteria", ()):
        key = criterion.left.key
        attr = "metadata_json" if key == "metadata" else key
        left_value = getattr(row, attr)
        right_value = getattr(criterion.right, "value", None)
        if criterion.operator is operator.eq and left_value != right_value:
            return False
        if criterion.operator is operator.ge and left_value < right_value:
            return False
        if criterion.operator.__name__ == "in_op" and left_value not in right_value:
            return False
    return True


@pytest.fixture
def relationship_session(monkeypatch: pytest.MonkeyPatch) -> _RelationshipSession:
    session = _RelationshipSession()

    @asynccontextmanager
    async def fake_session_scope(
        *,
        tenant_id: str | None = None,
        bypass_rls: bool = False,
    ) -> AsyncIterator[_RelationshipSession]:
        del tenant_id, bypass_rls
        yield session

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)
    return session


def _rel(
    source: str,
    target: str,
    *,
    tenant_id: str = "tenant-a",
    relation_type: str = "co_occurs",
    confidence: float = 0.7,
    evidence_count: int = 1,
) -> EntityRelationship:
    return EntityRelationship(
        tenant_id=tenant_id,
        source_entity_kind="skill",
        source_entity_id=source,
        target_entity_kind="task",
        target_entity_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        confidence=confidence,
        evidence_count=evidence_count,
    )


def _counter_value(counter, **labels: str) -> float:
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                return float(sample.value)
    return 0.0


def test_relationship_model_defaults_and_keys() -> None:
    rel = _rel("a", "b")

    assert rel.relation_id.startswith("rel-")
    assert rel.source_key == ("skill", "a")
    assert rel.target_key == ("task", "b")


def test_confidence_for_evidence_tiers() -> None:
    assert confidence_for_evidence(1) == 0.3
    assert confidence_for_evidence(3) == 0.7
    assert confidence_for_evidence(10) == 0.9


@pytest.mark.asyncio
async def test_add_and_get_relationships_from(relationship_session: _RelationshipSession) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b"))

    outgoing = await get_relationships_from("skill", "a", "tenant-a")

    assert [rel.target_entity_id for rel in outgoing] == ["b"]


@pytest.mark.asyncio
async def test_get_relationships_to(relationship_session: _RelationshipSession) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b"))

    incoming = await get_relationships_to("task", "b", "tenant-a")

    assert [rel.source_entity_id for rel in incoming] == ["a"]


@pytest.mark.asyncio
async def test_relation_type_and_min_confidence_filters(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b", relation_type="co_occurs", confidence=0.4))
    await add_relationship(_rel("a", "c", relation_type="mentions", confidence=0.9))

    outgoing = await get_relationships_from(
        "skill",
        "a",
        "tenant-a",
        relation_types=["mentions"],
        min_confidence=0.5,
    )

    assert [rel.target_entity_id for rel in outgoing] == ["c"]


@pytest.mark.asyncio
async def test_tenant_isolation_in_provider_queries(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b", tenant_id="tenant-a"))
    await add_relationship(_rel("a", "c", tenant_id="tenant-b"))

    outgoing = await get_relationships_from("skill", "a", "tenant-a", min_confidence=0.0)

    assert [rel.target_entity_id for rel in outgoing] == ["b"]


@pytest.mark.asyncio
async def test_reinforce_relationship_raises_confidence_at_three_evidence(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    rel = _rel("a", "b", confidence=0.3, evidence_count=2)
    await add_relationship(rel)

    await reinforce_relationship(rel.relation_id, "tenant-a")
    outgoing = await get_relationships_from("skill", "a", "tenant-a", min_confidence=0.0)

    assert outgoing[0].evidence_count == 3
    assert outgoing[0].confidence == 0.7


@pytest.mark.asyncio
async def test_reinforce_relationship_raises_confidence_at_ten_evidence(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    rel = _rel("a", "b", confidence=0.7, evidence_count=9)
    await add_relationship(rel)

    await reinforce_relationship(rel.relation_id, "tenant-a")
    outgoing = await get_relationships_from("skill", "a", "tenant-a", min_confidence=0.0)

    assert outgoing[0].evidence_count == 10
    assert outgoing[0].confidence == 0.9


@pytest.mark.asyncio
async def test_reinforce_missing_relationship_is_noop(
    relationship_session: _RelationshipSession,
) -> None:
    await reinforce_relationship("rel-missing", "tenant-a")

    assert relationship_session.rows == []


@pytest.mark.asyncio
async def test_find_path_returns_bfs_edges(relationship_session: _RelationshipSession) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b"))
    await add_relationship(
        EntityRelationship(
            tenant_id="tenant-a",
            source_entity_kind="task",
            source_entity_id="b",
            target_entity_kind="memory",
            target_entity_id="c",
            relation_type="mentions",
            confidence=0.8,
        )
    )

    path = await find_path(("skill", "a"), ("memory", "c"), "tenant-a", max_hops=2)

    assert path is not None
    assert [edge.target_entity_id for edge in path] == ["b", "c"]


@pytest.mark.asyncio
async def test_find_path_respects_max_hops(relationship_session: _RelationshipSession) -> None:
    del relationship_session
    await add_relationship(_rel("a", "b"))
    await add_relationship(
        EntityRelationship(
            tenant_id="tenant-a",
            source_entity_kind="task",
            source_entity_id="b",
            target_entity_kind="memory",
            target_entity_id="c",
            relation_type="mentions",
            confidence=0.8,
        )
    )

    assert await find_path(("skill", "a"), ("memory", "c"), "tenant-a", max_hops=1) is None


@pytest.mark.asyncio
async def test_relationship_mine_step_creates_co_occurrence_relationships(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    before = _counter_value(relationship_mine_step_throughput, relation_type="co_occurs")
    now = datetime.now(UTC)
    events = [
        PrecipitationEvent(
            event_id=f"e-{idx}",
            event_type="task.completed",
            payload={
                "tenant_id": "tenant-a",
                "entities": [
                    {"kind": "skill", "id": "python"},
                    {"kind": "task", "id": "analysis"},
                ],
            },
            occurred_at=now - timedelta(minutes=idx),
        )
        for idx in range(3)
    ]

    updates = await RelationshipMineStep().precipitate(
        PrecipitationEvent("trigger", "task.completed", {"tenant_id": "tenant-a"}, now),
        {"tenant_id": "tenant-a", "events": events},
    )
    outgoing = await get_relationships_from("skill", "python", "tenant-a", min_confidence=0.0)

    assert any(rel.relation_type == "co_occurs" for rel in outgoing)
    assert outgoing[0].confidence == 0.7
    assert updates
    after = _counter_value(relationship_mine_step_throughput, relation_type="co_occurs")
    assert after >= before + 2


@pytest.mark.asyncio
async def test_relationship_mine_step_creates_temporal_relationships(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    now = datetime.now(UTC)
    events = [
        PrecipitationEvent(
            event_id="source-1",
            event_type="task.completed",
            payload={"tenant_id": "tenant-a", "entities": [{"kind": "task", "id": "draft"}]},
            occurred_at=now - timedelta(minutes=30),
        ),
        PrecipitationEvent(
            event_id="target-1",
            event_type="task.completed",
            payload={"tenant_id": "tenant-a", "entities": [{"kind": "artifact", "id": "doc"}]},
            occurred_at=now - timedelta(minutes=20),
        ),
        PrecipitationEvent(
            event_id="source-2",
            event_type="task.completed",
            payload={"tenant_id": "tenant-a", "entities": [{"kind": "task", "id": "draft"}]},
            occurred_at=now - timedelta(minutes=10),
        ),
        PrecipitationEvent(
            event_id="target-2",
            event_type="task.completed",
            payload={"tenant_id": "tenant-a", "entities": [{"kind": "artifact", "id": "doc"}]},
            occurred_at=now - timedelta(minutes=5),
        ),
    ]

    await RelationshipMineStep().precipitate(
        PrecipitationEvent("trigger", "task.completed", {"tenant_id": "tenant-a"}, now),
        {"tenant_id": "tenant-a", "events": events},
    )
    outgoing = await get_relationships_from("task", "draft", "tenant-a", min_confidence=0.0)

    assert any(rel.relation_type == "produced_by" for rel in outgoing)


@pytest.mark.asyncio
async def test_relationship_mine_step_ignores_other_tenants(
    relationship_session: _RelationshipSession,
) -> None:
    del relationship_session
    now = datetime.now(UTC)
    event = PrecipitationEvent(
        event_id="e-other",
        event_type="task.completed",
        payload={
            "tenant_id": "tenant-b",
            "entities": [
                {"kind": "skill", "id": "python"},
                {"kind": "task", "id": "analysis"},
            ],
        },
        occurred_at=now,
    )

    await RelationshipMineStep().precipitate(
        PrecipitationEvent("trigger", "task.completed", {"tenant_id": "tenant-a"}, now),
        {"tenant_id": "tenant-a", "events": [event, event, event]},
    )
    outgoing = await get_relationships_from("skill", "python", "tenant-a", min_confidence=0.0)

    assert outgoing == []


@pytest.mark.asyncio
async def test_knowledge_precipitation_queues_relationship_mine_daily() -> None:
    kp = KnowledgePrecipitation()
    kp.register_step(RelationshipMineStep())

    updates = await kp.dispatch(
        PrecipitationEvent(
            event_id="trigger",
            event_type="task.completed",
            payload={"tenant_id": "tenant-a"},
        )
    )

    assert updates == []
    assert len(kp._scheduled_queue["daily"]) == 1
