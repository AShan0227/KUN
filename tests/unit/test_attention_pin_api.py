"""Tests for attention_pin API (V2.1 §3.5 + §18.4)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.attention_pin import router
from kun.core.attention_anchor import reset_manager


@pytest.fixture
def client() -> TestClient:
    reset_manager()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_create_pin_basic(client: TestClient) -> None:
    resp = client.post(
        "/api/preferences/pin",
        json={
            "target_asset_ref": "ka-postgres-syntax",
            "weight_boost": 0.2,
            "reason": "我用 PostgreSQL, 所有 SQL 例子要 PG 语法",
        },
        headers={"X-User-Id": "u-007", "X-Tenant-Id": "t-001"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["anchor_kind"] == "user_pin"
    assert body["target_asset_ref"] == "ka-postgres-syntax"
    assert body["weight_boost"] == 0.2
    assert body["user_id"] == "u-007"
    assert body["expires_at"] is not None  # 90 天默认


def test_create_pin_weight_capped(client: TestClient) -> None:
    """weight_boost > 0.5 → 422 (Pydantic 校验)."""
    resp = client.post(
        "/api/preferences/pin",
        json={"target_asset_ref": "x", "weight_boost": 0.9},
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 422


def test_list_pins_filters_to_user(client: TestClient) -> None:
    """每个用户只看到自己的 pin."""
    client.post(
        "/api/preferences/pin", json={"target_asset_ref": "a"}, headers={"X-User-Id": "u-1"}
    )
    client.post(
        "/api/preferences/pin", json={"target_asset_ref": "b"}, headers={"X-User-Id": "u-2"}
    )
    resp = client.get("/api/preferences/pin", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["pin_count"] == 1
    assert body["pins"][0]["target_asset_ref"] == "a"


def test_delete_pin(client: TestClient) -> None:
    create = client.post(
        "/api/preferences/pin",
        json={"target_asset_ref": "x"},
        headers={"X-User-Id": "u-1"},
    )
    aid = create.json()["anchor_id"]
    resp = client.delete(f"/api/preferences/pin/{aid}", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 204
    # 列表应空
    listing = client.get("/api/preferences/pin", headers={"X-User-Id": "u-1"})
    assert listing.json()["pin_count"] == 0


def test_delete_pin_others_user_forbidden(client: TestClient) -> None:
    """不能删别人的 pin."""
    create = client.post(
        "/api/preferences/pin",
        json={"target_asset_ref": "x"},
        headers={"X-User-Id": "u-1"},
    )
    aid = create.json()["anchor_id"]
    resp = client.delete(f"/api/preferences/pin/{aid}", headers={"X-User-Id": "u-other"})
    assert resp.status_code == 403


def test_delete_pin_unknown_id(client: TestClient) -> None:
    resp = client.delete("/api/preferences/pin/aa-nonexistent", headers={"X-User-Id": "u-1"})
    assert resp.status_code == 404


def test_anchors_endpoint_with_boost(client: TestClient) -> None:
    """anchors 端点带 boost 计算 (调试用)."""
    client.post(
        "/api/preferences/pin",
        json={"target_asset_ref": "ka-x", "weight_boost": 0.3},
        headers={"X-User-Id": "u-1"},
    )
    resp = client.get(
        "/api/preferences/pin/anchors?asset_ref=ka-x",
        headers={"X-User-Id": "u-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["anchor_count"] >= 1
    assert body["boost_for_asset"] == 0.3
