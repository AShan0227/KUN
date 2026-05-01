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
import hashlib
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
from kun.api.code_capability import router as code_capability_router
from kun.api.compiler import router as compiler_router
from kun.api.diagnose import router as diagnose_router
from kun.api.graph import router as graph_router
from kun.api.health import router as health_router
from kun.api.lab import router as lab_router
from kun.api.missions import router as missions_router
from kun.api.nuo import router as nuo_router
from kun.api.protocols import router as protocols_router
from kun.api.qi import router as qi_router
from kun.api.runtime import install_runtime
from kun.api.session import router as session_router
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
    try:
        from kun.context.storage import configure_store_from_settings

        context_store_backend = await configure_store_from_settings()
        app.state.context_store_backend = context_store_backend
        log.info("context.store.ready", backend=context_store_backend)
    except Exception as e:
        app.state.context_store_backend = "memory"
        log.warning("context.store.configure_failed", error=str(e))
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

        default_tenant = settings().default_tenant_id
        if default_tenant:
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
        from kun.core.nats_subscriber import make_watchtower_handler, subscriber_worker

        app.state.nats_subscriber_task = asyncio.create_task(
            subscriber_worker(
                subject_pattern=_os.getenv("KUN_NATS_SUBSCRIBE_SUBJECT", "kun.>"),
                queue=_os.getenv("KUN_NATS_QUEUE_GROUP", "kun-watchtower"),
                handlers=[make_watchtower_handler(rule_engine)],
            )
        )
        log.info("nats_subscriber.scheduled")

    # Start idle-batch worker (R-A8) if enabled.
    # Default ON in dev, off in production until we've verified it.
    import os

    default_tenant = settings().default_tenant_id
    if os.getenv("KUN_IDLE_BATCH_ENABLED", "1") == "1" and default_tenant:
        from kun.engineering.idle_batch import idle_batch_worker

        idle_interval = int(os.getenv("KUN_IDLE_BATCH_INTERVAL_SEC", "3600"))
        app.state.idle_batch_task = asyncio.create_task(
            idle_batch_worker(
                interval_sec=idle_interval,
                tenant_id=default_tenant,
            )
        )
        log.info("idle_batch.scheduled", interval_sec=idle_interval)

    # V2.1 M4: 真 cron scheduler — 默认开, KUN_CRON_SCHEDULER_ENABLED=0 关
    if os.getenv("KUN_CRON_SCHEDULER_ENABLED", "1") == "1":
        from kun.api.runtime import get_cron_scheduler
        from kun.engineering.idle_batch import run_all as _run_idle_steps
        from kun.engineering.precipitation_idle_step import get_kp

        sched = get_cron_scheduler(app)
        if default_tenant:

            async def _hourly_idle_batch() -> None:
                await _run_idle_steps(default_tenant)

            sched.register("idle_batch_hourly", "@hourly", _hourly_idle_batch)

        async def _daily_kp() -> None:
            await get_kp().run_scheduled("daily")

        async def _weekly_kp() -> None:
            await get_kp().run_scheduled("weekly")

        sched.register("kp_daily", "@daily", _daily_kp)
        sched.register("kp_weekly", "@weekly", _weekly_kp)

        if default_tenant and os.getenv("KUN_CONTEXT_MAINTENANCE_ENABLED", "1") == "1":
            from kun.context.maintenance import run_context_maintenance

            async def _daily_context_maintenance() -> None:
                mutate = os.getenv("KUN_CONTEXT_MAINTENANCE_MUTATE", "0") == "1"
                await run_context_maintenance(
                    tenant_id=default_tenant,
                    dry_run=not mutate,
                )

            sched.register("nuo_context_maintenance_daily", "@daily", _daily_context_maintenance)

        if default_tenant and os.getenv("KUN_MISSION_RESUME_WORKER_ENABLED", "1") == "1":
            from kun.api.runtime import get_mission_resume_worker

            async def _mission_resume_once() -> None:
                await get_mission_resume_worker(app).run_once(tenant_id=default_tenant)

            sched.register("mission_resume_every_minute", "* * * * *", _mission_resume_once)

        if default_tenant and os.getenv("KUN_MISSION_REAPER_ENABLED", "1") == "1":
            from datetime import timedelta

            from kun.engineering.mission_reaper import reap_stale_mission_tasks

            stale_after_sec = int(os.getenv("KUN_MISSION_REAPER_STALE_AFTER_SEC", "1800"))
            max_attempts = int(os.getenv("KUN_MISSION_REAPER_MAX_ATTEMPTS", "3"))

            async def _mission_reaper_once() -> None:
                await reap_stale_mission_tasks(
                    tenant_id=default_tenant,
                    stale_after=timedelta(seconds=stale_after_sec),
                    max_attempts=max_attempts,
                )

            sched.register("mission_reaper_every_5_minutes", "*/5 * * * *", _mission_reaper_once)

        if default_tenant and os.getenv("KUN_TASK_RESUME_WORKER_ENABLED", "1") == "1":
            from kun.api.runtime import get_pending_task_resume_worker

            async def _pending_task_resume_once() -> None:
                await get_pending_task_resume_worker(app).run_once(tenant_id=default_tenant)

            sched.register(
                "pending_task_resume_every_minute", "* * * * *", _pending_task_resume_once
            )

        # V2.3: 启 (Qi) cron — 启窗口内自动跑探索 (Darwin / AI Scientist /
        # PredictionTrainer). 默认装上 (KUN_QI_CRON_ENABLED=1), 但每次 tick 调用
        # 前会 require_qi_active 守门, 窗口外 skip.
        if (
            os.getenv("KUN_QI_CRON_ENABLED", "1") == "1"
            and default_tenant
            and getattr(app.state, "protocol_registry", None) is not None
        ):
            from kun.qi.cron_jobs import register_qi_cron_jobs

            register_qi_cron_jobs(sched, app, default_tenant)

        # V2.3: seed default protocols (5 stable starter protocols, 已存在则跳过)
        if (
            os.getenv("KUN_PROTOCOL_SEED_DEFAULTS", "1") == "1"
            and getattr(app.state, "protocol_registry", None) is not None
        ):
            try:
                from kun.qi.seed_protocols import seed_default_protocols

                seeded = await seed_default_protocols(app.state.protocol_registry)
                log.info("v23.protocol_seed.done", seeded=seeded)
            except Exception:
                log.exception("v23.protocol_seed.failed (non-fatal)")

        # V2.3: gauge metrics collector — 30s tick set qi_window_active /
        # pheromone_total_strength / capability_card_cache_hit_rate
        if default_tenant and os.getenv("KUN_V23_METRICS_COLLECTOR_ENABLED", "1") == "1":
            from kun.qi.metrics_collector import start_v23_metrics_collector

            app.state.v23_metrics_task = asyncio.create_task(
                start_v23_metrics_collector(app, default_tenant)
            )

        app.state.cron_scheduler_task = asyncio.create_task(sched.run_forever())
        log.info("cron_scheduler.started", jobs=sched.list_jobs())

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

    cron_task: asyncio.Task[None] | None = getattr(app.state, "cron_scheduler_task", None)
    if cron_task is not None:
        cron_sched = getattr(app.state, "cron_scheduler", None)
        if cron_sched is not None:
            cron_sched.stop()
        cron_task.cancel()
        with suppress(asyncio.CancelledError):
            await cron_task

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
    cfg = settings()
    auth_header = request.headers.get("Authorization")
    public_auth_paths = {
        "/api/auth/session/refresh",
        "/api/auth/signup",
        "/api/auth/invite/accept",
        "/api/auth/password/login",
    }
    if request.url.path in public_auth_paths and not auth_header:
        return await call_next(request)
    if auth_header:
        auth_secrets = cfg.auth_secret_candidates()
        if not auth_secrets:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "KUN_AUTH_SECRET or KUN_AUTH_SECRETS is required for bearer auth"
                },
            )
        from kun.security.auth import AuthTokenError, verify_bearer_token_any

        try:
            claims = verify_bearer_token_any(auth_header, auth_secrets)
        except AuthTokenError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        if claims.token_type == "refresh" and request.url.path != "/api/auth/session/refresh":
            return JSONResponse(
                status_code=401,
                content={"detail": "refresh token can only be used on session refresh endpoint"},
            )
        ctx = claims.to_tenant_context()
        if cfg.env == "production":
            try:
                revoked = await _check_and_record_auth_token(
                    tenant_id=ctx.tenant_id,
                    auth_header=auth_header,
                    ip_hash=_request_ip_hash(request),
                    user_agent=request.headers.get("User-Agent"),
                )
            except Exception as exc:
                return JSONResponse(
                    status_code=503,
                    content={"detail": f"token revocation check failed: {exc}"},
                )
            if revoked:
                return JSONResponse(status_code=401, content={"detail": "bearer token revoked"})
    elif cfg.env == "production":
        return JSONResponse(
            status_code=401,
            content={"detail": "Authorization: Bearer token is required in production"},
        )
    else:
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
        audience = (
            raw_audience if raw_audience in {"novice", "developer", "expert"} else "developer"
        )
        ctx = TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            scopes=scopes,
            audience=audience,  # type: ignore[arg-type]
        )
    with tenant_scope(ctx):
        return await call_next(request)


async def _check_and_record_auth_token(
    *,
    tenant_id: str,
    auth_header: str,
    ip_hash: str | None,
    user_agent: str | None,
) -> bool:
    from kun.core.db import session_scope
    from kun.ops.account_registry import hash_bearer_token, is_token_revoked, record_token_usage
    from kun.security.auth import extract_bearer_token

    token_hash = hash_bearer_token(extract_bearer_token(auth_header))
    async with session_scope(tenant_id=tenant_id) as s:
        revoked = await is_token_revoked(s, tenant_id=tenant_id, token_hash=token_hash)
        if not revoked:
            await record_token_usage(
                s,
                tenant_id=tenant_id,
                token_hash=token_hash,
                ip_hash=ip_hash,
                user_agent=user_agent,
            )
        return revoked


def _request_ip_hash(request: Request) -> str | None:
    raw_ip = request.client.host if request.client else ""
    cleaned = raw_ip.strip()
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(billing_router, prefix="/api/billing", tags=["billing"])
app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(code_capability_router)
app.include_router(compiler_router)
app.include_router(session_router)
app.include_router(ws_router)
app.include_router(nuo_router, prefix="/nuo", tags=["nuo"])
# V2.1 wire: 黑板 + 注意力 pin + task control (kill switch / timeout)
app.include_router(blackboard_router)
app.include_router(attention_pin_router)
app.include_router(task_control_router)
app.include_router(graph_router)
app.include_router(lab_router)
app.include_router(missions_router)
app.include_router(protocols_router)
app.include_router(qi_router)
# V2.1 §10.6 / M3.2 提前: 傩诊断
app.include_router(diagnose_router, prefix="/api/diagnose", tags=["diagnose"])


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "kun", "version": __version__}
