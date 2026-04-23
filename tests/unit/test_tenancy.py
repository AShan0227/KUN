"""Tenant scoping tests."""

import pytest
from kun.core.tenancy import TenantContext, current_tenant, default_tenant_id, tenant_scope


@pytest.mark.unit
def test_default_tenant():
    assert current_tenant().tenant_id == default_tenant_id()


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
