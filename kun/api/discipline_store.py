"""V7 Phase X.O — process-local discipline report cache.

The X.O self-audit found that ``kun/api/cockpit.py:/discipline/recent``
returned hardcoded ``[]`` even when the discipline enforcer was actually
firing (X.I-0a). Reports landed in event emissions but had no persistence
path → users never saw them.

This module is the minimal fix: a process-local thread-safe in-memory
cache that LongTaskOrchestrator pushes to and the cockpit reader reads
from. Future X.P upgrade can move this to PG; for now the WS process
caches its own recent reports so the cockpit panel shows truth instead
of an empty list.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.api.discipline_store")

_LOCK = threading.Lock()
_MAX_CACHE = 200

# Hold strong refs to in-flight write-through tasks so the event loop does
# not garbage-collect them before they finish (RUF006).
_PENDING_PG_WRITES: set[Any] = set()


@dataclass(frozen=True)
class DisciplineReportEntry:
    """V7 §13.6 frozen IO — one captured report for the cockpit reader."""

    task_id: str
    captured_at: str  # ISO-8601 UTC
    overall_score: float
    n_total: int
    n_passed: int
    failed_disciplines: list[str] = field(default_factory=list)


_CACHE: deque[DisciplineReportEntry] = deque(maxlen=_MAX_CACHE)


def record_discipline_report(
    *,
    task_id: str,
    overall_score: float,
    n_total: int,
    n_passed: int,
    failed_disciplines: list[str],
    tenant_id: str = "default",
) -> None:
    """Append a discipline report to the process-local cache AND PG (X.S).

    Called from LongTaskOrchestrator when it emits
    ``long_task.discipline_report``. Thread-safe via a single shared lock.
    The PG write (X.S) is best-effort write-through: failures fall back
    to the in-memory cache (which still serves the cockpit reader).
    """
    entry = DisciplineReportEntry(
        task_id=task_id,
        captured_at=datetime.now(UTC).isoformat(),
        overall_score=overall_score,
        n_total=n_total,
        n_passed=n_passed,
        failed_disciplines=list(failed_disciplines or []),
    )
    with _LOCK:
        _CACHE.append(entry)
    # X.S — best-effort PG write-through (survives multi-process deploys).
    _write_pg_report(tenant_id=tenant_id, entry=entry)


async def _record_pg(tenant_id: str, entry: DisciplineReportEntry) -> None:
    from kun.core.db import session_scope
    from kun.core.ids import new_id
    from kun.core.orm import EngineeringDisciplineReportRow

    async with session_scope(tenant_id=tenant_id) as s:
        s.add(
            EngineeringDisciplineReportRow(
                tenant_id=tenant_id,
                report_id=new_id("discipline_report"),  # dr- prefix (X.S)
                task_id=entry.task_id,
                overall_score=entry.overall_score,
                n_total=entry.n_total,
                n_passed=entry.n_passed,
                failed_disciplines=list(entry.failed_disciplines),
            )
        )
        await s.flush()


def _write_pg_report(*, tenant_id: str, entry: DisciplineReportEntry) -> None:
    """Schedule the PG write without blocking the caller. If no event loop
    is running (sync context / tests), skip PG and rely on the cache."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop — cache-only (sync caller / unit test)

    async def _safe() -> None:
        try:
            await _record_pg(tenant_id, entry)
        except Exception as e:  # pragma: no cover — best-effort
            log.warning(
                "discipline_store.pg_write_failed",
                task_id=entry.task_id,
                error=f"{type(e).__name__}: {e}",
            )

    task = loop.create_task(_safe())
    _PENDING_PG_WRITES.add(task)
    task.add_done_callback(_PENDING_PG_WRITES.discard)


def list_recent_discipline_reports(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent reports, newest first, capped at limit (1-100).

    Reads the process-local cache (L1). For cross-process history use
    :func:`list_recent_discipline_reports_pg` (async, reads PG).
    """
    limit = max(1, min(100, limit))
    with _LOCK:
        snapshot = list(_CACHE)
    snapshot.reverse()
    return [asdict(e) for e in snapshot[:limit]]


async def list_recent_discipline_reports_pg(
    *, tenant_id: str = "default", limit: int = 20
) -> list[dict[str, Any]]:
    """Read recent reports from PG (X.S — cross-process durable view).

    Falls back to the in-memory cache if PG is unreachable so the cockpit
    never shows an empty table when reports exist in-process.
    """
    limit = max(1, min(100, limit))
    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import EngineeringDisciplineReportRow

        async with session_scope(tenant_id=tenant_id) as s:
            stmt = (
                select(EngineeringDisciplineReportRow)
                .where(EngineeringDisciplineReportRow.tenant_id == tenant_id)
                .order_by(EngineeringDisciplineReportRow.captured_at.desc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "task_id": r.task_id,
                "captured_at": r.captured_at.isoformat(),
                "overall_score": float(r.overall_score),
                "n_total": int(r.n_total),
                "n_passed": int(r.n_passed),
                "failed_disciplines": list(r.failed_disciplines or []),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning(
            "discipline_store.pg_read_failed_falling_back_to_cache",
            tenant_id=tenant_id,
            error=f"{type(e).__name__}: {e}",
        )
        return list_recent_discipline_reports(limit=limit)


def reset_for_tests() -> None:
    """Test helper — clears the cache between tests."""
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "DisciplineReportEntry",
    "list_recent_discipline_reports",
    "record_discipline_report",
    "reset_for_tests",
]
