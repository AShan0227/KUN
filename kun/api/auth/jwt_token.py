"""HMAC-SHA256 JWT encode/decode (L6.AuthScaffold).

stdlib only — no PyJWT/python-jose 依赖. 故意保持简单:
  - HS256 (HMAC-SHA256) — symmetric, fits single-tenant control plane
  - JSON header + payload, base64url-encoded, '.' separated
  - 验签 + exp 检查; 不实现 nbf/iss/aud/jti (用到再加)

为何不引 PyJWT:
  - 一个 secret 共享给 issuer + verifier 已 cover 90% 内部需求
  - 减少 supply-chain surface (现在 cryptography stack 已经够大)
  - ~80 行 vs 加 ~10MB 依赖, ROI 明确

用 RS256/ES256 时 (multi-issuer / 公开 verifier) 再切到 PyJWT — 此时
本模块作为 wrapper 仍然有用 (统一 JWTDecodeError 类型).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any


class JWTDecodeError(ValueError):
    """Raised when JWT decoding or verification fails."""


@dataclass(frozen=True)
class JWTPayload:
    """Decoded JWT payload — only the claims we care about."""

    tenant_id: str
    sub: str  # subject (user id)
    iat: int  # issued-at (unix seconds)
    exp: int  # expiry (unix seconds)
    extra: dict[str, Any]
    """Additional claims passed through (role, scope, ...)."""


_HEADER = {"alg": "HS256", "typ": "JWT"}
_HEADER_ENCODED: bytes  # filled on import


def _b64url_encode(data: bytes) -> bytes:
    """base64url without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64url_decode(data: bytes) -> bytes:
    pad = b"=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


_HEADER_ENCODED = _b64url_encode(
    json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8")
)


def encode_jwt(
    *,
    tenant_id: str,
    sub: str,
    secret: str,
    ttl_seconds: int = 3600,
    extra_claims: dict[str, Any] | None = None,
    now: int | None = None,
) -> str:
    """Encode an HS256 JWT.

    Args:
      tenant_id: required, becomes "tenant_id" claim AND drives RLS.
      sub: required, the user/principal id.
      secret: HS256 signing key. Min 32 chars recommended.
      ttl_seconds: how long the token is valid (default 1h).
      extra_claims: optional dict merged into payload (role, scope, ...).
                    "tenant_id" / "sub" / "iat" / "exp" keys are reserved.
      now: override current time (testing). Defaults to time.time().

    Returns:
      JWT compact string "header.payload.signature".
    """
    if not secret or len(secret) < 16:
        raise ValueError("secret must be at least 16 chars")
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if not sub:
        raise ValueError("sub is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    issued_at = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "sub": sub,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    if extra_claims:
        reserved = {"tenant_id", "sub", "iat", "exp"}
        conflict = reserved & extra_claims.keys()
        if conflict:
            raise ValueError(f"extra_claims may not override reserved keys: {conflict}")
        payload.update(extra_claims)

    payload_encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = _HEADER_ENCODED + b"." + payload_encoded
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url_encode(sig)).decode("ascii")


def decode_jwt(
    token: str,
    *,
    secret: str,
    now: int | None = None,
    leeway_seconds: int = 0,
) -> JWTPayload:
    """Decode + verify an HS256 JWT.

    Raises:
      JWTDecodeError if structure is malformed, signature fails, or expired.

    Args:
      token: JWT compact string.
      secret: same secret used to encode.
      now: override current time (testing).
      leeway_seconds: clock skew tolerance.
    """
    if not isinstance(token, str) or not token:
        raise JWTDecodeError("token must be a non-empty string")
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTDecodeError("malformed token: expected 3 segments")
    header_b64, payload_b64, sig_b64 = parts

    # Verify header
    try:
        header = json.loads(_b64url_decode(header_b64.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as e:
        raise JWTDecodeError(f"malformed header: {e}") from e
    if header.get("alg") != "HS256":
        raise JWTDecodeError(f"unsupported alg: {header.get('alg')!r}")
    if header.get("typ") not in (None, "JWT"):
        raise JWTDecodeError(f"unexpected typ: {header.get('typ')!r}")

    # Verify signature (constant-time compare)
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    expected = hmac.new(
        secret.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    try:
        actual = _b64url_decode(sig_b64.encode("ascii"))
    except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
        raise JWTDecodeError(f"malformed signature: {e}") from e
    if not hmac.compare_digest(expected, actual):
        raise JWTDecodeError("signature mismatch")

    # Verify payload
    try:
        payload = json.loads(_b64url_decode(payload_b64.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as e:
        raise JWTDecodeError(f"malformed payload: {e}") from e
    if not isinstance(payload, dict):
        raise JWTDecodeError("payload must be a JSON object")
    for required in ("tenant_id", "sub", "iat", "exp"):
        if required not in payload:
            raise JWTDecodeError(f"missing required claim: {required}")

    current = int(now if now is not None else time.time())
    if current > int(payload["exp"]) + leeway_seconds:
        raise JWTDecodeError("token expired")

    reserved = {"tenant_id", "sub", "iat", "exp"}
    extra = {k: v for k, v in payload.items() if k not in reserved}
    return JWTPayload(
        tenant_id=str(payload["tenant_id"]),
        sub=str(payload["sub"]),
        iat=int(payload["iat"]),
        exp=int(payload["exp"]),
        extra=extra,
    )


__all__ = [
    "JWTDecodeError",
    "JWTPayload",
    "decode_jwt",
    "encode_jwt",
]
