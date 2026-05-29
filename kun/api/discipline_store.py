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

_LOCK = threading.Lock()
_MAX_CACHE = 200


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
) -> None:
    """Append a discipline report to the process-local cache.

    Called from LongTaskOrchestrator when it emits
    ``long_task.discipline_report``. Thread-safe via a single shared lock.
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


def list_recent_discipline_reports(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent reports, newest first, capped at limit (1-100)."""
    limit = max(1, min(100, limit))
    with _LOCK:
        snapshot = list(_CACHE)
    snapshot.reverse()
    return [asdict(e) for e in snapshot[:limit]]


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
