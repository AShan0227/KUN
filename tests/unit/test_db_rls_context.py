"""RLS context wiring tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.core import db as db_module
from kun.core.db import session_scope
from kun.core.tenancy import MissingTenantContextError, TenantContext, tenant_scope


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any] | None] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt: object, params: dict[str, Any] | None = None) -> None:
        self.calls.append(params)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeMaker:
    def __init__(self) -> None:
        self.session = _FakeSession()

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[_FakeSession]:
        yield self.session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_scope_sets_current_tenant_rls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker = _FakeMaker()
    monkeypatch.setattr(db_module, "_sessionmaker", maker)

    with tenant_scope(TenantContext(tenant_id="u-rls", user_id="alice")):
        async with session_scope():
            pass

    assert maker.session.calls == [
        {"tenant_id": "u-rls"},
    ]
    assert maker.session.committed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_scope_can_bypass_rls_for_system_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_maker = _FakeMaker()
    admin_maker = _FakeMaker()
    monkeypatch.setattr(db_module, "_sessionmaker", app_maker)
    monkeypatch.setattr(db_module, "_admin_sessionmaker", admin_maker)

    async with session_scope(tenant_id="system", bypass_rls=True):
        pass

    assert app_maker.session.calls == []
    assert admin_maker.session.calls == [
        {"tenant_id": "system"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bypass_rls_without_tenant_does_not_crash_or_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Audit F026: outbox poller / NATS subscriber / GC open bypass sessions with
    # no tenant every tick. In production current_tenant() raises — the bypass
    # path must not call it, must not crash, and must not set a tenant GUC.
    admin_maker = _FakeMaker()
    monkeypatch.setattr(db_module, "_admin_sessionmaker", admin_maker)

    def _boom() -> TenantContext:
        raise MissingTenantContextError("no tenant in production")

    monkeypatch.setattr(db_module, "current_tenant", _boom)

    async with session_scope(bypass_rls=True):  # no tenant_id, no tenant_scope
        pass

    assert admin_maker.session.calls == []  # no GUC scoping for admin/bypass
    assert admin_maker.session.committed is True
