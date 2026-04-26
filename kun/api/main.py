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
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from kun import __version__
from kun.api.attention_pin import router as attention_pin_router
from kun.api.billing_transparency import router as billing_router
from kun.api.blackboard import router as blackboard_router
from kun.api.chat import router as chat_router
from kun.api.diagnose import router as diagnose_router
from kun.api.health import router as health_router
from kun.api.nuo import router as nuo_router
from kun.api.runtime import install_runtime
from kun.api.task_control import router as task_control_router
from kun.api.ws import ws_router
from kun.core.config import settings
from kun.core.events import outbox_worker
from kun.core.logging import configure_logging, get_logger
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    resolve_tenant_id,
    tenant_scope,
)
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

    # V2.1 wire W5: 黑板 5 endpoint 接真实数据源 (events / TaskRow / RuntimeStateRow)
    try:
        from kun.api.blackboard_data_sources import install_blackboard_data_sources

        install_blackboard_data_sources()
        log.info("blackboard.data_sources.installed")
    except Exception as e:
        log.warning("blackboard.data_sources.install_failed", error=str(e))

    # V2.1 wire W7: KnowledgePrecipitation 接 idle_batch
    try:
        from kun.engineering.precipitation_idle_step import install_precipitation_steps

        install_precipitation_steps()
        log.info("precipitation.idle_steps.installed")
    except Exception as e:
        log.warning("precipitation.idle_steps.install_failed", error=str(e))

    # Register builtin executable skills (R-A2). Imports the 6 builtin
    # modules so their @register calls populate the dispatcher table.
    try:
        from kun.skills.dispatcher import autoload_builtins, list_registered

        autoload_builtins()
        log.info("skills.builtin.loaded", count=len(list_registered()))
    except Exception as e:
        log.warning("skills.builtin.load_failed", error=str(e))

    # Seed context assets if the store is empty — without seeds the
    # ContextPacker preheat path returns nothing (R-A5).
    try:
        from kun.context.seeds import seed_default

        default_tenant = settings().default_tenant_id or "u-sylvan"
        seeded = await seed_default(tenant_id=default_tenant)
        if seeded:
            log.info("context.seeds.applied", tenant_id=default_tenant, count=seeded)
    except Exception as e:
        log.warning("context.seeds.startup_failed", error=str(e))

    # Start outbox worker
    app.state.outbox_task = asyncio.create_task(outbox_worker(interval_sec=0.5))

    # Start NATS subscriber (小尾巴 C: 跨进程订阅)
    # Default ON; KUN_NATS_SUBSCRIBER_ENABLED=0 关闭. 没 NATS 时 worker 自己
    # 优雅退出, 不会阻塞 startup.
    import os as _os

    if _os.getenv("KUN_NATS_SUBSCRIBER_ENABLED", "1") == "1":
        from kun.core.nats_subscriber import subscriber_worker

        app.state.nats_subscriber_task = asyncio.create_task(
            subscriber_worker(
                subject_pattern=_os.getenv("KUN_NATS_SUBSCRIBE_SUBJECT", "kun.>"),
                queue=_os.getenv("KUN_NATS_QUEUE_GROUP", "kun-watchtower"),
            )
        )
        log.info("nats_subscriber.scheduled")

    # Start idle-batch worker (R-A8) if enabled.
    # Default ON in dev, off in production until we've verified it.
    import os

    if os.getenv("KUN_IDLE_BATCH_ENABLED", "1") == "1":
        from kun.engineering.idle_batch import idle_batch_worker

        idle_interval = int(os.getenv("KUN_IDLE_BATCH_INTERVAL_SEC", "3600"))
        app.state.idle_batch_task = asyncio.create_task(
            idle_batch_worker(
                interval_sec=idle_interval,
                tenant_id=settings().default_tenant_id or "u-sylvan",
            )
        )
        log.info("idle_batch.scheduled", interval_sec=idle_interval)

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

    idle_batch: asyncio.Task[None] | None = getattr(app.state, "idle_batch_task", None)
    if idle_batch is not None:
        idle_batch.cancel()
        with suppress(asyncio.CancelledError):
            await idle_batch

    nats_sub: asyncio.Task[None] | None = getattr(app.state, "nats_subscriber_task", None)
    if nats_sub is not None:
        nats_sub.cancel()
        with suppress(asyncio.CancelledError):
            await nats_sub

    # Close LLM router providers — important for CodexMcpProvider which holds
    # a long-lived `codex mcp-server` subprocess. Without this, an API restart
    # leaves orphan processes that pile up over time.
    try:
        from kun.interface.llm.router import get_router

        await get_router().close()
    except Exception as e:
        log.warning("kun.api.router_close_failed", error=str(e))

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
    """ADR-007: resolve tenant from X-Tenant-Id, with fallback disabled in production.

    Also threads X-Scopes (comma-separated) into TenantContext so endpoints can
    enforce permission checks (R-A12). Empty / missing scopes = empty tuple.
    """
    try:
        tenant_id = resolve_tenant_id(request.headers.get("X-Tenant-Id"))
    except MissingTenantContextError:
        return JSONResponse(
            status_code=400,
            content={"detail": "X-Tenant-Id header is required"},
        )
    user_id = request.headers.get("X-User-Id")
    raw_scopes = request.headers.get("X-Scopes") or ""
    scopes = tuple(s.strip() for s in raw_scopes.split(",") if s.strip())
    raw_audience = (request.headers.get("X-Audience") or "developer").lower()
    audience = raw_audience if raw_audience in {"novice", "developer", "expert"} else "developer"
    ctx = TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        scopes=scopes,
        audience=audience,  # type: ignore[arg-type]
    )
    with tenant_scope(ctx):
        return await call_next(request)


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(billing_router, prefix="/api/billing", tags=["billing"])
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(ws_router)
app.include_router(nuo_router, prefix="/nuo", tags=["nuo"])
# V2.1 wire: 黑板 + 注意力 pin + task control (kill switch / timeout)
app.include_router(blackboard_router)
app.include_router(attention_pin_router)
app.include_router(task_control_router)
# V2.1 §10.6 / M3.2 提前: 傩诊断
app.include_router(diagnose_router, prefix="/api/diagnose", tags=["diagnose"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "kun", "version": __version__}
