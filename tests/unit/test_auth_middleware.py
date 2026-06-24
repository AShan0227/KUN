"""L6.AuthScaffold — auth middleware (Bearer → tenant resolution) 单测."""

from __future__ import annotations

import pytest
from kun.api.auth.jwt_token import JWTDecodeError, JWTPayload
from kun.api.auth.middleware import (
    AuthError,
    AuthSettings,
    build_auth_settings,
    extract_bearer_token,
    resolve_tenant_id,
)


def _payload(tenant: str = "t-acme", sub: str = "u-1") -> JWTPayload:
    return JWTPayload(tenant_id=tenant, sub=sub, iat=0, exp=9999999999, extra={})


# ---- extract_bearer_token ----


def test_extract_bearer_happy_path() -> None:
    assert extract_bearer_token("Bearer abc123") == "abc123"


def test_extract_bearer_case_insensitive_scheme() -> None:
    assert extract_bearer_token("bearer abc123") == "abc123"
    assert extract_bearer_token("BEARER abc123") == "abc123"


def test_extract_bearer_none_when_missing() -> None:
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None


def test_extract_bearer_rejects_basic_scheme() -> None:
    assert extract_bearer_token("Basic abc123") is None


def test_extract_bearer_rejects_no_space() -> None:
    assert extract_bearer_token("Bearer") is None


def test_extract_bearer_rejects_empty_token() -> None:
    assert extract_bearer_token("Bearer ") is None


def test_extract_bearer_trims_token() -> None:
    assert extract_bearer_token("Bearer  abc  ") == "abc"


# ---- resolve_tenant_id (auth DISABLED) ----


def test_resolve_disabled_uses_default_tenant() -> None:
    settings = AuthSettings(
        auth_enabled=False, auth_jwt_secret=None, default_tenant_id="u-sylvan"
    )
    identity = resolve_tenant_id(
        authorization_header="Bearer ignored-when-disabled",
        settings=settings,
    )
    assert identity.tenant_id == "u-sylvan"
    assert identity.sub is None
    assert identity.auth_source == "default_tenant_id"
    assert identity.payload is None


def test_resolve_disabled_no_default_raises_misconfig() -> None:
    settings = AuthSettings(
        auth_enabled=False, auth_jwt_secret=None, default_tenant_id=None
    )
    with pytest.raises(RuntimeError, match="DEFAULT_TENANT_ID"):
        resolve_tenant_id(authorization_header=None, settings=settings)


# ---- resolve_tenant_id (auth ENABLED) ----


def test_resolve_enabled_decodes_token() -> None:
    settings = AuthSettings(
        auth_enabled=True,
        auth_jwt_secret="s" * 32,
        default_tenant_id=None,
    )

    def fake_decoder(token: str, secret: str) -> JWTPayload:
        assert token == "tok-x"
        assert secret == "s" * 32
        return _payload(tenant="t-real", sub="u-42")

    identity = resolve_tenant_id(
        authorization_header="Bearer tok-x",
        settings=settings,
        decoder=fake_decoder,
    )
    assert identity.tenant_id == "t-real"
    assert identity.sub == "u-42"
    assert identity.auth_source == "jwt"
    assert identity.payload is not None
    assert identity.payload.sub == "u-42"


def test_resolve_enabled_missing_header_raises_auth_error() -> None:
    settings = AuthSettings(
        auth_enabled=True,
        auth_jwt_secret="s" * 32,
        default_tenant_id=None,
    )
    with pytest.raises(AuthError, match="missing"):
        resolve_tenant_id(authorization_header=None, settings=settings)


def test_resolve_enabled_malformed_header_raises_auth_error() -> None:
    settings = AuthSettings(
        auth_enabled=True, auth_jwt_secret="s" * 32, default_tenant_id=None
    )
    with pytest.raises(AuthError, match="missing"):
        resolve_tenant_id(
            authorization_header="Basic foo",
            settings=settings,
        )


def test_resolve_enabled_decoder_failure_maps_to_auth_error() -> None:
    settings = AuthSettings(
        auth_enabled=True, auth_jwt_secret="s" * 32, default_tenant_id=None
    )

    def failing_decoder(token: str, secret: str) -> JWTPayload:
        raise JWTDecodeError("signature mismatch")

    with pytest.raises(AuthError, match="invalid token"):
        resolve_tenant_id(
            authorization_header="Bearer bad",
            settings=settings,
            decoder=failing_decoder,
        )


def test_resolve_enabled_without_secret_raises_misconfig() -> None:
    settings = AuthSettings(
        auth_enabled=True, auth_jwt_secret=None, default_tenant_id=None
    )
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        resolve_tenant_id(
            authorization_header="Bearer x", settings=settings
        )


# ---- build_auth_settings adapter ----


def test_build_auth_settings_from_object() -> None:
    class Stub:
        auth_enabled = True
        auth_jwt_secret = "s" * 32
        default_tenant_id = "u-sylvan"

    auth = build_auth_settings(Stub())
    assert auth.auth_enabled is True
    assert auth.auth_jwt_secret == "s" * 32
    assert auth.default_tenant_id == "u-sylvan"


def test_build_auth_settings_missing_fields_default() -> None:
    class Bare:
        pass

    auth = build_auth_settings(Bare())
    assert auth.auth_enabled is False
    assert auth.auth_jwt_secret is None
    assert auth.default_tenant_id is None
