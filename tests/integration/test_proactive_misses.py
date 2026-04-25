"""Integration: task.tool_skipped is persisted and promoted per tenant."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.core.config import settings
from kun.core.orm import EventRow
from kun.watchtower.handlers import handle_tool_skipped
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.integration


def _event(*, tenant_id: str, event_id: str) -> EventRow:
    return EventRow(
        event_id=event_id,
        tenant_id=tenant_id,
        event_type="task.tool_skipped",
        subject=f"kun.{tenant_id}.task.task.tool_skipped",
        payload={
            "missed": [
                {
                    "skill_id": "web-search",
                    "reason": "executor_unregistered",
                    "pattern": "latest|today",
                    "trigger_source": "skill_manifest",
                }
            ]
        },
        task_ref="task-proactive",
    )


async def _cleanup(engine: AsyncEngine, *tenant_ids: str) -> None:
    async with engine.begin() as conn:
        for tenant_id in tenant_ids:
            await conn.execute(
                text("DELETE FROM events WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await conn.execute(
                text("DELETE FROM proactive_misses WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )


@pytest.mark.asyncio
async def test_tool_skipped_promotes_once_and_stays_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "KUN_PG_ADMIN_DSN" not in os.environ and "KUN_PG_DSN" not in os.environ:
        pytest.skip("no Postgres configured (KUN_PG_ADMIN_DSN / KUN_PG_DSN)")

    admin_engine = create_async_engine(settings().pg_admin_dsn, pool_size=1, pool_pre_ping=True)
    app_engine = create_async_engine(settings().pg_dsn, pool_size=1, pool_pre_ping=True)
    admin_maker = async_sessionmaker(
        admin_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    app_maker = async_sessionmaker(
        app_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    @asynccontextmanager
    async def local_session_scope(
        *,
        tenant_id: str | None = None,
        bypass_rls: bool = False,
    ) -> AsyncIterator[AsyncSession]:
        maker = admin_maker if bypass_rls else app_maker
        async with maker() as s:
            try:
                await s.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id or "tenant-pro-a"},
                )
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr("kun.core.db.session_scope", local_session_scope)

    await _cleanup(admin_engine, "tenant-pro-a", "tenant-pro-b")
    try:
        for i in range(10):
            await handle_tool_skipped(_event(tenant_id="tenant-pro-a", event_id=f"evt-a-{i}"))
        for i in range(9):
            await handle_tool_skipped(_event(tenant_id="tenant-pro-b", event_id=f"evt-b-{i}"))

        async with admin_engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT tenant_id, miss_count, promoted_at IS NOT NULL AS promoted
                        FROM proactive_misses
                        WHERE tenant_id IN ('tenant-pro-a', 'tenant-pro-b')
                        ORDER BY tenant_id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            events = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT tenant_id, count(*) AS n
                        FROM events
                        WHERE event_type = 'proactive.trigger_promoted'
                          AND tenant_id IN ('tenant-pro-a', 'tenant-pro-b')
                        GROUP BY tenant_id
                        ORDER BY tenant_id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )

        assert [dict(row) for row in rows] == [
            {"tenant_id": "tenant-pro-a", "miss_count": 10, "promoted": True},
            {"tenant_id": "tenant-pro-b", "miss_count": 9, "promoted": False},
        ]
        assert [dict(row) for row in events] == [{"tenant_id": "tenant-pro-a", "n": 1}]
    finally:
        await _cleanup(admin_engine, "tenant-pro-a", "tenant-pro-b")
        await app_engine.dispose()
        await admin_engine.dispose()
