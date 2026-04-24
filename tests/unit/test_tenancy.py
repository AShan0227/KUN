"""Tenant scoping tests."""

import pytest
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    current_tenant,
    default_tenant_id,
    resolve_tenant_id,
    tenant_scope,
)


class _Settings:
    def __init__(self, *, env: str = "dev", default_tenant_id: str | None = "u-sylvan") -> None:
        self.env = env
        self.default_tenant_id = default_tenant_id


@pytest.mark.unit
def test_default_tenant():
    assert current_tenant().tenant_id == default_tenant_id()


@pytest.mark.unit
def test_production_requires_explicit_tenant(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "kun.core.tenancy.settings",
        lambda: _Settings(env="production", default_tenant_id="u-sylvan"),
    )

    assert default_tenant_id() is None
    with pytest.raises(MissingTenantContextError):
        current_tenant()
    assert resolve_tenant_id("tenant-explicit") == "tenant-explicit"


@pytest.mark.unit
def test_blank_default_tenant_requires_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "kun.core.tenancy.settings",
        lambda: _Settings(env="dev", default_tenant_id=None),
    )

    with pytest.raises(MissingTenantContextError):
        resolve_tenant_id(None)


@pytest.mark.unit
def test_override_with_scope():
    original = current_tenant()
    with tenant_scope(TenantContext(tenant_id="u-other", user_id="alice")):
        ctx = current_tenant()
        assert ctx.tenant_id == "u-other"
        assert ctx.user_id == "alice"
    assert current_tenant().tenant_id == original.tenant_id


@pytest.mark.unit
def test_tenant_context_immutable():
    ctx = TenantContext(tenant_id="u-x")
    with pytest.raises(Exception):
        ctx.tenant_id = "u-y"  # type: ignore[misc]
