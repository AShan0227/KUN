"""Honest delivery status for NUO/KUN."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.nuo.health_panel import router
from kun.engineering.delivery_status import get_v3_delivery_status, validate_delivery_status
from kun.world.gateway import EmailSendHandler, WorldGateway


def test_delivery_status_is_honest_about_incomplete_capabilities() -> None:
    items = get_v3_delivery_status()

    by_id = {item.capability_id: item for item in items}
    assert by_id["llm_provider"].status == "ready"
    assert by_id["llm_provider"].can_claim_complete is True
    assert by_id["world_gateway"].status == "partial"
    assert by_id["production_deployment"].status == "not_ready"
    assert by_id["world_gateway"].can_claim_complete is False
    assert any("local_file.write" in item for item in by_id["world_gateway"].done)
    assert validate_delivery_status(items) == []


def test_delivery_status_endpoint() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/delivery-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["ready"] >= 1
    assert body["summary"]["not_ready"] >= 1
    assert body["validation_issues"] == []
    assert any(item["capability_id"] == "world_gateway" for item in body["items"])


def test_delivery_status_derives_world_gateway_capabilities_from_registry(tmp_path) -> None:
    async def sender(_message):
        return {"provider_message_id": "smtp-test"}

    gateway = WorldGateway(
        artifact_root=tmp_path,
        handlers=[
            EmailSendHandler(
                output_root=tmp_path,
                smtp_host="smtp.example.com",
                smtp_port=587,
                smtp_username=None,
                smtp_password=None,
                smtp_from="kun@example.com",
                sender=sender,
            )
        ],
    )

    items = get_v3_delivery_status(world_gateway=gateway)
    world_gateway = {item.capability_id: item for item in items}["world_gateway"]

    assert any("email.send 已注册真实执行 handler" in item for item in world_gateway.done)
    assert not any(item.startswith("真实邮件发送") for item in world_gateway.missing)
    assert any(item.startswith("真实浏览器操作") for item in world_gateway.missing)
