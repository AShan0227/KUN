"""Event bus service (ADR-005 Outbox pattern).

Write path:
    async with session_scope() as s:
        await emit(s, event)      # same txn as business writes

Read path (outbox poller):
    for row in await fetch_unpublished():
        await nats.publish(row.subject, row.event_id)
        mark_published(row.event_id)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.logging import get_logger
from kun.core.metrics import events_outbox_lag, events_published_total
from kun.core.orm import EventRow
from kun.datamodel.events import Event

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS

log = get_logger("kun.events")


async def emit(session: AsyncSession, event: Event) -> None:
    """Emit an event in the current transaction.

    The caller is responsible for session.commit(). If the transaction
    rolls back, the event is never written — exactly-once semantics
    with the business data.
    """
    row = EventRow(
        event_id=event.event_id,
        tenant_id=event.tenant_id,
        event_type=event.event_type,
        subject=event.subject,
        payload=event.payload,
        occurred_at=event.occurred_at,
        trace_id=event.trace_id,
        span_id=event.span_id,
        causation_event_id=event.causation_event_id,
        task_ref=event.task_ref,
    )
    session.add(row)
    log.debug("event.emitted", event_id=event.event_id, type=event.event_type)


async def fetch_unpublished(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[EventRow]:
    """Fetch oldest unpublished events."""
    stmt = (
        select(EventRow)
        .where(EventRow.published_at.is_(None))
        .order_by(EventRow.occurred_at)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def mark_published(session: AsyncSession, event_ids: list[str]) -> None:
    """Mark events as published."""
    if not event_ids:
        return
    from datetime import UTC, datetime

    stmt = (
        update(EventRow)
        .where(EventRow.event_id.in_(event_ids))
        .values(published_at=datetime.now(UTC))
    )
    await session.execute(stmt)


async def count_unpublished(session: AsyncSession) -> int:
    """Count events still waiting to publish — feeds metric."""
    from sqlalchemy import func

    stmt = select(func.count()).select_from(EventRow).where(EventRow.published_at.is_(None))
    result = await session.execute(stmt)
    return int(result.scalar_one())


# =============== NATS integration ===============


@asynccontextmanager
async def nats_client() -> AsyncIterator[NATS | None]:
    """Yield a NATS client or None if unavailable (dev fallback)."""
    try:
        import nats
    except ImportError:
        log.warning("nats.unavailable", reason="module not installed")
        yield None
        return

    from kun.core.config import settings

    nc = None
    try:
        nc = await nats.connect(settings().nats_url)
        yield nc
    except Exception as e:
        log.warning("nats.connection_failed", error=str(e))
        yield None
    finally:
        if nc is not None:
            await nc.drain()


async def publish_to_nats(nc: NATS | None, event: EventRow) -> bool:
    """Publish an event to NATS. Returns True on success."""
    if nc is None:
        return False
    try:
        await nc.publish(
            event.subject,
            event.event_id.encode("utf-8"),
            headers={"event_id": event.event_id, "event_type": event.event_type},
        )
        events_published_total.labels(event_type=event.event_type).inc()
        return True
    except Exception as e:
        log.warning("nats.publish_failed", event_id=event.event_id, error=str(e))
        return False


async def outbox_worker(*, interval_sec: float = 0.5) -> None:
    """Long-running worker: poll outbox → publish to NATS → mark published.

    Start this as a background task on app startup.
    """
    from kun.core.db import session_scope

    log.info("outbox.worker.started", interval_sec=interval_sec)
    async with nats_client() as nc:
        while True:
            try:
                async with session_scope() as s:
                    rows = await fetch_unpublished(s, limit=100)
                    lag = await count_unpublished(s)
                    events_outbox_lag.set(lag)

                    published_ids: list[str] = []
                    for row in rows:
                        if await publish_to_nats(nc, row):
                            published_ids.append(row.event_id)

                    if published_ids:
                        await mark_published(s, published_ids)
                        log.debug("outbox.published", count=len(published_ids))
            except Exception as e:
                log.exception("outbox.worker.error", error=str(e))
            await asyncio.sleep(interval_sec)
