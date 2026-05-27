"""L6.AuthScaffold — Postgres RLS binding 单测 (用 stub session, 无需真 DB)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.api.auth.rls import InvalidTenantIdError, bind_tenant_to_session


class _RecordingSession:
    """Stub AsyncSession recording every execute() call."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, clause: Any, params: dict | None = None) -> None:
        # clause is a SQLAlchemy `TextClause` — str(clause) renders the SQL
        self.executed.append((str(clause), params))


@pytest.mark.asyncio
async def test_bind_emits_set_local_with_tenant() -> None:
    session = _RecordingSession()
    await bind_tenant_to_session(session, "u-sylvan")  # type: ignore[arg-type]
    assert len(session.executed) == 1
    sql, _ = session.executed[0]
    assert "SET LOCAL app.tenant_id" in sql
    assert "u-sylvan" in sql


@pytest.mark.asyncio
async def test_bind_allows_typical_tenant_ids() -> None:
    session = _RecordingSession()
    for tid in [
        "u-sylvan",
        "t-acme",
        "tenant_123",
        "u-ABC-DEF",
        "550e8400-e29b-41d4-a716-446655440000",  # UUID
        "tenant.subtenant",
    ]:
        await bind_tenant_to_session(session, tid)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bind_rejects_sql_injection_attempts() -> None:
    session = _RecordingSession()
    bad_inputs = [
        "evil'; DROP TABLE users; --",
        "x' OR '1'='1",
        "tenant with space",
        "tenant\nwith\nnewline",
        "tenant;extra",
        "tenant\"quote",
    ]
    for bad in bad_inputs:
        with pytest.raises(InvalidTenantIdError):
            await bind_tenant_to_session(session, bad)  # type: ignore[arg-type]
    # nothing was executed
    assert session.executed == []


@pytest.mark.asyncio
async def test_bind_rejects_empty_tenant_id() -> None:
    session = _RecordingSession()
    with pytest.raises(InvalidTenantIdError, match="non-empty"):
        await bind_tenant_to_session(session, "")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bind_rejects_non_string() -> None:
    session = _RecordingSession()
    with pytest.raises(InvalidTenantIdError, match="non-empty"):
        await bind_tenant_to_session(session, None)  # type: ignore[arg-type]
    with pytest.raises(InvalidTenantIdError, match="non-empty"):
        await bind_tenant_to_session(session, 123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_bind_rejects_overlong_tenant() -> None:
    session = _RecordingSession()
    with pytest.raises(InvalidTenantIdError):
        await bind_tenant_to_session(session, "x" * 200)  # type: ignore[arg-type]
