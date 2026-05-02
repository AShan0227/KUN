"""Small signed-token auth for production API entry.

This is intentionally boring: a compact HMAC token that lets KUN stop trusting
raw X-Tenant-Id / X-Scopes headers in production. It is not a full account
system, but it closes the dangerous "any caller can pick a tenant" gap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kun.core.tenancy import Audience, TenantContext


class AuthTokenError(ValueError):
    """Raised when a bearer token is absent, malformed, expired, or invalid."""


@dataclass(frozen=True)
class AuthClaims:
    tenant_id: str
    user_id: str | None = None
    scopes: tuple[str, ...] = ()
    audience: Audience = "developer"
    exp: int | None = None
    token_id: str | None = None
    token_type: str | None = None

    def to_tenant_context(self) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            scopes=self.scopes,
            audience=self.audience,
        )


def sign_auth_token(claims: dict[str, Any], secret: str) -> str:
    """Create a test/dev token compatible with ``verify_bearer_token``."""

    payload = _b64_json(claims)
    sig = _signature(payload, secret)
    return f"{payload}.{sig}"


def verify_bearer_token(header_value: str | None, secret: str) -> AuthClaims:
    token = extract_bearer_token(header_value)
    try:
        payload_b64, sig = token.rsplit(".", 1)
    except ValueError as exc:
        raise AuthTokenError("malformed bearer token") from exc
    expected = _signature(payload_b64, secret)
    if not hmac.compare_digest(sig, expected):
        raise AuthTokenError("invalid bearer token signature")
    try:
        raw = _b64_decode(payload_b64)
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthTokenError("invalid bearer token payload") from exc
    claims = _claims_from_payload(data)
    if claims.exp is not None and datetime.now(UTC).timestamp() > claims.exp:
        raise AuthTokenError("bearer token expired")
    return claims


def verify_bearer_token_any(header_value: str | None, secrets: Iterable[str]) -> AuthClaims:
    """Verify a bearer token against active rotation secrets."""

    errors: list[str] = []
    for secret in secrets:
        try:
            return verify_bearer_token(header_value, secret)
        except AuthTokenError as exc:
            errors.append(str(exc))
    if not errors:
        raise AuthTokenError("no auth secrets configured")
    # Keep response small; callers do not need every old-secret mismatch.
    raise AuthTokenError(errors[-1])


def extract_bearer_token(header_value: str | None) -> str:
    """Return the raw bearer token without the ``Bearer`` prefix."""

    if not header_value or not header_value.lower().startswith("bearer "):
        raise AuthTokenError("missing bearer token")
    token = header_value.split(" ", 1)[1].strip()
    if not token:
        raise AuthTokenError("missing bearer token")
    return token


def _claims_from_payload(data: dict[str, Any]) -> AuthClaims:
    tenant_id = str(data.get("tenant_id") or data.get("tenant") or "").strip()
    if not tenant_id:
        raise AuthTokenError("tenant_id is required")
    scopes_raw = data.get("scopes") or []
    if isinstance(scopes_raw, str):
        scopes = tuple(s.strip() for s in scopes_raw.split(",") if s.strip())
    elif isinstance(scopes_raw, list):
        scopes = tuple(str(s).strip() for s in scopes_raw if str(s).strip())
    else:
        scopes = ()
    raw_audience = str(data.get("audience") or "developer").lower()
    if raw_audience == "novice":
        audience: Audience = "novice"
    elif raw_audience == "expert":
        audience = "expert"
    else:
        audience = "developer"
    exp_raw = data.get("exp")
    exp = (
        int(exp_raw) if isinstance(exp_raw, int | float | str) and str(exp_raw).isdigit() else None
    )
    return AuthClaims(
        tenant_id=tenant_id,
        user_id=str(data["user_id"]) if data.get("user_id") is not None else None,
        scopes=scopes,
        audience=audience,
        exp=exp,
        token_id=str(data["jti"]) if data.get("jti") is not None else None,
        token_type=str(data["token_type"]) if data.get("token_type") is not None else None,
    )


def _b64_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _signature(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


__all__ = [
    "AuthClaims",
    "AuthTokenError",
    "extract_bearer_token",
    "sign_auth_token",
    "verify_bearer_token",
    "verify_bearer_token_any",
]
