"""Minimal account session endpoints.

This exposes two honest auth slices:

* refresh-token renewal for already-issued account sessions;
* optional invite-code signup that creates a tenant ledger row and session pair.

It is still not password login, OAuth, device management, or billing.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.orm import TenantTokenIssueRow
from kun.core.tenancy import current_tenant
from kun.ops.account_registry import (
    Audience,
    TenantAccountRecord,
    revoke_token_issue,
    upsert_tenant_account_member,
)
from kun.ops.account_sessions import (
    AccessTokenRefresh,
    SessionTokenError,
    SessionTokenPair,
    issue_session_token_pair,
    refresh_session_access_token,
)
from kun.security.auth import AuthTokenError, extract_bearer_token, verify_bearer_token_any

router = APIRouter(prefix="/api/auth", tags=["auth-session"])


class RefreshSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = Field(default=None, min_length=1)


class RefreshSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None
    audience: str
    scopes: list[str]
    access_token_id: str
    access_token: str
    access_expires_at: int
    refresh_token_id: str
    honest_limits: list[str]


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite_code: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1, max_length=80)
    owner_user_id: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    organization_id: str | None = Field(default=None, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["chat:write", "world:approve"])
    audience: Audience = "developer"
    plan: str = Field(default="dev", max_length=40)
    billing_status: str = Field(default="manual", max_length=40)


class SignupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    organization_id: str
    display_name: str
    owner_user_id: str
    account_persisted: bool
    access_token_id: str
    access_token: str
    access_expires_at: int
    refresh_token_id: str
    refresh_token: str
    refresh_expires_at: int
    scopes: list[str]
    audience: Audience
    honest_limits: list[str]


class CurrentSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    audience: Audience
    honest_limits: list[str]


class SessionTokenSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    token_kind: str
    status: str
    expires_at: str | None = None
    revoked_at: str | None = None
    scopes: list[str] = Field(default_factory=list)


class CurrentUserSessionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    tokens: list[SessionTokenSummary]
    honest_limits: list[str]


class RevokeOwnSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_id: str
    status: str
    message: str


@router.post("/signup", response_model=SignupResponse)
async def signup(payload: SignupRequest) -> SignupResponse:
    """Create a tenant account and a refreshable session, if invite signup is enabled."""

    cfg = settings()
    if not cfg.self_signup_enabled:
        raise HTTPException(status_code=403, detail="self signup is disabled")
    expected_invite = (cfg.self_signup_invite_code or "").strip()
    if not expected_invite:
        raise HTTPException(
            status_code=503,
            detail="KUN_SELF_SIGNUP_INVITE_CODE is required when self signup is enabled",
        )
    if payload.invite_code.strip() != expected_invite:
        raise HTTPException(status_code=403, detail="invalid invite code")
    secrets = cfg.auth_secret_candidates()
    if not secrets:
        raise HTTPException(
            status_code=503,
            detail="KUN_AUTH_SECRET or KUN_AUTH_SECRETS is required for signup",
        )

    tenant_id = payload.tenant_id.strip()
    owner_user_id = payload.owner_user_id.strip()
    organization_id = (payload.organization_id or tenant_id).strip()
    display_name = (payload.display_name or tenant_id).strip()
    scopes = _clean_scopes(payload.scopes)
    async with session_scope(tenant_id=tenant_id) as s:
        account = await upsert_tenant_account_member(
            s,
            tenant_id=tenant_id,
            organization_id=organization_id,
            display_name=display_name,
            owner_user_id=owner_user_id,
            scopes=scopes,
            plan=payload.plan,
            billing_status=payload.billing_status,
            metadata={"source": "api.auth.signup"},
        )
        pair = await issue_session_token_pair(
            s,
            tenant_id=tenant_id,
            user_id=owner_user_id,
            scopes=scopes,
            audience=payload.audience,
            secret=secrets[0],
            metadata={"source": "api.auth.signup"},
        )
    return _signup_response(account, pair)


@router.get("/session/me", response_model=CurrentSessionResponse)
async def current_session() -> CurrentSessionResponse:
    """Return the authenticated tenant/user context without exposing secrets."""

    tenant = current_tenant()
    return CurrentSessionResponse(
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        scopes=list(tenant.scopes),
        audience=tenant.audience,
        honest_limits=[
            "这里只返回当前请求上下文，不代表完整设备识别或异常登录风控。",
        ],
    )


@router.get("/session/tokens", response_model=CurrentUserSessionsResponse)
async def current_user_sessions() -> CurrentUserSessionsResponse:
    """List the current user's issued access/refresh tokens for this tenant."""

    tenant = current_tenant()
    if not tenant.user_id:
        raise HTTPException(status_code=400, detail="authenticated user_id is required")
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        rows = (
            await s.execute(
                select(TenantTokenIssueRow)
                .where(
                    TenantTokenIssueRow.tenant_id == tenant.tenant_id,
                    TenantTokenIssueRow.user_id == tenant.user_id,
                )
                .order_by(TenantTokenIssueRow.created_at.desc())
            )
        ).scalars()
        tokens = [_token_summary(row) for row in rows]
    return CurrentUserSessionsResponse(
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        tokens=tokens,
        honest_limits=[
            "这是最小会话列表：能看 token 账本状态，但还没有设备指纹、IP 历史或异常登录风控。",
            "这里不会返回原始 token 或哈希值。",
        ],
    )


@router.post("/session/tokens/{token_id}/revoke", response_model=RevokeOwnSessionResponse)
async def revoke_own_session(token_id: str) -> RevokeOwnSessionResponse:
    """Revoke one token owned by the current authenticated user."""

    cleaned = token_id.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="token_id is required")
    tenant = current_tenant()
    if not tenant.user_id:
        raise HTTPException(status_code=400, detail="authenticated user_id is required")
    async with session_scope(tenant_id=tenant.tenant_id) as s:
        row = (
            await s.execute(
                select(TenantTokenIssueRow.user_id).where(
                    TenantTokenIssueRow.tenant_id == tenant.tenant_id,
                    TenantTokenIssueRow.token_id == cleaned,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="token not found")
        if row != tenant.user_id and "account:admin" not in tenant.scopes:
            raise HTTPException(status_code=403, detail="cannot revoke another user's token")
        revoked = await revoke_token_issue(
            s,
            tenant_id=tenant.tenant_id,
            token_id=cleaned,
            reason="self_session_revoke",
        )
    if not revoked:
        raise HTTPException(status_code=404, detail="issued token not found")
    return RevokeOwnSessionResponse(
        token_id=cleaned,
        status="revoked",
        message="Token 已撤销；生产请求中间件会拒绝这条 token 后续访问。",
    )


@router.post("/session/refresh", response_model=RefreshSessionResponse)
async def refresh_session(
    payload: RefreshSessionRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> RefreshSessionResponse:
    """Exchange a valid refresh token for a new short-lived access token."""

    cfg = settings()
    secrets = cfg.auth_secret_candidates()
    if not secrets:
        raise HTTPException(
            status_code=503,
            detail="KUN_AUTH_SECRET or KUN_AUTH_SECRETS is required for session refresh",
        )
    refresh_token = (payload.refresh_token or "").strip()
    if not refresh_token:
        try:
            refresh_token = extract_bearer_token(authorization)
        except AuthTokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        claims = verify_bearer_token_any(f"Bearer {refresh_token}", secrets)
    except AuthTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if claims.token_type != "refresh":
        raise HTTPException(status_code=400, detail="refresh token required")
    async with session_scope(tenant_id=claims.tenant_id) as s:
        try:
            result = await refresh_session_access_token(
                s,
                refresh_token=refresh_token,
                auth_secrets=secrets,
                signing_secret=secrets[0],
            )
        except SessionTokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _response(result)


def _response(result: AccessTokenRefresh) -> RefreshSessionResponse:
    return RefreshSessionResponse(
        tenant_id=result.tenant_id,
        user_id=result.user_id,
        audience=result.audience,
        scopes=result.scopes,
        access_token_id=result.access_token_id,
        access_token=result.access_token,
        access_expires_at=result.access_expires_at,
        refresh_token_id=result.refresh_token_id,
        honest_limits=result.honest_limits,
    )


def _signup_response(
    account: TenantAccountRecord,
    pair: SessionTokenPair,
) -> SignupResponse:
    return SignupResponse(
        tenant_id=account.tenant_id,
        organization_id=account.organization_id,
        display_name=account.display_name,
        owner_user_id=account.owner_user_id,
        account_persisted=account.persisted,
        access_token_id=pair.access_token_id,
        access_token=pair.access_token,
        access_expires_at=pair.access_expires_at,
        refresh_token_id=pair.refresh_token_id,
        refresh_token=pair.refresh_token,
        refresh_expires_at=pair.refresh_expires_at,
        scopes=pair.scopes,
        audience=pair.audience,
        honest_limits=[
            "这是邀请码注册 + refresh session，不是密码登录 / OAuth / 设备风控。",
            "注册默认关闭；必须显式设置 KUN_SELF_SIGNUP_ENABLED=true 和邀请码。",
            "账单仍是记录字段，不代表已经接入真实支付。",
        ],
    )


def _clean_scopes(scopes: list[str]) -> list[str]:
    cleaned = [str(scope).strip() for scope in scopes if str(scope).strip()]
    return cleaned or ["chat:write", "world:approve"]


def _token_summary(row: TenantTokenIssueRow) -> SessionTokenSummary:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    return SessionTokenSummary(
        token_id=row.token_id,
        token_kind=str(metadata.get("kind") or "unknown"),
        status=row.status,
        expires_at=row.expires_at.isoformat() if row.expires_at is not None else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at is not None else None,
        scopes=[str(scope) for scope in row.scopes if str(scope).strip()]
        if isinstance(row.scopes, list)
        else [],
    )
