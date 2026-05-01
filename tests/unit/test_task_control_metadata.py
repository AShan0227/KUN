from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api import task_control
from kun.api.task_control import router
from kun.core.orm import TaskRow
from kun.core.state_ledger import get_state_ledger, reset_state_ledger
from kun.core.tenancy import TenantContext, tenant_scope


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _FakeSession:
    def __init__(self, task: TaskRow | None) -> None:
        self.task = task
        self.added: list[Any] = []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return _ScalarResult(self.task)

    def add(self, row: Any) -> None:
        self.added.append(row)


@pytest.fixture(autouse=True)
def _clear_ledger() -> None:
    reset_state_ledger()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.unit
def test_update_task_metadata_persists_event_and_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    task = TaskRow(
        task_id="task-a",
        tenant_id="tenant-a",
        fingerprint="sha256:" + "a" * 64,
        task_type="chat.unstructured",
        risk_level="low",
        complexity_score=0.2,
        user_id="user-a",
        estimated_cost_usd=0.05,
        estimated_duration_sec=10,
        success_criteria_short="old goal",
        version=1,
        spec_json={"constraints": []},
    )
    fake_session = _FakeSession(task)

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any) -> AsyncIterator[_FakeSession]:
        yield fake_session

    monkeypatch.setattr(task_control, "session_scope", fake_scope)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_app()).patch(
            "/api/tasks/task-a/metadata",
            json={
                "risk_level": "high",
                "estimated_cost_usd": 1.25,
                "constraint_note": "先问我再外发",
                "confirmation_policy": "ask_before_external",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] is True
    assert body["changed_fields"] == [
        "confirmation_policy",
        "constraint_note",
        "estimated_cost_usd",
        "risk_level",
    ]
    assert task.risk_level == "high"
    assert task.estimated_cost_usd == 1.25
    spec_json = task.spec_json or {}
    assert spec_json["constraints"][-1] == {"kind": "custom", "detail": "先问我再外发"}
    assert spec_json["user_controls"]["confirmation_policy"] == "ask_before_external"
    assert fake_session.added[0].event_type == "task.metadata_updated"

    ledger = get_state_ledger().snapshot("task-a", tenant_id="tenant-a")
    assert ledger is not None
    assert ledger.current_risk == "high"
    assert ledger.budget_estimated_usd == 1.25
    assert ledger.recent_events[-1].kind == "task.metadata_updated"


@pytest.mark.unit
def test_update_task_metadata_requires_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession(None)

    monkeypatch.setattr(task_control, "session_scope", fake_scope)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_app()).patch("/api/tasks/task-a/metadata", json={})

    assert response.status_code == 400
