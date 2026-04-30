"""傩 · 账号和租户账本面板.

这不是完整登录系统。它只把现有的 operator-managed 账号、成员和
token 签发账本暴露出来，方便用户/运维知道当前租户是谁、哪些 token
还活着、必要时能撤销。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.orm import TenantAccountRow, TenantMemberRow, TenantTokenIssueRow
from kun.core.tenancy import current_tenant, require_scope
from kun.ops.account_registry import (
    MemberRole,
    invite_tenant_member,
    revoke_token_issue,
)

router = APIRouter()


class TenantAccountSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    organization_id: str | None = None
    display_name: str | None = None
    owner_user_id: str | None = None
    status: str = "missing"
    plan: str | None = None
    billing_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenantMemberSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    role: str
    scopes: list[str] = Field(default_factory=list)
    status: str
    created_at: datetime
    updated_at: datetime


class TenantTokenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    user_id: str | None = None
    audience: str
    scopes: list[str] = Field(default_factory=list)
    status: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    expired: bool
    expires_in_sec: int | None = None


class TenantAccountLedgerSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    account: TenantAccountSummary
    members: list[TenantMemberSummary]
    tokens: list[TenantTokenSummary]
    counts: dict[str, int]
    honest_limits: list[str]


class RevokeTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=120)
    role: MemberRole = "member"
    scopes: list[str] = Field(default_factory=lambda: ["chat:write"])


class InviteMemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    role: MemberRole
    scopes: list[str]
    status: str
    message: str
    honest_limits: list[str]


class RevokeTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    status: str
    message: str


@router.get("/summary", response_model=TenantAccountLedgerSummary)
async def account_summary() -> TenantAccountLedgerSummary:
    """Return tenant account/member/token ledger without exposing raw secrets."""

    _require_scope_when_enforced("account:read")
    tenant = current_tenant()
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        account = (
            await s.execute(
                select(TenantAccountRow).where(TenantAccountRow.tenant_id == tenant.tenant_id)
            )
        ).scalar_one_or_none()
        members = (
            await s.execute(
                select(TenantMemberRow)
                .where(TenantMemberRow.tenant_id == tenant.tenant_id)
                .order_by(TenantMemberRow.role.asc(), TenantMemberRow.user_id.asc())
            )
        ).scalars()
        tokens = (
            await s.execute(
                select(TenantTokenIssueRow)
                .where(TenantTokenIssueRow.tenant_id == tenant.tenant_id)
                .order_by(TenantTokenIssueRow.created_at.desc())
            )
        ).scalars()

        member_items = [_member_summary(row) for row in members]
        token_items = [_token_summary(row) for row in tokens]

    return TenantAccountLedgerSummary(
        tenant_id=tenant.tenant_id,
        account=_account_summary(tenant.tenant_id, account),
        members=member_items,
        tokens=token_items,
        counts={
            "members": len(member_items),
            "issued_tokens": sum(1 for item in token_items if item.status == "issued"),
            "revoked_tokens": sum(1 for item in token_items if item.status == "revoked"),
            "expired_tokens": sum(1 for item in token_items if item.expired),
        },
        honest_limits=[
            "这里只展示账号/成员/token 签发账本，不返回 raw bearer token 或 token_hash。",
            "refresh token 续期已有最小闭环，但这还不是完整自助注册 / OAuth / 设备登录态。",
            "账单字段仍是运营状态记录，不代表已经接入真实支付闭环。",
        ],
    )


@router.post("/members/invite", response_model=InviteMemberResponse)
async def invite_member(payload: InviteMemberRequest) -> InviteMemberResponse:
    """Add a member invitation row for the current tenant.

    This is a ledger action only.  It does not send email or mint a session
    token, so the UI/API cannot accidentally imply the invite was delivered.
    """

    _require_scope_when_enforced("account:admin")
    tenant = current_tenant()
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        invited = await invite_tenant_member(
            s,
            tenant_id=tenant.tenant_id,
            user_id=payload.user_id,
            role=payload.role,
            scopes=_string_list(payload.scopes),
        )
    return InviteMemberResponse(
        tenant_id=invited.tenant_id,
        user_id=invited.user_id,
        role=invited.role,
        scopes=invited.scopes,
        status=invited.status,
        message="成员邀请已写入账本；尚未发送邮件，也尚未签发 session。",
        honest_limits=invited.honest_limits,
    )


@router.post("/tokens/{token_id}/revoke", response_model=RevokeTokenResponse)
async def revoke_token(token_id: str, payload: RevokeTokenRequest) -> RevokeTokenResponse:
    """Revoke an issued token for the current tenant."""

    cleaned_token_id = token_id.strip()
    if not cleaned_token_id:
        raise HTTPException(status_code=400, detail="token_id is required")
    _require_scope_when_enforced("account:admin")
    tenant = current_tenant()
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        revoked = await revoke_token_issue(
            s,
            tenant_id=tenant.tenant_id,
            token_id=cleaned_token_id,
            reason=payload.reason,
        )
    if not revoked:
        raise HTTPException(status_code=404, detail="issued token not found")
    return RevokeTokenResponse(
        token_id=cleaned_token_id,
        status="revoked",
        message="Token 已撤销；生产请求中间件会拒绝这条 token 后续访问。",
    )


def _account_summary(
    tenant_id: str,
    row: TenantAccountRow | None,
) -> TenantAccountSummary:
    if row is None:
        return TenantAccountSummary(tenant_id=tenant_id)
    return TenantAccountSummary(
        tenant_id=row.tenant_id,
        organization_id=row.organization_id,
        display_name=row.display_name,
        owner_user_id=row.owner_user_id,
        status=row.status,
        plan=row.plan,
        billing_status=row.billing_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _member_summary(row: TenantMemberRow) -> TenantMemberSummary:
    return TenantMemberSummary(
        user_id=row.user_id,
        role=row.role,
        scopes=_string_list(row.scopes),
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _token_summary(row: TenantTokenIssueRow) -> TenantTokenSummary:
    now = datetime.now(UTC)
    expired = bool(row.expires_at and row.expires_at <= now)
    expires_in_sec = None
    if row.expires_at is not None:
        expires_in_sec = max(0, int((row.expires_at - now).total_seconds()))
    return TenantTokenSummary(
        token_id=row.token_id,
        user_id=row.user_id,
        audience=row.audience,
        scopes=_string_list(row.scopes),
        status=row.status,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expired=expired,
        expires_in_sec=expires_in_sec,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
