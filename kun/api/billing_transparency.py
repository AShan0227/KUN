"""Billing transparency API — 计费透明承诺.

这版先提供稳定 API 契约和内存审计账本, 后续可替换成真实 billing table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from kun.core.tenancy import current_tenant

router = APIRouter()


class BillingPromise(BaseModel):
    version: str = "ADR-022"
    effective_at: datetime
    commitments: list[str]
    small_talk_free_rule: str
    refund_rule: str
    notice_window_days: int = 30


class BillingAuditEntry(BaseModel):
    entry_id: str
    tenant_id: str
    user_id: str
    occurred_at: datetime
    kind: Literal["charge", "refund", "credit", "adjustment"]
    amount_usd: float
    saved_usd: float = 0.0
    reason: str
    task_id: str | None = None
    reversible: bool = True
    refund_eligible: bool = True


class RefundRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)


class RefundResponse(BaseModel):
    request_id: str
    status: Literal["received"]
    amount_usd: float
    message: str


class UpcomingBillingChange(BaseModel):
    change_id: str
    title: str
    effective_at: datetime
    impact_summary: str


class BillingDashboard(BaseModel):
    tenant_id: str
    user_id: str
    used_today: float
    used_month: float
    saved_by_kun: float
    refundable_balance: float
    audit_entry_count: int
    upcoming_change_count: int


_AUDIT_LOG: list[BillingAuditEntry] = []
_UPCOMING_CHANGES: list[UpcomingBillingChange] = []


def reset_billing_transparency_state() -> None:
    """测试用: 清空内存账本."""

    _AUDIT_LOG.clear()
    _UPCOMING_CHANGES.clear()


def record_billing_audit(entry: BillingAuditEntry) -> None:
    """记录一笔账单流水."""

    _AUDIT_LOG.append(entry)


def set_upcoming_billing_changes(changes: list[UpcomingBillingChange]) -> None:
    """测试 / 管理端用: 设置未来 30 天账单变化."""

    _UPCOMING_CHANGES[:] = changes


@router.get("/promise")
def get_billing_promise() -> BillingPromise:
    """返还 KUN 的计费透明承诺正文."""

    return BillingPromise(
        effective_at=datetime(2026, 4, 26, tzinfo=UTC),
        commitments=[
            "任何价格、套餐、扣费规则变化至少提前 30 天预告。",
            "用户余额不会因为套餐变化被静默清零或蒸发。",
            "每一笔扣费都能在 audit log 里看到原因、金额和关联任务。",
            "寒暄、状态问询、系统解释类轻交互默认不计费。",
            "用户可对可退流水发起自助退款请求。",
        ],
        small_talk_free_rule="寒暄、查看状态、解释账单、查看承诺不计费；真正执行任务才计费。",
        refund_rule="对可退流水按剩余可退余额自助申请退款，系统保留审计记录。",
    )


@router.get("/audit-log")
def get_billing_audit_log(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> dict[str, object]:
    """用户自己的每笔扣款审计."""

    tenant_id, user_id = _resolve_identity(x_tenant_id, x_user_id)
    entries = _entries_for(tenant_id, user_id)
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "entries": entries,
        "entry_count": len(entries),
    }


@router.post("/refund-request", status_code=status.HTTP_202_ACCEPTED)
def create_refund_request(
    req: RefundRequest,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> RefundResponse:
    """用户一键退款请求."""

    tenant_id, user_id = _resolve_identity(x_tenant_id, x_user_id)
    refundable = _refundable_balance(tenant_id, user_id)
    if req.amount_usd > refundable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "退款金额超过当前可退余额",
                "refundable_balance": refundable,
            },
        )

    now = datetime.now(UTC)
    request_id = f"refund-{len(_AUDIT_LOG) + 1}"
    _AUDIT_LOG.append(
        BillingAuditEntry(
            entry_id=request_id,
            tenant_id=tenant_id,
            user_id=user_id,
            occurred_at=now,
            kind="refund",
            amount_usd=-req.amount_usd,
            reason=f"refund requested: {req.reason}",
            reversible=False,
            refund_eligible=False,
        ),
    )
    return RefundResponse(
        request_id=request_id,
        status="received",
        amount_usd=req.amount_usd,
        message="退款请求已记录, 后续由 billing executor 处理; 这笔记录不会被隐藏。",
    )


@router.get("/upcoming-changes")
def get_upcoming_billing_changes() -> dict[str, object]:
    """未来 30 天账单变化预告; 空数组也是合法承诺."""

    changes = _upcoming_changes_in_notice_window()
    return {
        "notice_window_days": 30,
        "changes": changes,
        "change_count": len(changes),
    }


@router.get("/dashboard")
def get_billing_dashboard(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> BillingDashboard:
    """账单透明总览."""

    tenant_id, user_id = _resolve_identity(x_tenant_id, x_user_id)
    entries = _entries_for(tenant_id, user_id)
    now = datetime.now(UTC)
    day_start = now - timedelta(days=1)
    month_start = now - timedelta(days=30)
    used_today = _sum_charges(entries, since=day_start)
    used_month = _sum_charges(entries, since=month_start)
    saved_by_kun = _round_money(sum(entry.saved_usd for entry in entries))
    return BillingDashboard(
        tenant_id=tenant_id,
        user_id=user_id,
        used_today=used_today,
        used_month=used_month,
        saved_by_kun=saved_by_kun,
        refundable_balance=_refundable_balance(tenant_id, user_id),
        audit_entry_count=len(entries),
        upcoming_change_count=len(_upcoming_changes_in_notice_window()),
    )


def _resolve_identity(x_tenant_id: str | None, x_user_id: str | None) -> tuple[str, str]:
    if x_tenant_id:
        tenant_id = x_tenant_id.strip()
        user_id = (x_user_id or "u-anon").strip()
        return tenant_id, user_id

    ctx = current_tenant()
    tenant_id = ctx.tenant_id.strip()
    user_id = (x_user_id or ctx.user_id or "u-anon").strip()
    return tenant_id, user_id


def _entries_for(tenant_id: str, user_id: str) -> list[BillingAuditEntry]:
    return [entry for entry in _AUDIT_LOG if entry.tenant_id == tenant_id and entry.user_id == user_id]


def _sum_charges(entries: list[BillingAuditEntry], *, since: datetime) -> float:
    return _round_money(
        sum(
            entry.amount_usd
            for entry in entries
            if entry.kind == "charge" and entry.occurred_at >= since
        ),
    )


def _refundable_balance(tenant_id: str, user_id: str) -> float:
    return _round_money(
        sum(
            entry.amount_usd
            for entry in _entries_for(tenant_id, user_id)
            if entry.refund_eligible and entry.amount_usd > 0
        ),
    )


def _upcoming_changes_in_notice_window() -> list[UpcomingBillingChange]:
    now = datetime.now(UTC)
    cutoff = now + timedelta(days=30)
    return [change for change in _UPCOMING_CHANGES if now <= change.effective_at <= cutoff]


def _round_money(value: float) -> float:
    return round(value, 6)
