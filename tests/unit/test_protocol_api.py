from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from kun.api.protocols import router
from kun.core.tenancy import TenantContext, tenant_scope
from kun.qi import InMemoryProtocolStorage, ProtocolRegistry


def _app() -> FastAPI:
    app = FastAPI()
    app.state.protocol_registry = ProtocolRegistry(InMemoryProtocolStorage())

    @app.middleware("http")
    async def tenant_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        with tenant_scope(TenantContext(tenant_id=request.headers.get("X-Tenant-Id", "t-1"))):
            return await call_next(request)

    app.include_router(router)
    return app


def _protocol_payload(status: str = "stable") -> dict:
    return {
        "protocol_id": "writing.short",
        "version": "1.0.0",
        "tenant_id": "ignored",
        "status": status,
        "trigger": {"task_type_pattern": "writing.*"},
        "execution": {"mode": "SMART", "llm_strategy": "tier_strong_mid_temp"},
    }


def test_protocol_api_save_list_get_match() -> None:
    client = TestClient(_app())

    saved = client.post(
        "/api/protocols",
        json=_protocol_payload(),
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert saved.status_code == 200
    assert saved.json()["protocol_id"] == "writing.short"

    listed = client.get("/api/protocols", headers={"X-Tenant-Id": "tenant-a"})
    assert listed.status_code == 200
    assert listed.json()[0]["tenant_id"] == "tenant-a"

    detail = client.get(
        "/api/protocols/writing.short/versions/1.0.0",
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert detail.status_code == 200
    assert detail.json()["execution"]["mode"] == "SMART"

    matched = client.post(
        "/api/protocols/match",
        json={"task_meta": {"task_type": "writing.email", "risk_level": "low"}},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert matched.status_code == 200
    assert matched.json()["protocol_id"] == "writing.short"


def test_protocol_api_promote_and_rollback() -> None:
    client = TestClient(_app())

    client.post(
        "/api/protocols",
        json=_protocol_payload(status="experimental"),
        headers={"X-Tenant-Id": "tenant-a"},
    )
    promote = client.post(
        "/api/protocols/writing.short/versions/1.0.0/promote",
        json={"target_status": "shadow"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert promote.status_code == 200
    assert promote.json()["status"] == "shadow"

    rollback = client.post(
        "/api/protocols/writing.short/versions/1.0.0/rollback",
        json={"reason": "bad canary"},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "rolled_back"
