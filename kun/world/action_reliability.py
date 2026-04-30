"""World action reliability review.

傩用它定期看外部动作执行账本: 哪些能安全重试, 哪些必须人工补偿,
哪些只是正常完成。这里不直接重发外部动作, 避免重复邮件/重复 API 写入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select

from kun.core.db import session_scope
from kun.core.orm import WorldActionExecutionRow

ReliabilityAction = Literal["none", "review_retry", "review_compensation", "investigate"]


class WorldActionReliabilityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    task_ref: str
    action_type: str
    status: str
    attempt_count: int
    handler_id: str | None = None
    external_dispatched: bool = False
    requires_handler: bool = False
    can_auto_retry: bool = False
    requires_human_confirmation: bool = True
    recommended_action: ReliabilityAction = "none"
    reason: str = ""
    compensation_status: str = "not_needed"
    retry_status: str = "not_needed"
    idempotency_status: str = "unknown"
    last_error: str = ""
    updated_at: datetime | None = None


class WorldActionReliabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    items: list[WorldActionReliabilityItem]
    summary: dict[str, int]


async def collect_world_action_reliability(
    *,
    tenant_id: str,
    limit: int = 50,
) -> WorldActionReliabilityReport:
    """Load recent world-action executions and classify follow-up needs."""

    safe_limit = max(1, min(int(limit), 200))
    async with session_scope(tenant_id=tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(WorldActionExecutionRow)
                    .where(WorldActionExecutionRow.tenant_id == tenant_id)
                    .order_by(desc(WorldActionExecutionRow.updated_at))
                    .limit(safe_limit)
                )
            )
            .scalars()
            .all()
        )
    items = reliability_items_from_rows(rows)
    return WorldActionReliabilityReport(
        tenant_id=tenant_id,
        items=items,
        summary=summarize_reliability(items),
    )


def reliability_items_from_rows(rows: list[Any]) -> list[WorldActionReliabilityItem]:
    return [_item_from_row(row) for row in rows]


def summarize_reliability(items: list[WorldActionReliabilityItem]) -> dict[str, int]:
    summary = {
        "total": len(items),
        "needs_retry_review": 0,
        "needs_compensation_review": 0,
        "needs_investigation": 0,
        "auto_retry_allowed": 0,
    }
    for item in items:
        if item.recommended_action == "review_retry":
            summary["needs_retry_review"] += 1
        if item.recommended_action == "review_compensation":
            summary["needs_compensation_review"] += 1
        if item.recommended_action == "investigate":
            summary["needs_investigation"] += 1
        if item.can_auto_retry:
            summary["auto_retry_allowed"] += 1
    return summary


def _item_from_row(row: Any) -> WorldActionReliabilityItem:
    status = str(row.status or "")
    external_dispatched = bool(row.external_dispatched)
    requires_handler = bool(row.requires_handler)
    retry_policy = str(row.retry_policy or "")
    compensation_strategy = str(row.compensation_strategy or "")
    idempotency_key = str(row.idempotency_key or "")

    compensation_status = _compensation_status(
        status=status,
        external_dispatched=external_dispatched,
        compensation_strategy=compensation_strategy,
    )
    retry_status, can_auto_retry = _retry_status(
        status=status,
        external_dispatched=external_dispatched,
        retry_policy=retry_policy,
        attempt_count=int(row.attempt_count or 0),
    )
    idempotency_status = _idempotency_status(idempotency_key, str(row.action_id))
    recommended_action, reason = _recommendation(
        status=status,
        requires_handler=requires_handler,
        compensation_status=compensation_status,
        retry_status=retry_status,
        last_error=str(row.last_error or ""),
    )
    return WorldActionReliabilityItem(
        action_id=str(row.action_id),
        task_ref=str(row.task_ref),
        action_type=str(row.action_type),
        status=status,
        attempt_count=int(row.attempt_count or 0),
        handler_id=getattr(row, "handler_id", None),
        external_dispatched=external_dispatched,
        requires_handler=requires_handler,
        can_auto_retry=can_auto_retry,
        requires_human_confirmation=not can_auto_retry or external_dispatched,
        recommended_action=recommended_action,
        reason=reason,
        compensation_status=compensation_status,
        retry_status=retry_status,
        idempotency_status=idempotency_status,
        last_error=str(row.last_error or ""),
        updated_at=getattr(row, "updated_at", None),
    )


def _retry_status(
    *,
    status: str,
    external_dispatched: bool,
    retry_policy: str,
    attempt_count: int,
) -> tuple[str, bool]:
    if status not in {"failed", "blocked", "cancelled"}:
        return "not_needed", False
    if external_dispatched:
        return "manual_only_external_dispatched", False
    lowered = retry_policy.lower()
    if "不自动重试" in retry_policy or "manual" in lowered or "人工" in retry_policy:
        return "manual_only_policy", False
    if attempt_count >= 3:
        return "manual_only_attempt_limit", False
    if "auto" in lowered or "自动重试" in retry_policy:
        return "auto_allowed", True
    return "review_required", False


def _compensation_status(
    *,
    status: str,
    external_dispatched: bool,
    compensation_strategy: str,
) -> str:
    if not external_dispatched:
        return "not_needed"
    if status == "executed":
        return "available" if _has_clear_compensation(compensation_strategy) else "weak"
    return "needed" if _has_clear_compensation(compensation_strategy) else "missing"


def _idempotency_status(idempotency_key: str, action_id: str) -> str:
    if not idempotency_key:
        return "missing"
    if idempotency_key == action_id:
        return "weak_action_id_only"
    return "present"


def _recommendation(
    *,
    status: str,
    requires_handler: bool,
    compensation_status: str,
    retry_status: str,
    last_error: str,
) -> tuple[ReliabilityAction, str]:
    if requires_handler:
        return "investigate", "缺少真实执行器；先补 handler 或改用已支持动作。"
    if compensation_status in {"missing", "needed"}:
        return "review_compensation", "外部动作可能已影响真实世界，需要人工确认补偿方案。"
    if retry_status in {"auto_allowed", "review_required", "manual_only_policy"}:
        return "review_retry", _retry_reason(retry_status)
    if status in {"failed", "blocked", "cancelled"}:
        return "investigate", last_error or "动作没有完成，需要人工查看失败原因。"
    return "none", "外部动作账本正常。"


def _retry_reason(retry_status: str) -> str:
    if retry_status == "auto_allowed":
        return "该动作没有外发且策略允许自动重试，可进入安全重试队列。"
    if retry_status == "manual_only_policy":
        return "该动作失败但策略要求人工确认后再重试，避免重复副作用。"
    return "该动作失败且未外发，可人工确认是否重试。"


def _has_clear_compensation(strategy: str) -> bool:
    text = strategy.strip().lower()
    if not text:
        return False
    weak_markers = (
        "无法自动撤回",
        "depends",
        "人工确认",
        "manual",
        "取决于",
        "cannot_recall",
    )
    return not any(marker in text for marker in weak_markers)


__all__ = [
    "WorldActionReliabilityItem",
    "WorldActionReliabilityReport",
    "collect_world_action_reliability",
    "reliability_items_from_rows",
    "summarize_reliability",
]
