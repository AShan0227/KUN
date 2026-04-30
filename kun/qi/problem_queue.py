"""Qi problem queue — 让“启”从真实问题里学习，而不是凭空自嗨。

这不是生产级队列，先做进程内版本。核心价值是把傩/守望/WorldGateway
发现的问题变成启的探索输入：哪里真的卡、哪里真的风险高、哪里真的值得优化。
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

ProblemCategory = Literal[
    "world_gateway",
    "runtime",
    "cost",
    "risk",
    "delivery",
    "context",
    "memory",
    "unknown",
]


class QiProblemSignal(BaseModel):
    signal_id: str
    tenant_id: str
    category: ProblemCategory = "unknown"
    severity: str = "info"
    summary: str
    source: str = ""
    task_type: str = "general"
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def build(
        cls,
        *,
        tenant_id: str,
        category: ProblemCategory,
        summary: str,
        severity: str = "info",
        source: str = "",
        task_type: str = "general",
        evidence: dict[str, Any] | None = None,
    ) -> QiProblemSignal:
        key = "|".join([tenant_id, category, severity, source, task_type, summary])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return cls(
            signal_id=f"qps_{digest}",
            tenant_id=tenant_id,
            category=category,
            severity=severity,
            summary=summary,
            source=source,
            task_type=task_type,
            evidence=evidence or {},
        )


class QiProblemQueue:
    """Small in-memory queue with dedupe."""

    def __init__(self) -> None:
        self._signals: dict[str, dict[str, QiProblemSignal]] = defaultdict(dict)

    def enqueue(self, signal: QiProblemSignal) -> None:
        self._signals[signal.tenant_id][signal.signal_id] = signal

    def enqueue_many(self, signals: list[QiProblemSignal]) -> int:
        before = sum(len(v) for v in self._signals.values())
        for signal in signals:
            self.enqueue(signal)
        after = sum(len(v) for v in self._signals.values())
        return max(0, after - before)

    def list(self, tenant_id: str, *, limit: int = 20) -> list[QiProblemSignal]:
        signals = list(self._signals.get(tenant_id, {}).values())
        signals.sort(
            key=lambda item: (_severity_rank(item.severity), item.created_at), reverse=True
        )
        return signals[:limit]

    def pick(self, tenant_id: str) -> QiProblemSignal | None:
        listed = self.list(tenant_id, limit=1)
        return listed[0] if listed else None

    def clear(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._signals.clear()
        else:
            self._signals.pop(tenant_id, None)


class AsyncQiProblemQueue(Protocol):
    """Async queue contract used by the optional SQL-backed implementation."""

    async def enqueue(self, signal: QiProblemSignal) -> None: ...

    async def enqueue_many(self, signals: list[QiProblemSignal]) -> int: ...

    async def list(self, tenant_id: str, *, limit: int = 20) -> list[QiProblemSignal]: ...

    async def pick(self, tenant_id: str) -> QiProblemSignal | None: ...

    async def clear(self, tenant_id: str | None = None) -> None: ...


class SqlQiProblemQueue:
    """Postgres-backed Qi problem queue.

    It only persists and dedupes real problem signals. Qi still needs a later
    consumer/experiment loop to turn a signal into a fix; this class deliberately
    does not claim auto-remediation.
    """

    async def enqueue(self, signal: QiProblemSignal) -> None:
        await self.enqueue_many([signal])

    async def enqueue_many(self, signals: list[QiProblemSignal]) -> int:
        if not signals:
            return 0

        from kun.core.db import session_scope

        inserted = 0
        by_tenant: dict[str, list[QiProblemSignal]] = defaultdict(list)
        for signal in signals:
            by_tenant[signal.tenant_id].append(signal)

        now = datetime.now(UTC)
        for tenant_id, tenant_signals in by_tenant.items():
            async with session_scope(tenant_id=tenant_id) as session:
                for signal in tenant_signals:
                    result = await session.execute(_upsert_problem_signal_stmt(signal, now))
                    if bool(result.scalar_one()):
                        inserted += 1
        return inserted

    async def list(self, tenant_id: str, *, limit: int = 20) -> list[QiProblemSignal]:
        from kun.core.db import session_scope
        from kun.core.orm import QiProblemSignalRow

        async with session_scope(tenant_id=tenant_id) as session:
            result = await session.execute(
                select(QiProblemSignalRow)
                .where(
                    QiProblemSignalRow.tenant_id == tenant_id,
                    QiProblemSignalRow.status == "open",
                )
                .order_by(QiProblemSignalRow.last_seen_at.desc())
                .limit(max(limit * 5, limit))
            )
            rows = list(result.scalars().all())

        signals = [_row_to_signal(row) for row in rows]
        signals.sort(
            key=lambda item: (_severity_rank(item.severity), item.created_at), reverse=True
        )
        return signals[:limit]

    async def pick(self, tenant_id: str) -> QiProblemSignal | None:
        listed = await self.list(tenant_id, limit=1)
        return listed[0] if listed else None

    async def clear(self, tenant_id: str | None = None) -> None:
        from kun.core.db import session_scope
        from kun.core.orm import QiProblemSignalRow

        async with session_scope(tenant_id=tenant_id) as session:
            stmt = delete(QiProblemSignalRow)
            if tenant_id is not None:
                stmt = stmt.where(QiProblemSignalRow.tenant_id == tenant_id)
            await session.execute(stmt)


_queue: QiProblemQueue | None = None
_sql_queue: SqlQiProblemQueue | None = None


def get_qi_problem_queue() -> QiProblemQueue:
    global _queue
    if _queue is None:
        _queue = QiProblemQueue()
    return _queue


def get_sql_qi_problem_queue() -> SqlQiProblemQueue:
    global _sql_queue
    if _sql_queue is None:
        _sql_queue = SqlQiProblemQueue()
    return _sql_queue


def get_configured_qi_problem_queue() -> QiProblemQueue | SqlQiProblemQueue:
    """Return the queue Qi producers and consumers should share."""

    if _durable_problem_queue_enabled():
        return get_sql_qi_problem_queue()
    return get_qi_problem_queue()


def reset_qi_problem_queue() -> None:
    global _queue, _sql_queue
    _queue = None
    _sql_queue = None


async def collect_problem_signals(tenant_id: str) -> list[QiProblemSignal]:
    """从当前系统状态采样真实问题，供启夜间探索使用.

    When the optional SQL queue is enabled, sampled signals are also upserted
    into Postgres so a restart does not erase the problem backlog. The function
    still returns the signals for the existing in-memory queue path.
    """
    signals: list[QiProblemSignal] = []
    try:
        from kun.engineering.nuo_system_health import collect_system_health_report

        report = await collect_system_health_report(tenant_id=tenant_id)
        for finding in report.findings:
            signals.append(
                QiProblemSignal.build(
                    tenant_id=tenant_id,
                    category=_category_from_finding(finding.subsystem),
                    severity=finding.severity,
                    summary=finding.title,
                    source="nuo.system_health",
                    evidence=finding.model_dump(mode="json"),
                )
            )
    except Exception:
        return signals
    await persist_problem_signals(signals)
    return signals


async def persist_problem_signals(signals: list[QiProblemSignal]) -> int:
    """Best-effort durable upsert, falling back to the in-memory queue."""
    if not signals:
        return 0
    if _durable_problem_queue_enabled():
        try:
            return await get_sql_qi_problem_queue().enqueue_many(signals)
        except Exception:
            pass
    return get_qi_problem_queue().enqueue_many(signals)


def prompt_for_problem(signal: QiProblemSignal) -> str:
    """把真实问题压成启可探索的 prompt。"""
    return (
        "KUN 发现了一个真实系统问题，请提出可验证的改进协议。\n"
        f"问题类型: {signal.category}\n"
        f"严重级别: {signal.severity}\n"
        f"来源: {signal.source}\n"
        f"摘要: {signal.summary}\n"
        "要求: 给出最小可落地改动、验证方式、回滚条件，不能只写泛泛建议。"
    )


def _severity_rank(severity: str) -> int:
    return {"critical": 4, "error": 3, "warning": 2, "warn": 2, "info": 1}.get(
        severity,
        0,
    )


def _category_from_finding(category: str) -> ProblemCategory:
    if category in {"world_gateway", "runtime", "cost", "risk", "delivery", "context", "memory"}:
        return cast(ProblemCategory, category)
    if "world" in category or "handler" in category:
        return "world_gateway"
    if "runtime" in category or "mission" in category:
        return "runtime"
    if "cost" in category or "budget" in category:
        return "cost"
    if "delivery" in category:
        return "delivery"
    return "unknown"


def _durable_problem_queue_enabled() -> bool:
    return os.getenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _upsert_problem_signal_stmt(signal: QiProblemSignal, now: datetime) -> Any:
    from kun.core.orm import QiProblemSignalRow

    stmt = insert(QiProblemSignalRow).values(
        tenant_id=signal.tenant_id,
        signal_id=signal.signal_id,
        category=signal.category,
        severity=signal.severity,
        summary=signal.summary,
        source=signal.source,
        task_type=signal.task_type,
        status="open",
        evidence=signal.evidence,
        occurrence_count=1,
        created_at=signal.created_at,
        last_seen_at=now,
        updated_at=now,
    )
    return stmt.on_conflict_do_update(
        index_elements=[QiProblemSignalRow.tenant_id, QiProblemSignalRow.signal_id],
        set_={
            "category": stmt.excluded.category,
            "severity": stmt.excluded.severity,
            "summary": stmt.excluded.summary,
            "source": stmt.excluded.source,
            "task_type": stmt.excluded.task_type,
            "status": "open",
            "evidence": stmt.excluded.evidence,
            "occurrence_count": QiProblemSignalRow.occurrence_count + 1,
            "last_seen_at": now,
            "updated_at": now,
        },
    ).returning(QiProblemSignalRow.occurrence_count == 1)


def _row_to_signal(row: Any) -> QiProblemSignal:
    return QiProblemSignal(
        signal_id=row.signal_id,
        tenant_id=row.tenant_id,
        category=_category_from_finding(str(row.category)),
        severity=str(row.severity),
        summary=str(row.summary),
        source=str(row.source or ""),
        task_type=str(row.task_type or "general"),
        evidence=dict(row.evidence or {}),
        created_at=row.created_at,
    )


__all__ = [
    "AsyncQiProblemQueue",
    "QiProblemQueue",
    "QiProblemSignal",
    "SqlQiProblemQueue",
    "collect_problem_signals",
    "get_configured_qi_problem_queue",
    "get_qi_problem_queue",
    "get_sql_qi_problem_queue",
    "persist_problem_signals",
    "prompt_for_problem",
    "reset_qi_problem_queue",
]
