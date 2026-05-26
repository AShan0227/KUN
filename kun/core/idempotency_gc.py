"""Idempotency-key garbage collection worker.

`idempotency_keys` rows have a per-row `ttl_sec` column but no automatic
expiry. Without this worker, replaying a fingerprint forever returns the
stale result and the table grows unbounded.

Runs every `interval_sec` (default 3600s). Each pass deletes rows whose
`created_at + ttl_sec` is in the past. Uses the admin DSN — the cleanup
is system-wide and must bypass per-tenant RLS.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, text

from kun.core.db import get_admin_sessionmaker
from kun.core.logging import get_logger
from kun.core.orm import IdempotencyRow

log = get_logger("kun.idempotency_gc")


async def expire_once() -> int:
    """Delete expired idempotency rows once. Returns the number removed."""
    Session = get_admin_sessionmaker()
    async with Session() as s:
        # Use SQL math on (created_at + ttl_sec seconds) so the comparison
        # happens server-side without pulling rows into Python.
        result = await s.execute(
            delete(IdempotencyRow).where(
                text("created_at + (ttl_sec * interval '1 second') < now()")
            )
        )
        await s.commit()
        deleted = result.rowcount or 0
        if deleted:
            log.info("idempotency_gc.expired", count=deleted)
        return int(deleted)


async def idempotency_gc_worker(*, interval_sec: int = 3600) -> None:
    """Periodically drop expired idempotency keys."""
    log.info("idempotency_gc.worker.start", interval_sec=interval_sec)
    while True:
        try:
            await expire_once()
        except Exception as e:
            log.warning("idempotency_gc.tick_failed", error=str(e))
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            log.info("idempotency_gc.worker.stop")
            raise
