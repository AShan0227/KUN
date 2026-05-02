"""Integration: SoulFile DB persistence (load_or_create / save / preload).

需要真 Postgres (KUN_PG_DSN / KUN_PG_ADMIN_DSN). 没配置即 skip.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.core.config import settings
from kun.datamodel.soul_file_provider import (
    load_or_create_soul_file,
    preload_all_soul_files,
    reset_store,
    save_soul_file,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def db_session_setup(monkeypatch: pytest.MonkeyPatch):
    """每测重置 in-memory cache + 重写 session_scope 使其指向真 PG."""
    if "KUN_PG_ADMIN_DSN" not in os.environ and "KUN_PG_DSN" not in os.environ:
        pytest.skip("no Postgres configured (KUN_PG_ADMIN_DSN / KUN_PG_DSN)")

    admin_engine = create_async_engine(settings().pg_admin_dsn, pool_size=1, pool_pre_ping=True)
    app_engine = create_async_engine(settings().pg_dsn, pool_size=1, pool_pre_ping=True)
    admin_maker = async_sessionmaker(admin_engine, class_=AsyncSession, expire_on_commit=False)
    app_maker = async_sessionmaker(app_engine, class_=AsyncSession, expire_on_commit=False)

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
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": tenant_id or "tenant-soul-test"},
                )
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    monkeypatch.setattr("kun.core.db.session_scope", local_session_scope)
    reset_store()

    async def _cleanup() -> None:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM soul_files WHERE tenant_id = :t"),
                {"t": "tenant-soul-test"},
            )

    await _cleanup()
    yield
    await _cleanup()
    reset_store()
    await app_engine.dispose()
    await admin_engine.dispose()


@pytest.mark.asyncio
async def test_load_or_create_inserts_default(db_session_setup) -> None:
    soul = await load_or_create_soul_file("u-1", tenant_id="tenant-soul-test")
    assert soul.user_id == "u-1"
    assert soul.audience == "developer"  # 默认


@pytest.mark.asyncio
async def test_save_then_reload_preserves_changes(db_session_setup) -> None:
    soul = await load_or_create_soul_file("u-2", tenant_id="tenant-soul-test")
    soul.audience = "expert"
    soul.cost_sensitivity = "high"
    soul.professional_role = "后端工程师"
    await save_soul_file(soul)

    # 清 cache, 强制走 DB
    reset_store()
    reloaded = await load_or_create_soul_file("u-2", tenant_id="tenant-soul-test")
    assert reloaded.audience == "expert"
    assert reloaded.cost_sensitivity == "high"
    assert reloaded.professional_role == "后端工程师"


@pytest.mark.asyncio
async def test_preload_all_loads_existing(db_session_setup) -> None:
    s1 = await load_or_create_soul_file("u-3", tenant_id="tenant-soul-test")
    s1.audience = "expert"
    await save_soul_file(s1)
    s2 = await load_or_create_soul_file("u-4", tenant_id="tenant-soul-test")
    s2.audience = "novice"
    await save_soul_file(s2)

    reset_store()
    count = await preload_all_soul_files(tenant_id="tenant-soul-test")
    assert count >= 2

    # cache 现在应该有 u-3 + u-4, get_soul_file 不会再创建默认
    from kun.datamodel.soul_file_provider import get_soul_file

    assert get_soul_file("u-3", tenant_id="tenant-soul-test").audience == "expert"
    assert get_soul_file("u-4", tenant_id="tenant-soul-test").audience == "novice"


@pytest.mark.asyncio
async def test_save_persists_nested_blob(db_session_setup) -> None:
    """nested 字段 (revision_history / preferred_tools) 走 blob JSONB."""
    from kun.datamodel.soul_file import EvolvedTrait

    soul = await load_or_create_soul_file("u-5", tenant_id="tenant-soul-test")
    soul.evolved_traits = [
        EvolvedTrait(trait="prefers_concise_output", evidence_count=5),
    ]
    soul.preferred_tools = [{"tool_id": "web_search", "trust_level": 0.9}]
    await save_soul_file(soul)

    reset_store()
    reloaded = await load_or_create_soul_file("u-5", tenant_id="tenant-soul-test")
    assert len(reloaded.evolved_traits) == 1
    assert reloaded.evolved_traits[0].trait == "prefers_concise_output"
    assert reloaded.preferred_tools[0]["tool_id"] == "web_search"
