"""Unit tests for kun.core.logging structlog processors.

Regression guard for audit finding F002/F006: in production current_tenant()
raises MissingTenantContextError (a RuntimeError, NOT a LookupError) when there
is no explicit tenant scope; the _add_tenant processor must swallow it so a log
call outside tenant context never crashes startup / background workers.
"""

from __future__ import annotations

import pytest

from kun.core import logging as kun_logging
from kun.core.tenancy import (
    MissingTenantContextError,
    TenantContext,
    tenant_scope,
)


def test_add_tenant_swallows_missing_tenant_context(monkeypatch):
    """Production path: current_tenant() raising MissingTenantContextError must
    NOT propagate out of the log processor (would otherwise crash the process)."""

    def _raise() -> TenantContext:
        raise MissingTenantContextError("explicit tenant context is required")

    monkeypatch.setattr(kun_logging, "current_tenant", _raise)

    event_dict = {"event": "startup"}
    # Must not raise.
    out = kun_logging._add_tenant(None, "info", event_dict)
    assert out is event_dict
    assert "tenant_id" not in out


def test_add_tenant_swallows_lookup_error(monkeypatch):
    """The original LookupError path stays handled."""

    def _raise() -> TenantContext:
        raise LookupError

    monkeypatch.setattr(kun_logging, "current_tenant", _raise)
    out = kun_logging._add_tenant(None, "info", {"event": "x"})
    assert "tenant_id" not in out


def test_add_tenant_attaches_labels_when_context_present():
    """When a tenant scope is active the labels are attached."""
    with tenant_scope(TenantContext(tenant_id="u-test", user_id="user-1")):
        out = kun_logging._add_tenant(None, "info", {"event": "y"})
    assert out["tenant_id"] == "u-test"
    assert out["user_id"] == "user-1"


def test_add_tenant_does_not_overwrite_existing_labels():
    with tenant_scope(TenantContext(tenant_id="u-test")):
        out = kun_logging._add_tenant(None, "info", {"event": "z", "tenant_id": "u-explicit"})
    assert out["tenant_id"] == "u-explicit"


def test_add_tenant_propagates_unexpected_errors(monkeypatch):
    """Only the two known context errors are swallowed — genuine bugs still surface."""

    def _raise() -> TenantContext:
        raise ValueError("unexpected")

    monkeypatch.setattr(kun_logging, "current_tenant", _raise)
    with pytest.raises(ValueError):
        kun_logging._add_tenant(None, "info", {"event": "boom"})
