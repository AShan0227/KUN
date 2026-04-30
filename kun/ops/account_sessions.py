"""Minimal refresh-token session lifecycle for operator-managed accounts.

This is still not a full signup/OAuth/device-login product.  It closes the
dangerous gap where KUN only had long-lived bearer tokens by adding an auditable
refresh token row and short-lived access token renewal.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.orm import TenantTokenIssueRow
from kun.ops.account_registry import Audience, hash_bearer_token
from kun.security.auth import AuthTokenError, sign_auth_token, verify_bearer_token_any


class SessionTokenPair(BaseModel):
    """Access + refresh token pair returned after an operator-approved session mint."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    audience: Audience
    scopes: list[str] = Field(default_factory=list)
    access_token_id: str
    access_token: str
    access_expires_at: int
    refresh_token_id: str
    refresh_token: str
    refresh_expires_at: int
    persisted: bool = True
    honest_limits: list[str] = Field(default_factory=list)


class AccessTokenRefresh(BaseModel):
    """Result of exchanging a valid refresh token for a new access token."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None
    audience: Audience
    scopes: list[str] = Field(default_factory=list)
    access_token_id: str
    access_token: str
    access_expires_at: int
    refresh_token_id: str
    honest_limits: list[str] = Field(default_factory=list)


class SessionTokenError(ValueError):
    """Raised when a refresh token cannot be used."""


async def issue_session_token_pair(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    secret: str,
    scopes: list[str],
    audience: Audience = "developer",
    access_ttl_sec: int = 900,
    refresh_ttl_sec: int = 60 * 60 * 24 * 30,
    metadata: dict[str, Any] | None = None,
) -> SessionTokenPair:
    """Mint short-lived access + long-lived refresh tokens and persist both rows."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_user = _required("user_id", user_id)
    cleaned_scopes = _clean_scopes(scopes)
    if len(secret) < 32:
        raise ValueError("secret must be at least 32 characters")
    now_ts = int(time.time())
    access_id = _token_id("acc", cleaned_tenant, cleaned_user)
    refresh_id = _token_id("rfr", cleaned_tenant, cleaned_user)
    access_expires_at = now_ts + max(60, access_ttl_sec)
    refresh_expires_at = now_ts + max(access_ttl_sec + 60, refresh_ttl_sec)
    access_token = _sign_session_token(
        tenant_id=cleaned_tenant,
        user_id=cleaned_user,
        scopes=cleaned_scopes,
        audience=audience,
        token_id=access_id,
        token_type="access",
        expires_at=access_expires_at,
        secret=secret,
    )
    refresh_token = _sign_session_token(
        tenant_id=cleaned_tenant,
        user_id=cleaned_user,
        scopes=cleaned_scopes,
        audience=audience,
        token_id=refresh_id,
        token_type="refresh",
        expires_at=refresh_expires_at,
        secret=secret,
    )
    await _store_token_issue(
        session,
        tenant_id=cleaned_tenant,
        token_id=access_id,
        token=access_token,
        user_id=cleaned_user,
        audience=audience,
        scopes=cleaned_scopes,
        expires_at=access_expires_at,
        metadata={
            **(metadata or {}),
            "source": "api.session",
            "kind": "access",
            "refresh_token_id": refresh_id,
        },
    )
    await _store_token_issue(
        session,
        tenant_id=cleaned_tenant,
        token_id=refresh_id,
        token=refresh_token,
        user_id=cleaned_user,
        audience=audience,
        scopes=cleaned_scopes,
        expires_at=refresh_expires_at,
        metadata={**(metadata or {}), "source": "api.session", "kind": "refresh"},
    )
    return SessionTokenPair(
        tenant_id=cleaned_tenant,
        user_id=cleaned_user,
        audience=audience,
        scopes=cleaned_scopes,
        access_token_id=access_id,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token_id=refresh_id,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
        honest_limits=_honest_limits(),
    )


async def refresh_session_access_token(
    session: AsyncSession,
    *,
    refresh_token: str,
    auth_secrets: list[str],
    signing_secret: str | None = None,
    access_ttl_sec: int = 900,
) -> AccessTokenRefresh:
    """Exchange a ledger-backed refresh token for a new short-lived access token."""

    if not auth_secrets:
        raise SessionTokenError("no auth secrets configured")
    secret = signing_secret or auth_secrets[0]
    try:
        claims = verify_bearer_token_any(f"Bearer {refresh_token.strip()}", auth_secrets)
    except AuthTokenError as exc:
        raise SessionTokenError(str(exc)) from exc
    if claims.token_type != "refresh":
        raise SessionTokenError("refresh token required")
    token_hash = hash_bearer_token(refresh_token.strip())
    row = (
        await session.execute(
            select(TenantTokenIssueRow).where(
                TenantTokenIssueRow.tenant_id == claims.tenant_id,
                TenantTokenIssueRow.token_hash == token_hash,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise SessionTokenError("refresh token is not in the account ledger")
    if row.status != "issued":
        raise SessionTokenError("refresh token revoked")
    if _metadata(row).get("kind") != "refresh":
        raise SessionTokenError("refresh token ledger row is not refresh-kind")
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        raise SessionTokenError("refresh token expired")
    scopes = _clean_scopes(row.scopes)
    audience = _audience(row.audience)
    access_id = _token_id("acc", row.tenant_id, row.user_id or "")
    access_expires_at = int(time.time()) + max(60, access_ttl_sec)
    access_token = _sign_session_token(
        tenant_id=row.tenant_id,
        user_id=row.user_id or claims.user_id or "",
        scopes=scopes,
        audience=audience,
        token_id=access_id,
        token_type="access",
        expires_at=access_expires_at,
        secret=secret,
    )
    await _store_token_issue(
        session,
        tenant_id=row.tenant_id,
        token_id=access_id,
        token=access_token,
        user_id=row.user_id,
        audience=audience,
        scopes=scopes,
        expires_at=access_expires_at,
        metadata={
            "source": "api.session.refresh",
            "kind": "access",
            "refresh_token_id": row.token_id,
        },
    )
    return AccessTokenRefresh(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        audience=audience,
        scopes=scopes,
        access_token_id=access_id,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token_id=row.token_id,
        honest_limits=_honest_limits(),
    )


def _sign_session_token(
    *,
    tenant_id: str,
    user_id: str,
    scopes: list[str],
    audience: Audience,
    token_id: str,
    token_type: str,
    expires_at: int,
    secret: str,
) -> str:
    return sign_auth_token(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "scopes": scopes,
            "audience": audience,
            "exp": expires_at,
            "jti": token_id,
            "token_type": token_type,
        },
        secret,
    )


async def _store_token_issue(
    session: AsyncSession,
    *,
    tenant_id: str,
    token_id: str,
    token: str,
    user_id: str | None,
    audience: Audience,
    scopes: list[str],
    expires_at: int,
    metadata: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(TenantTokenIssueRow)
        .values(
            tenant_id=tenant_id,
            token_id=token_id,
            token_hash=hash_bearer_token(token),
            user_id=user_id,
            audience=audience,
            scopes=scopes,
            status="issued",
            expires_at=datetime.fromtimestamp(expires_at, UTC),
            metadata_json=metadata,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[TenantTokenIssueRow.tenant_id, TenantTokenIssueRow.token_id],
            set_={
                "token_hash": hash_bearer_token(token),
                "user_id": user_id,
                "audience": audience,
                "scopes": scopes,
                "status": "issued",
                "expires_at": datetime.fromtimestamp(expires_at, UTC),
                "revoked_at": None,
                "metadata_json": metadata,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)


def _token_id(prefix: str, tenant_id: str, user_id: str) -> str:
    raw = f"{prefix}:{tenant_id}:{user_id}:{time.time_ns()}"
    return prefix + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _clean_scopes(scopes: list[str] | object) -> list[str]:
    if not isinstance(scopes, list):
        return []
    return [str(scope).strip() for scope in scopes if str(scope).strip()]


def _metadata(row: TenantTokenIssueRow) -> dict[str, Any]:
    return row.metadata_json if isinstance(row.metadata_json, dict) else {}


def _audience(value: str) -> Audience:
    if value == "novice":
        return "novice"
    if value == "expert":
        return "expert"
    return "developer"


def _required(field: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _honest_limits() -> list[str]:
    return [
        "这是最小 refresh-token 生命周期，不是完整自助注册 / OAuth / 设备登录态。",
        "refresh token 已进入账号账本，可撤销、可审计；access token 保持短有效期。",
        "后续还需要真实登录入口、设备列表、异常登录风控和自助密钥轮换。",
    ]


__all__ = [
    "AccessTokenRefresh",
    "SessionTokenError",
    "SessionTokenPair",
    "issue_session_token_pair",
    "refresh_session_access_token",
]
