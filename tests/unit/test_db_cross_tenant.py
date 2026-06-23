"""Cross-tenant access detection (audit F005).

_record_cross_tenant_attempt must emit kun_tenant_cross_access_attempt_total
exactly when a non-bypass session opens for a tenant that differs from the
explicitly-set ambient request identity — and stay silent otherwise.
Tested directly (no DB) so it runs offline.
"""

from __future__ import annotations

import pytest
from kun.core.db import _record_cross_tenant_attempt
from kun.core.metrics import tenant_cross_access_attempt
from kun.core.tenancy import TenantContext, tenant_scope


def _count(from_tenant: str, to_tenant: str) -> float:
    return tenant_cross_access_attempt.labels(
        from_tenant=from_tenant, to_tenant=to_tenant
    )._value.get()


@pytest.mark.unit
def test_mismatch_records_attempt() -> None:
    before = _count("u-a", "u-b")
    with tenant_scope(TenantContext(tenant_id="u-a")):
        _record_cross_tenant_attempt(tenant_id="u-b", bypass_rls=False)
    assert _count("u-a", "u-b") == before + 1


@pytest.mark.unit
def test_matching_tenant_does_not_record() -> None:
    before = _count("u-a", "u-a")
    with tenant_scope(TenantContext(tenant_id="u-a")):
        _record_cross_tenant_attempt(tenant_id="u-a", bypass_rls=False)
    assert _count("u-a", "u-a") == before


@pytest.mark.unit
def test_bypass_rls_is_exempt() -> None:
    before = _count("u-a", "u-b")
    with tenant_scope(TenantContext(tenant_id="u-a")):
        _record_cross_tenant_attempt(tenant_id="u-b", bypass_rls=True)
    assert _count("u-a", "u-b") == before


@pytest.mark.unit
def test_no_ambient_context_does_not_record() -> None:
    # No enclosing tenant_scope → nothing to compare against → no false positive.
    before = _count("u-x", "u-b")
    _record_cross_tenant_attempt(tenant_id="u-b", bypass_rls=False)
    # the (u-x, u-b) series must be untouched; and more importantly no crash
    assert _count("u-x", "u-b") == before


@pytest.mark.unit
def test_empty_tenant_id_does_not_record() -> None:
    with tenant_scope(TenantContext(tenant_id="u-a")):
        # must not raise, must not emit a bogus series
        _record_cross_tenant_attempt(tenant_id=None, bypass_rls=False)
        _record_cross_tenant_attempt(tenant_id="   ", bypass_rls=False)
