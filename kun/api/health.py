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
    """Check all runtime dependencies — covers DB / Redis / NATS / Qdrant /
    MinIO / codex CLI / claude CLI (best-effort).

    Each probe is wrapped in its own try/except — a single down dependency
    surfaces as ``degraded`` instead of failing the whole endpoint. R-D2.
    """
    from kun.core.db import get_engine

    checks: dict[str, str] = {}
    production_issues = settings().production_safety_issues()
    checks["production_config"] = "ok" if not production_issues else "; ".join(production_issues)

    # Postgres + RLS guard
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

    # Redis
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings().redis_url)
        await cast(Awaitable[Any], r.ping())
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"down: {e!r}"

    # NATS
    try:
        import nats

        nc = await nats.connect(settings().nats_url, connect_timeout=2)
        await nc.close()
        checks["nats"] = "ok"
    except Exception as e:
        checks["nats"] = f"down: {e!r}"

    # Qdrant
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            r2 = await client.get(f"{settings().qdrant_url}/healthz")
            checks["qdrant"] = "ok" if r2.status_code < 400 else f"degraded: {r2.status_code}"
    except Exception as e:
        checks["qdrant"] = f"down: {e!r}"

    # MinIO / S3
    try:
        from kun.core.object_store import get_object_store

        await get_object_store().ensure_bucket()
        checks["minio"] = "ok"
    except Exception as e:
        checks["minio"] = f"down: {e!r}"

    # Codex CLI (subscription path)
    import shutil

    checks["codex_cli"] = "ok" if shutil.which("codex") else "absent"
    # Claude Code CLI (subscription path)
    checks["claude_cli"] = "ok" if shutil.which("claude") else "absent"

    overall = "ok" if all(v in {"ok", "absent"} for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
