"""KUN FastAPI entrypoint.

Routes:
  /api/*          KUN main business
  /nuo/*          傩 (NUO) separate namespace (ADR-012 schema-isolated)
  /ws             WebSocket dialog (ADR-010)
  /health         Liveness
  /metrics        Prometheus scrape
  /docs           OpenAPI docs
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from kun import __version__
from kun.api.chat import router as chat_router
from kun.api.health import router as health_router
from kun.api.nuo import router as nuo_router
from kun.api.runtime import install_runtime
from kun.api.ws import ws_router
from kun.core.config import settings
from kun.core.events import outbox_worker
from kun.core.logging import configure_logging, get_logger
from kun.core.tenancy import TenantContext, tenant_scope
from kun.watchtower.engine import RuleEngine, load_rules

log = get_logger("kun.api.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks."""
    configure_logging()
    log.info("kun.api.starting", version=__version__, env=settings().env)

    # Load rules into a shared engine; orchestrator reuses it
    rules = load_rules("rules")
    rule_engine = RuleEngine(rules)
    install_runtime(app, rule_engine=rule_engine)
    log.info("rules.ready", count=len(rules))

    # Start outbox worker
    app.state.outbox_task = asyncio.create_task(outbox_worker(interval_sec=0.5))

    # Opentelemetry auto-instrumentation (best effort)
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        log.warning("otel.instrumentation_failed", error=str(e))

    yield

    outbox: asyncio.Task[None] | None = getattr(app.state, "outbox_task", None)
    if outbox is not None:
        outbox.cancel()
        with suppress(asyncio.CancelledError):
            await outbox
    log.info("kun.api.stopped")


app = FastAPI(
    title="鲲 (KUN) API",
    description="Agent OS / Agent 管家",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().api_cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def tenant_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ADR-007: resolve tenant from X-Tenant-Id header or fall back to default."""
    tenant_id = request.headers.get("X-Tenant-Id", settings().default_tenant_id)
    user_id = request.headers.get("X-User-Id")
    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id)
    with tenant_scope(ctx):
        return await call_next(request)


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(ws_router)
app.include_router(nuo_router, prefix="/nuo", tags=["nuo"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "kun", "version": __version__}
