"""Honest delivery status for NUO/KUN."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.nuo.health_panel import router
from kun.engineering.delivery_status import get_v3_delivery_status


def test_delivery_status_is_honest_about_incomplete_capabilities() -> None:
    items = get_v3_delivery_status()

    by_id = {item.capability_id: item for item in items}
    assert by_id["llm_provider"].status == "ready"
    assert by_id["world_gateway"].status == "audit_only"
    assert by_id["production_deployment"].status == "not_ready"
    assert by_id["world_gateway"].can_claim_complete is False
    assert "external_dispatched=false" in " ".join(by_id["world_gateway"].done)


def test_delivery_status_endpoint() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/delivery-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["ready"] >= 1
    assert body["summary"]["not_ready"] >= 1
    assert any(item["capability_id"] == "world_gateway" for item in body["items"])
