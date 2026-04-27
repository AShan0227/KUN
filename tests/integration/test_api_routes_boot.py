"""Smoke test — FastAPI app should boot and register all expected routes.

Runs without a real DB (endpoints that hit DB are skipped here; covered by
integration_db suite with testcontainers).
"""

from __future__ import annotations

import pytest
from kun.api.main import app


@pytest.mark.integration
def test_routes_registered():
    paths = {getattr(r, "path", None) for r in app.routes}
    expected = {
        "/",
        "/metrics",
        "/health/",
        "/health/ready",
        "/api/chat/run",
        "/api/lab/experiments",
        "/api/lab/experiments/{experiment_id}",
        "/api/lab/run",
        "/ws",
        "/nuo/health/summary",
        "/nuo/budget/summary",
        "/nuo/actions/pending",
        "/nuo/actions/{action_id}/decision",
    }
    assert expected <= paths, f"missing routes: {expected - paths}"


@pytest.mark.integration
def test_app_version():
    assert app.version == "0.1.0"
