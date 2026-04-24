"""Event builder tests."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from kun.core import events as events_module
from kun.core.events import _unpublished_stmt, outbox_worker
from kun.datamodel.events import Event
from sqlalchemy.dialects import postgresql


@pytest.mark.unit
def test_event_subject_format() -> None:
    ev = Event.build(
        tenant_id="u-sylvan",
        event_type="task.started",
        payload={"task_id": "tk-xxx"},
        task_ref="tk-xxx",
    )
    assert ev.subject == "kun.u-sylvan.task.task.started"
    assert ev.event_id.startswith("ev-")
    assert ev.task_ref == "tk-xxx"


@pytest.mark.unit
def test_outbox_fetch_uses_skip_locked() -> None:
    dialect = cast(Any, postgresql.dialect())  # type: ignore[no-untyped-call]
    sql = str(_unpublished_stmt(limit=5).compile(dialect=dialect))

    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_worker_retries_nats_when_initial_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def fake_connect() -> object | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None
        raise asyncio.CancelledError

    async def fake_sleep(_interval: float) -> None:
        return None

    @asynccontextmanager
    async def fake_session_scope() -> AsyncIterator[object]:
        yield object()

    async def fake_count_unpublished(_session: object) -> int:
        return 0

    monkeypatch.setattr(events_module, "connect_nats", fake_connect)
    monkeypatch.setattr("kun.core.events.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(events_module, "count_unpublished", fake_count_unpublished)
    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)

    with pytest.raises(asyncio.CancelledError):
        await outbox_worker(interval_sec=0.01)

    assert attempts == 2
