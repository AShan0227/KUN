"""Health and readiness endpoints."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, cast

from fastapi import APIRouter
from sqlalchemy import text

from kun import __version__
from kun.core.config import settings
from kun.core.logging import get_logger

router = APIRouter()
log = get_logger("kun.api.health")


@router.get("/")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "env": settings().env}


@router.get("/ready")
async def ready() -> dict[str, Any]:
    """Check dependencies: DB, Redis, NATS (best-effort)."""
    from kun.core.db import get_engine

    checks: dict[str, str] = {}

    # Postgres
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            role = (
                await conn.execute(
                    text(
                        """
                        SELECT rolsuper, rolbypassrls
                        FROM pg_roles
                        WHERE rolname = current_user
                        """
                    )
                )
            ).one()
        if bool(role.rolsuper) or bool(role.rolbypassrls):
            checks["postgres"] = "degraded: app role bypasses RLS"
        else:
            checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"down: {e!r}"

    # Redis (optional)
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings().redis_url)
        await cast(Awaitable[Any], r.ping())
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"down: {e!r}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
