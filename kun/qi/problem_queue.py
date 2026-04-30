"""Qi problem queue — 让“启”从真实问题里学习，而不是凭空自嗨。

这不是生产级队列，先做进程内版本。核心价值是把傩/守望/WorldGateway
发现的问题变成启的探索输入：哪里真的卡、哪里真的风险高、哪里真的值得优化。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

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


_queue: QiProblemQueue | None = None


def get_qi_problem_queue() -> QiProblemQueue:
    global _queue
    if _queue is None:
        _queue = QiProblemQueue()
    return _queue


def reset_qi_problem_queue() -> None:
    global _queue
    _queue = None


async def collect_problem_signals(tenant_id: str) -> list[QiProblemSignal]:
    """从当前系统状态采样真实问题，供启夜间探索使用。"""
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
    return signals


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


__all__ = [
    "QiProblemQueue",
    "QiProblemSignal",
    "collect_problem_signals",
    "get_qi_problem_queue",
    "prompt_for_problem",
    "reset_qi_problem_queue",
]
