"""Handler registry. Handlers implement actions invoked by rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.config import settings
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.orm import EventRow, ProactiveMissRow
from kun.core.tenancy import MissingTenantContextError, current_tenant

log = get_logger("kun.watchtower.handlers")

HandlerContext = dict[str, Any]
HandlerFunc = Callable[[HandlerContext, dict[str, Any]], Awaitable[None]]

_registry: dict[str, HandlerFunc] = {}


def register_handler(name: str) -> Callable[[HandlerFunc], HandlerFunc]:
    """Decorator to register a handler by name.

    Usage:
        @register_handler("pause_task")
        async def _handle(ctx, params):
            ...
    """

    def _decorator(fn: HandlerFunc) -> HandlerFunc:
        if name in _registry:
            log.warning("handler.override", name=name)
        _registry[name] = fn
        return fn

    return _decorator


def get_handler(name: str) -> HandlerFunc | None:
    return _registry.get(name)


def list_handlers() -> list[str]:
    return sorted(_registry)


async def handle_tool_skipped(row: EventRow) -> None:
    """把 task.tool_skipped 事件累积成 proactive trigger 学习信号。"""
    if row.event_type != "task.tool_skipped":
        return

    from kun.core.db import session_scope
    from kun.datamodel.events import Event

    tenant_id = row.tenant_id
    missed = row.payload.get("missed") if isinstance(row.payload, dict) else None
    if not isinstance(missed, list):
        return

    threshold = settings().missed_tool_threshold
    async with session_scope(tenant_id=tenant_id) as s:
        for raw in missed:
            miss = _coerce_tool_miss(raw)
            if miss is None:
                continue
            promoted_count = await _record_proactive_miss(
                s,
                tenant_id=tenant_id,
                skill_id=miss["skill_id"],
                pattern=miss["pattern"],
                reason=miss["reason"],
                trigger_source=miss["trigger_source"],
                threshold=threshold,
            )
            if promoted_count is None:
                continue
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="proactive.trigger_promoted",
                    payload={
                        "skill_id": miss["skill_id"],
                        "pattern": miss["pattern"],
                        "miss_count": promoted_count,
                        "threshold": threshold,
                        "trigger_source": miss["trigger_source"],
                    },
                    task_ref=row.task_ref,
                    causation_event_id=row.event_id,
                ),
            )
            log.info(
                "proactive.trigger_promoted",
                tenant_id=tenant_id,
                skill_id=miss["skill_id"],
                miss_count=promoted_count,
            )


def _coerce_tool_miss(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    skill_id = str(raw.get("skill_id") or "").strip()
    pattern = str(raw.get("pattern") or "").strip()[:512]
    if not skill_id or not pattern:
        return None
    return {
        "skill_id": skill_id[:128],
        "pattern": pattern,
        "reason": str(raw.get("reason") or "")[:500],
        "trigger_source": str(raw.get("trigger_source") or "")[:64],
    }


async def _record_proactive_miss(
    s: AsyncSession,
    *,
    tenant_id: str,
    skill_id: str,
    pattern: str,
    reason: str,
    trigger_source: str,
    threshold: int,
) -> int | None:
    now = datetime.now(UTC)
    stmt = pg_insert(ProactiveMissRow).values(
        tenant_id=tenant_id,
        skill_id=skill_id,
        pattern=pattern,
        miss_count=1,
        last_reason=reason,
        trigger_source=trigger_source,
        last_missed_at=now,
    )
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=[
            ProactiveMissRow.tenant_id,
            ProactiveMissRow.skill_id,
            ProactiveMissRow.pattern,
        ],
        set_={
            "miss_count": ProactiveMissRow.miss_count + 1,
            "last_reason": reason,
            "trigger_source": trigger_source,
            "last_missed_at": now,
        },
    ).returning(ProactiveMissRow.miss_count)
    miss_count = int((await s.execute(upsert_stmt)).scalar_one())
    if miss_count < threshold:
        return None

    promoted = (
        await s.execute(
            update(ProactiveMissRow)
            .where(
                ProactiveMissRow.tenant_id == tenant_id,
                ProactiveMissRow.skill_id == skill_id,
                ProactiveMissRow.pattern == pattern,
                ProactiveMissRow.promoted_at.is_(None),
            )
            .values(promoted_at=now)
            .returning(ProactiveMissRow.miss_count)
        )
    ).scalar_one_or_none()
    return int(promoted) if promoted is not None else None


# ================ Built-in handlers ================


@register_handler("log")
async def _log(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Simple logging handler (also serves as default/smoke)."""
    log.info(
        "watchtower.action.log",
        rule=ctx.get("rule_id"),
        event_type=ctx.get("event_type"),
        message=params.get("message", ""),
    )


@register_handler("pause_task")
async def _pause_task(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Pause a running task (via RuntimeState update)."""
    task_ref = ctx.get("task_ref")
    if not task_ref:
        log.warning("pause_task.no_task_ref", ctx=ctx)
        return
    from sqlalchemy import update

    from kun.core.db import session_scope
    from kun.core.orm import RuntimeStateRow

    async with session_scope() as s:
        await s.execute(
            update(RuntimeStateRow)
            .where(RuntimeStateRow.task_ref == task_ref)
            .values(status="paused")
        )
    log.info("watchtower.action.pause_task", task_ref=task_ref)


@register_handler("notify_user")
async def _notify_user(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Emit a notification through NotificationLayer."""
    from kun.core.db import session_scope
    from kun.core.orm import NotificationRow
    from kun.datamodel.notification import Notification

    tenant_id = _tenant_id_from_ctx(ctx)
    template = params.get("template", "generic")
    kind = params.get("kind", "alert")
    severity = params.get("severity", "warn")

    notif = Notification(
        tenant_id=tenant_id,
        kind=kind,
        severity=severity,
        channel=params.get("channel", "side"),
        title=str(params.get("title", template)),
        body=str(params.get("body", "")),
        payload=ctx,
        task_ref=ctx.get("task_ref"),
        causation_event_id=ctx.get("event_id"),
    )
    async with session_scope() as s:
        s.add(
            NotificationRow(
                notification_id=notif.notification_id,
                tenant_id=notif.tenant_id,
                kind=notif.kind,
                severity=notif.severity,
                channel=notif.channel,
                title=notif.title,
                body=notif.body,
                payload=notif.payload,
                render_hint=notif.render_hint,
                task_ref=notif.task_ref,
                causation_event_id=notif.causation_event_id,
                created_at=notif.created_at,
            )
        )
    log.info("watchtower.action.notify_user", notification_id=notif.notification_id, kind=kind)


def _tenant_id_from_ctx(ctx: HandlerContext) -> str:
    tenant_id = ctx.get("tenant_id")
    if tenant_id:
        return str(tenant_id)
    try:
        return current_tenant().tenant_id
    except MissingTenantContextError:
        return "unknown"


@register_handler("escalate_human")
async def _escalate_human(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Level-4 escalation to human (§6.2 分级自治). Emits a high-priority notif."""
    params = {**params, "severity": "error", "kind": "alert", "channel": "main"}
    await _notify_user(ctx, params)


@register_handler("rollback_version")
async def _rollback_version(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Flip an experiment pointer to previous version."""
    from sqlalchemy import update

    from kun.core.db import session_scope
    from kun.core.orm import ExperimentRow

    experiment_id = params.get("experiment_id") or ctx.get("experiment_id")
    if not experiment_id:
        log.warning("rollback_version.no_id")
        return
    async with session_scope() as s:
        await s.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id)
            .values(status="rolled_back", rollout_percent=0)
        )
    log.info("watchtower.action.rollback", experiment_id=experiment_id)


@register_handler("cache_ttl_escalate")
async def _cache_ttl_escalate(ctx: HandlerContext, params: dict[str, Any]) -> None:
    """Switch prompt cache tier to extended 1-hour beta (ADR-016)."""
    log.info("watchtower.action.cache_ttl_escalate", tier=params.get("tier", "stable"))
    # Real impl would flip a config flag; for now we just emit.
