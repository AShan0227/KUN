from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from kun.api.graph import router
from kun.context.graph_traversal import NeighborEntity
from kun.core.orm import EntityRelationshipRow
from kun.core.tenancy import TenantContext, tenant_scope


class _Result:
    def __init__(self, row: EntityRelationshipRow | None = None, *, rowcount: int = 0):
        self._row = row
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, store: dict[tuple[str, str], EntityRelationshipRow]):
        self.store = store

    async def get(self, _model, key):
        return self.store.get(tuple(key))

    def add(self, row: EntityRelationshipRow) -> None:
        self.store[(row.relation_id, row.tenant_id)] = row

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):
        tenant_id = _tenant_from_statement_or_store(self.store)
        row = next((r for (_rid, tid), r in self.store.items() if tid == tenant_id), None)
        if getattr(stmt, "is_update", False):
            if row is None:
                return _Result(None, rowcount=0)
            for key, bind in stmt._values.items():
                attr = key.key if hasattr(key, "key") else str(key)
                if attr == "metadata":
                    attr = "metadata_json"
                value = getattr(bind, "value", bind)
                setattr(row, attr, value)
            return _Result(row, rowcount=1)
        if getattr(stmt, "is_delete", False):
            if row is None:
                return _Result(None, rowcount=0)
            del self.store[(row.relation_id, row.tenant_id)]
            return _Result(row, rowcount=1)
        return _Result(row, rowcount=1 if row else 0)


def _tenant_from_statement_or_store(store: dict[tuple[str, str], EntityRelationshipRow]) -> str:
    if store:
        return next(iter(store.values())).tenant_id
    return "t-1"


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_middleware(request: Request, call_next: Callable[..., Any]):
        with tenant_scope(
            TenantContext(
                tenant_id=request.headers.get("X-Tenant-Id", "t-1"),
                user_id=request.headers.get("X-User-Id"),
            )
        ):
            return await call_next(request)

    app.include_router(router)
    return app


@pytest.fixture
def store(monkeypatch) -> dict[tuple[str, str], EntityRelationshipRow]:
    rows: dict[tuple[str, str], EntityRelationshipRow] = {}

    @asynccontextmanager
    async def _scope(*_args, **_kwargs) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(rows)

    monkeypatch.setattr("kun.api.graph.session_scope", _scope)
    return rows


@pytest.mark.unit
def test_graph_neighbors_endpoint(monkeypatch):
    class FakeTraversal:
        async def neighbors(self, kind, entity_id, *, hops=1, limit_per_hop=20):
            assert kind == "task"
            assert entity_id == "task-1"
            assert hops == 2
            assert limit_per_hop == 10
            return [
                NeighborEntity(
                    entity_kind="skill",
                    entity_id="s-1",
                    relation_type="produced_by",
                    confidence=0.8,
                    hops=1,
                    via_path=(("task", "task-1"), ("skill", "s-1")),
                )
            ]

    monkeypatch.setattr("kun.api.graph.GraphTraversal", FakeTraversal)
    client = TestClient(_make_app())

    res = client.get(
        "/api/graph/relationships?source_kind=task&source_id=task-1&hops=2&limit_per_hop=10",
        headers={"X-Tenant-Id": "t-1", "X-User-Id": "u-1"},
    )

    assert res.status_code == 200
    assert res.json()[0]["entity_id"] == "s-1"
    assert res.json()[0]["score"] == 0.8


@pytest.mark.unit
def test_graph_relationship_crud(store):
    client = TestClient(_make_app())
    headers = {"X-Tenant-Id": "t-1", "X-User-Id": "u-1"}

    created = client.post(
        "/api/graph/relationships",
        headers=headers,
        json={
            "source_entity_kind": "task",
            "source_entity_id": "task-1",
            "target_entity_kind": "skill",
            "target_entity_id": "s-1",
            "relation_type": "produced_by",
            "confidence": 0.7,
            "metadata": {"source": "manual"},
        },
    )
    assert created.status_code == 201
    relation_id = created.json()["relation_id"]
    assert (relation_id, "t-1") in store

    detail = client.get(f"/api/graph/relationships/{relation_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["target_entity_id"] == "s-1"

    patched = client.patch(
        f"/api/graph/relationships/{relation_id}",
        headers=headers,
        json={"confidence": 0.9, "metadata": {"reviewed": True}},
    )
    assert patched.status_code == 200
    assert patched.json()["confidence"] == 0.9
    assert patched.json()["metadata"] == {"reviewed": True}

    deleted = client.delete(f"/api/graph/relationships/{relation_id}", headers=headers)
    assert deleted.status_code == 204
    assert (relation_id, "t-1") not in store


@pytest.mark.unit
def test_graph_websocket_explore(monkeypatch):
    class FakeTraversal:
        async def neighbors(self, kind, entity_id, *, hops=1, limit_per_hop=20):
            return [
                NeighborEntity(
                    entity_kind="asset",
                    entity_id=f"{kind}:{entity_id}",
                    relation_type="mentions",
                    confidence=0.6,
                    hops=1,
                    via_path=((kind, entity_id), ("asset", f"{kind}:{entity_id}")),
                )
            ]

    monkeypatch.setattr("kun.api.graph.GraphTraversal", FakeTraversal)
    client = TestClient(_make_app())

    with client.websocket_connect("/api/graph/explore?tenant_id=t-1&user_id=u-1") as ws:
        ws.send_json({"source_kind": "task", "source_id": "task-1", "hops": 1})
        msg = ws.receive_json()

    assert msg["type"] == "graph_neighbors"
    assert msg["neighbors"][0]["entity_id"] == "task:task-1"
