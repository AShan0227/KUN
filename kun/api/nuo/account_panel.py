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
    last_used_at: datetime | None = None
    last_ip_hash: str | None = None
    last_user_agent: str | None = None
    use_count: int = 0
    session_risk_level: str = "info"
    session_risk_reasons: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    expired: bool
    expires_in_sec: int | None = None


class TenantAccountLedgerSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    current_user_id: str | None = None
    current_member: TenantMemberSummary | None = None
    membership_validated: bool = False
    membership_warning: str = ""
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
    invite_ttl_sec: int = Field(default=604800, ge=60, le=2592000)


class InviteEmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str
    recipient_user_id: str
    delivery_status: str = "draft_only"


class InviteMemberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    role: MemberRole
    scopes: list[str]
    status: str
    acceptance_token_id: str | None = None
    acceptance_token: str | None = None
    invite_expires_at: datetime | None = None
    email_draft: InviteEmailDraft
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
        current_member = _current_member_summary(
            current_user_id=tenant.user_id,
            members=member_items,
        )
        membership_validated = current_member is not None and current_member.status == "active"

    return TenantAccountLedgerSummary(
        tenant_id=tenant.tenant_id,
        current_user_id=tenant.user_id,
        current_member=current_member,
        membership_validated=membership_validated,
        membership_warning=_membership_warning(
            current_user_id=tenant.user_id,
            current_member=current_member,
        ),
        account=_account_summary(tenant.tenant_id, account),
        members=member_items,
        tokens=token_items,
        counts={
            "members": len(member_items),
            "issued_tokens": sum(1 for item in token_items if item.status == "issued"),
            "revoked_tokens": sum(1 for item in token_items if item.status == "revoked"),
            "expired_tokens": sum(1 for item in token_items if item.expired),
            "session_risk_tokens": sum(
                1 for item in token_items if item.session_risk_level != "info"
            ),
        },
        honest_limits=[
            "这里只展示账号/成员/token 签发账本，不返回 raw bearer token 或 token_hash。",
            "refresh token 续期已有最小闭环，但这还不是完整自助注册 / OAuth / 设备登录态。",
            "会话风险只是最小账本提示，不等于完整设备登录态或异常登录风控。",
            "账单字段仍是运营状态记录，不代表已经接入真实支付闭环。",
            "这里仅验证当前请求在当前租户内的成员身份，还不是跨租户服务端切换器。",
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
    secrets = settings().auth_secret_candidates()
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        invited = await invite_tenant_member(
            s,
            tenant_id=tenant.tenant_id,
            user_id=payload.user_id,
            role=payload.role,
            scopes=_string_list(payload.scopes),
            invite_secret=secrets[0] if secrets else None,
            invite_ttl_sec=payload.invite_ttl_sec,
            invited_by_user_id=tenant.user_id,
        )
    message = "成员邀请已写入账本；尚未发送邮件。"
    if invited.acceptance_token:
        message += " 已生成一次性接受 token，请通过可信渠道交给被邀请成员。"
    else:
        message += " 未生成一次性接受 token，因为当前没有可用 KUN_AUTH_SECRET。"
    return InviteMemberResponse(
        tenant_id=invited.tenant_id,
        user_id=invited.user_id,
        role=invited.role,
        scopes=invited.scopes,
        status=invited.status,
        acceptance_token_id=invited.acceptance_token_id,
        acceptance_token=invited.acceptance_token,
        invite_expires_at=invited.invite_expires_at,
        email_draft=_invite_email_draft(invited),
        message=message,
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


def _current_member_summary(
    *,
    current_user_id: str | None,
    members: list[TenantMemberSummary],
) -> TenantMemberSummary | None:
    if not current_user_id:
        return None
    for member in members:
        if member.user_id == current_user_id:
            return member
    return None


def _membership_warning(
    *,
    current_user_id: str | None,
    current_member: TenantMemberSummary | None,
) -> str:
    if not current_user_id:
        return "当前请求没有 user_id，不能验证成员身份。"
    if current_member is None:
        return (
            "当前 user_id 不在当前租户成员账本里；这可能是本地 fallback、未完成邀请或错误 token。"
        )
    if current_member.status != "active":
        return f"当前成员状态是 {current_member.status}，不是 active。"
    return ""


def _invite_email_draft(invited: Any) -> InviteEmailDraft:
    subject = f"你被邀请加入 KUN 租户 {invited.tenant_id}"
    lines = [
        f"你被邀请加入 KUN 租户：{invited.tenant_id}",
        f"用户 ID：{invited.user_id}",
        f"角色：{invited.role}",
        f"权限：{', '.join(_string_list(invited.scopes)) or '无'}",
    ]
    if invited.invite_expires_at is not None:
        lines.append(f"过期时间：{invited.invite_expires_at.isoformat()}")
    lines.extend(
        [
            "",
            "请打开 KUN 前端的账号入口，在“接受成员邀请”里粘贴下面的一次性 invite token：",
            invited.acceptance_token or "<当前没有生成 invite token>",
            "",
            "注意：这个 token 等同于一次性邀请凭证，请不要公开转发。",
            "系统只生成了邮件草稿，没有自动发送邮件。",
        ]
    )
    return InviteEmailDraft(
        subject=subject,
        body="\n".join(lines),
        recipient_user_id=str(invited.user_id),
    )


def _token_summary(row: TenantTokenIssueRow) -> TenantTokenSummary:
    now = datetime.now(UTC)
    expired = bool(row.expires_at and row.expires_at <= now)
    expires_in_sec = None
    if row.expires_at is not None:
        expires_in_sec = max(0, int((row.expires_at - now).total_seconds()))
    risk_level, risk_reasons = _token_session_risk(
        status=str(row.status),
        expired=expired,
        expires_in_sec=expires_in_sec,
        use_count=int(row.use_count or 0),
        last_ip_hash=row.last_ip_hash,
        last_user_agent=row.last_user_agent,
    )
    return TenantTokenSummary(
        token_id=row.token_id,
        user_id=row.user_id,
        audience=row.audience,
        scopes=_string_list(row.scopes),
        status=row.status,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        last_ip_hash=row.last_ip_hash,
        last_user_agent=row.last_user_agent,
        use_count=int(row.use_count or 0),
        session_risk_level=risk_level,
        session_risk_reasons=risk_reasons,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expired=expired,
        expires_in_sec=expires_in_sec,
    )


def _token_session_risk(
    *,
    status: str,
    expired: bool,
    expires_in_sec: int | None,
    use_count: int,
    last_ip_hash: str | None,
    last_user_agent: str | None,
) -> tuple[str, list[str]]:
    if status == "revoked":
        return "info", ["token 已撤销"]
    if expired:
        return "info", ["token 已过期"]

    reasons: list[str] = []
    level = "info"
    if expires_in_sec is None:
        reasons.append("token 没有过期时间")
        level = "warn"
    elif expires_in_sec > 60 * 60 * 24 * 30:
        reasons.append("token 有效期超过 30 天")
        level = "warn"

    if use_count > 0 and not last_ip_hash:
        reasons.append("已有调用但缺少 IP 指纹")
        level = "warn"
    if use_count > 0 and not last_user_agent:
        reasons.append("已有调用但缺少 UA 摘要")
        level = "warn"
    if use_count > 1000:
        reasons.append("token 使用次数很高，需要定期复核")
        level = "warn"

    if not reasons:
        reasons.append("未发现明显会话风险")
    return level, reasons


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
