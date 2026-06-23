"""Tenant resolution in the API middleware (audit F007/F023).

resolve_request_tenant is the pure core of the http tenant middleware:
- auth ENABLED  -> identity from verified JWT only; X-Tenant-Id ignored; 401 on
  missing/invalid token.
- auth DISABLED -> legacy header-based behaviour preserved (dev/staging).
"""

from __future__ import annotations

import pytest
from kun.api.auth.jwt_token import encode_jwt
from kun.api.auth.middleware import AuthSettings
from kun.api.main import resolve_request_tenant
from kun.core.tenancy import TenantContext

_SECRET = "x" * 40


def _enabled() -> AuthSettings:
    return AuthSettings(auth_enabled=True, auth_jwt_secret=_SECRET, default_tenant_id=None)


def _disabled() -> AuthSettings:
    return AuthSettings(auth_enabled=False, auth_jwt_secret=None, default_tenant_id="u-dev")


@pytest.mark.unit
def test_auth_enabled_uses_jwt_and_ignores_tenant_header() -> None:
    token = encode_jwt(
        tenant_id="u-from-token",
        sub="user-9",
        secret=_SECRET,
        extra_claims={"scopes": ["read", "write"]},
    )
    out = resolve_request_tenant(
        auth_settings=_enabled(),
        authorization=f"Bearer {token}",
        x_tenant_id="u-attacker-spoof",  # must be ignored
        x_user_id="spoofed",
        x_scopes="admin",  # must be ignored
        x_audience="expert",
    )
    assert isinstance(out, TenantContext)
    assert out.tenant_id == "u-from-token"  # NOT the spoofed header
    assert out.user_id == "user-9"
    assert set(out.scopes) == {"read", "write"}  # from token, not "admin" header
    assert out.audience == "expert"


@pytest.mark.unit
def test_auth_enabled_missing_token_is_401() -> None:
    out = resolve_request_tenant(
        auth_settings=_enabled(),
        authorization=None,
        x_tenant_id="u-attacker-spoof",  # must NOT grant access
        x_user_id=None,
        x_scopes=None,
        x_audience=None,
    )
    assert isinstance(out, tuple) and out[0] == 401


@pytest.mark.unit
def test_auth_enabled_invalid_token_is_401() -> None:
    out = resolve_request_tenant(
        auth_settings=_enabled(),
        authorization="Bearer not.a.valid.jwt",
        x_tenant_id=None,
        x_user_id=None,
        x_scopes=None,
        x_audience=None,
    )
    assert isinstance(out, tuple) and out[0] == 401


@pytest.mark.unit
def test_auth_enabled_wrong_secret_is_401() -> None:
    token = encode_jwt(tenant_id="u-x", sub="u", secret="y" * 40)
    out = resolve_request_tenant(
        auth_settings=_enabled(),
        authorization=f"Bearer {token}",
        x_tenant_id=None,
        x_user_id=None,
        x_scopes=None,
        x_audience=None,
    )
    assert isinstance(out, tuple) and out[0] == 401


@pytest.mark.unit
def test_auth_disabled_preserves_header_behaviour() -> None:
    out = resolve_request_tenant(
        auth_settings=_disabled(),
        authorization=None,
        x_tenant_id="u-explicit",
        x_user_id="user-1",
        x_scopes="a, b ,c",
        x_audience="novice",
    )
    assert isinstance(out, TenantContext)
    assert out.tenant_id == "u-explicit"
    assert out.user_id == "user-1"
    assert out.scopes == ("a", "b", "c")
    assert out.audience == "novice"


@pytest.mark.unit
def test_auth_disabled_no_header_uses_global_default_or_400() -> None:
    # The disabled path delegates to tenancy.resolve_tenant_id, which reads the
    # *global* settings default tenant (not AuthSettings). Assert against that:
    # a configured default → that tenant; no default (prod) → 400.
    from kun.core.tenancy import default_tenant_id

    expected = default_tenant_id()
    out = resolve_request_tenant(
        auth_settings=_disabled(),
        authorization=None,
        x_tenant_id=None,
        x_user_id=None,
        x_scopes=None,
        x_audience=None,
    )
    if expected is None:
        assert isinstance(out, tuple) and out[0] == 400
    else:
        assert isinstance(out, TenantContext) and out.tenant_id == expected
