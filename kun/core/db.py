"""SQLAlchemy async engine + session factory + base declarative."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from kun.core.config import settings
from kun.core.tenancy import current_tenant

# Naming convention for constraints — keeps alembic migrations deterministic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_admin_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_admin_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings().pg_dsn,
            pool_size=settings().pg_pool_size,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_admin_engine() -> AsyncEngine:
    global _admin_engine
    if _admin_engine is None:
        _admin_engine = create_async_engine(
            settings().pg_admin_dsn,
            pool_size=1,
            pool_pre_ping=True,
            echo=False,
        )
    return _admin_engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


def get_admin_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _admin_sessionmaker
    if _admin_sessionmaker is None:
        _admin_sessionmaker = async_sessionmaker(
            get_admin_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _admin_sessionmaker


@asynccontextmanager
async def session_scope(
    *,
    tenant_id: str | None = None,
    bypass_rls: bool = False,
) -> AsyncIterator[AsyncSession]:
    """Transactional scope for a unit of work.

    Every business session sets the Postgres RLS tenant GUC up front. Normal
    code uses the application role. System workers such as the outbox poller
    must opt into bypass_rls explicitly, which uses the admin DSN instead of a
    user-settable "bypass" flag.
    """
    maker = get_admin_sessionmaker() if bypass_rls else get_sessionmaker()
    async with maker() as s:
        try:
            await _set_rls_context(s, tenant_id=tenant_id)
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def _set_rls_context(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
) -> None:
    effective_tenant_id = (tenant_id or "").strip() or current_tenant().tenant_id
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": effective_tenant_id},
    )
