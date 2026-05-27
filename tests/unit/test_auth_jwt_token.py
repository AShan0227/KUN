"""L6.AuthScaffold — HMAC-SHA256 JWT 单测."""

from __future__ import annotations

import base64
import json

import pytest
from kun.api.auth.jwt_token import (
    JWTDecodeError,
    JWTPayload,
    decode_jwt,
    encode_jwt,
)

SECRET = "x" * 32  # ≥16 chars required


def test_encode_decode_round_trip() -> None:
    token = encode_jwt(tenant_id="t-acme", sub="u-1", secret=SECRET)
    payload = decode_jwt(token, secret=SECRET)
    assert isinstance(payload, JWTPayload)
    assert payload.tenant_id == "t-acme"
    assert payload.sub == "u-1"
    assert payload.exp > payload.iat
    assert payload.extra == {}


def test_encode_includes_extra_claims() -> None:
    token = encode_jwt(
        tenant_id="t-acme",
        sub="u-1",
        secret=SECRET,
        extra_claims={"role": "admin", "scope": "rw"},
    )
    payload = decode_jwt(token, secret=SECRET)
    assert payload.extra == {"role": "admin", "scope": "rw"}


def test_extra_claims_cannot_override_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        encode_jwt(
            tenant_id="t-acme",
            sub="u-1",
            secret=SECRET,
            extra_claims={"tenant_id": "evil"},
        )


def test_encode_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="16 chars"):
        encode_jwt(tenant_id="t-acme", sub="u-1", secret="short")


def test_encode_rejects_empty_tenant() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        encode_jwt(tenant_id="", sub="u-1", secret=SECRET)


def test_encode_rejects_empty_sub() -> None:
    with pytest.raises(ValueError, match="sub"):
        encode_jwt(tenant_id="t-acme", sub="", secret=SECRET)


def test_encode_rejects_zero_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        encode_jwt(
            tenant_id="t-acme", sub="u-1", secret=SECRET, ttl_seconds=0
        )


def test_decode_rejects_wrong_secret() -> None:
    token = encode_jwt(tenant_id="t-acme", sub="u-1", secret=SECRET)
    with pytest.raises(JWTDecodeError, match="signature mismatch"):
        decode_jwt(token, secret="y" * 32)


def test_decode_rejects_tampered_payload() -> None:
    token = encode_jwt(tenant_id="t-acme", sub="u-1", secret=SECRET)
    parts = token.split(".")
    # forge a new payload with different tenant_id
    forged_payload = base64.urlsafe_b64encode(
        json.dumps({"tenant_id": "evil", "sub": "u-1", "iat": 0, "exp": 9999999999})
        .encode()
    ).rstrip(b"=").decode("ascii")
    tampered = ".".join([parts[0], forged_payload, parts[2]])
    with pytest.raises(JWTDecodeError, match="signature mismatch"):
        decode_jwt(tampered, secret=SECRET)


def test_decode_rejects_expired_token() -> None:
    # issued 2 hours ago, ttl 1 hour
    token = encode_jwt(
        tenant_id="t-acme",
        sub="u-1",
        secret=SECRET,
        ttl_seconds=3600,
        now=1000000,
    )
    # decode "now" is 2 hours later
    with pytest.raises(JWTDecodeError, match="expired"):
        decode_jwt(token, secret=SECRET, now=1000000 + 7200)


def test_decode_accepts_leeway() -> None:
    token = encode_jwt(
        tenant_id="t-acme",
        sub="u-1",
        secret=SECRET,
        ttl_seconds=3600,
        now=1000000,
    )
    # 10 sec past expiry, with 30 sec leeway → still valid
    payload = decode_jwt(
        token, secret=SECRET, now=1000000 + 3610, leeway_seconds=30
    )
    assert payload.tenant_id == "t-acme"


def test_decode_rejects_malformed_token() -> None:
    with pytest.raises(JWTDecodeError, match="3 segments"):
        decode_jwt("not.a.jwt.token", secret=SECRET)
    with pytest.raises(JWTDecodeError, match="3 segments"):
        decode_jwt("only.two", secret=SECRET)


def test_decode_rejects_empty_token() -> None:
    with pytest.raises(JWTDecodeError, match="non-empty"):
        decode_jwt("", secret=SECRET)


def test_decode_rejects_non_hs256_alg() -> None:
    bad_header = (
        base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    bad_payload = (
        base64.urlsafe_b64encode(
            json.dumps({"tenant_id": "x", "sub": "y", "iat": 0, "exp": 999999})
            .encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    token = f"{bad_header}.{bad_payload}."
    with pytest.raises(JWTDecodeError, match="unsupported alg"):
        decode_jwt(token, secret=SECRET)


def test_decode_rejects_missing_claims() -> None:
    # Encode a payload missing tenant_id
    bad_payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "u-1", "iat": 0, "exp": 999999999}).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    # We have to forge a valid signature too
    import hashlib
    import hmac

    header_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "typ": "JWT"}, sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signing_input = f"{header_b64}.{bad_payload}".encode()
    sig = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    token = f"{header_b64}.{bad_payload}.{sig_b64}"
    with pytest.raises(JWTDecodeError, match=r"missing.*tenant_id"):
        decode_jwt(token, secret=SECRET)


def test_decode_rejects_non_object_payload() -> None:
    import hashlib
    import hmac

    header_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "typ": "JWT"}, sort_keys=True).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    # payload is a JSON array, not an object
    payload_b64 = (
        base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode("ascii")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    token = f"{header_b64}.{payload_b64}.{sig_b64}"
    with pytest.raises(JWTDecodeError, match="JSON object"):
        decode_jwt(token, secret=SECRET)
