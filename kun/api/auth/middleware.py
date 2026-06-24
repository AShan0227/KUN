"""Auth middleware — Bearer 提取 → JWT decode → tenant_id 落 request.state.

ADR-019 (中期 posture). 现在默认 `KUN_AUTH_ENABLED=false` —
未启用时 fall back 到 `settings.default_tenant_id` (dev 行为不变).

启用后 (KUN_AUTH_ENABLED=true) 的行为:
  - 缺 Authorization header → 401 (无 X-Tenant-Id fall-back)
  - 非 Bearer scheme → 401
  - JWT 解码失败 / 签名 / 过期 → 401
  - 成功 → request.state.tenant_id = payload.tenant_id

resolve_tenant_id 是纯函数 — 不依赖 FastAPI Request 类, 方便单测.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kun.api.auth.jwt_token import JWTDecodeError, JWTPayload, decode_jwt


class AuthError(Exception):
    """Raised when auth check fails (caller maps to HTTP 401)."""


@dataclass(frozen=True)
class AuthSettings:
    """Subset of Settings the auth layer actually reads."""

    auth_enabled: bool
    auth_jwt_secret: str | None
    default_tenant_id: str | None


@dataclass(frozen=True)
class ResolvedIdentity:
    """Result of resolve_tenant_id — what to attach to request.state."""

    tenant_id: str
    sub: str | None
    """Subject from JWT, or None when auth disabled."""
    auth_source: str
    """'jwt' | 'default_tenant_id' — for audit logging."""
    payload: JWTPayload | None
    """Full decoded payload when auth_source='jwt', else None."""


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract token from `Authorization: Bearer <token>` header.

    Returns None when header missing or scheme is not Bearer.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


# Decoder injectable for tests — type alias keeps middleware decoupled from
# the concrete decode_jwt signature.
JWTDecoder = Callable[[str, str], JWTPayload]


def _default_decoder(token: str, secret: str) -> JWTPayload:
    return decode_jwt(token, secret=secret)


def resolve_tenant_id(
    *,
    authorization_header: str | None,
    settings: AuthSettings,
    decoder: JWTDecoder = _default_decoder,
) -> ResolvedIdentity:
    """Resolve tenant identity from request.

    Behaviour:
      - settings.auth_enabled = False:
          * Use settings.default_tenant_id (must not be None).
          * Header is ignored. Source = "default_tenant_id".
      - settings.auth_enabled = True:
          * settings.auth_jwt_secret must be set (config validator enforces).
          * Authorization header must be Bearer <token>.
          * Token is decoded + verified; tenant_id taken from payload.
          * Any failure → AuthError.

    Raises:
      AuthError on missing/invalid auth when enabled.
      RuntimeError on misconfig (auth disabled but no default_tenant_id).
    """
    if not settings.auth_enabled:
        if not settings.default_tenant_id:
            raise RuntimeError(
                "auth disabled but KUN_DEFAULT_TENANT_ID is unset — "
                "either enable auth or set a default tenant"
            )
        return ResolvedIdentity(
            tenant_id=settings.default_tenant_id,
            sub=None,
            auth_source="default_tenant_id",
            payload=None,
        )

    # Auth enabled — require valid JWT
    if not settings.auth_jwt_secret:
        raise RuntimeError(
            "auth enabled but KUN_AUTH_JWT_SECRET is unset — refusing to start"
        )
    token = extract_bearer_token(authorization_header)
    if token is None:
        raise AuthError("missing or malformed Authorization header")
    try:
        payload = decoder(token, settings.auth_jwt_secret)
    except JWTDecodeError as e:
        raise AuthError(f"invalid token: {e}") from e
    return ResolvedIdentity(
        tenant_id=payload.tenant_id,
        sub=payload.sub,
        auth_source="jwt",
        payload=payload,
    )


def build_auth_settings(settings_obj: Any) -> AuthSettings:
    """Adapter: pull only the fields auth needs from a full Settings."""
    return AuthSettings(
        auth_enabled=getattr(settings_obj, "auth_enabled", False),
        auth_jwt_secret=getattr(settings_obj, "auth_jwt_secret", None),
        default_tenant_id=getattr(settings_obj, "default_tenant_id", None),
    )


__all__ = [
    "AuthError",
    "AuthSettings",
    "JWTDecoder",
    "ResolvedIdentity",
    "build_auth_settings",
    "extract_bearer_token",
    "resolve_tenant_id",
]
