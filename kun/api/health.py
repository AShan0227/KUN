"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from kun import __version__
from kun.core.config import settings
from kun.core.logging import get_logger

router = APIRouter()
log = get_logger("kun.api.health")


@router.get("/")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "env": settings().env}


@router.get("/ready")
async def ready() -> dict:
    """Check dependencies: DB, Redis, NATS (best-effort)."""
    from kun.core.db import get_engine

    checks: dict[str, str] = {}

    # Postgres
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"down: {e!r}"

    # Redis (optional)
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings().redis_url)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"down: {e!r}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
