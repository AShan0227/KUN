"""Minimal account session endpoints.

This exposes refresh-token renewal only.  It is intentionally honest: signup,
password/OAuth login, device management, and billing remain outside this slice.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kun.core.config import settings
from kun.core.db import session_scope
from kun.ops.account_sessions import (
    AccessTokenRefresh,
    SessionTokenError,
    refresh_session_access_token,
)
from kun.security.auth import AuthTokenError, extract_bearer_token, verify_bearer_token_any

router = APIRouter(prefix="/api/auth/session", tags=["auth-session"])


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


@router.post("/refresh", response_model=RefreshSessionResponse)
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
