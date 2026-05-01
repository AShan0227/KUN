from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from kun.api.main import app
from kun.context.storage import InMemoryAssetStore, reset_store, set_store


@pytest.mark.unit
def test_compiler_api_ingests_manifest_for_current_tenant(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# KUN\n\nCompiler API", encoding="utf-8")
    store = InMemoryAssetStore()
    set_store(store)
    try:
        response = TestClient(app).post(
            "/api/compiler/ingest-manifest",
            headers={"X-Tenant-Id": "tenant-api", "X-User-Id": "user-api"},
            json={
                "allowed_root": str(root),
                "layer": "L2_project",
                "items": [
                    {
                        "id": "path",
                        "type": "path",
                        "value": "note.md",
                        "tenant_id": "evil-tenant",
                    },
                    {
                        "id": "inline",
                        "type": "text",
                        "value": "inline compiler api",
                    },
                    {
                        "id": "url",
                        "type": "url",
                        "value": "https://example.com/report.html",
                    },
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["tenant_id"] == "tenant-api"
        assert payload["stored"] == 2
        assert payload["skipped"] == 1
        assert payload["results"][2]["reason"] == "material_status_placeholder"
        stored_active = asyncio.run(store.list(tenant_id="tenant-api"))
        stored_evil = asyncio.run(store.list(tenant_id="evil-tenant"))
        assert len(stored_active) == 2
        assert stored_evil == []
    finally:
        reset_store()


@pytest.mark.unit
def test_compiler_api_path_requires_allowed_root() -> None:
    response = TestClient(app).post(
        "/api/compiler/ingest-manifest",
        headers={"X-Tenant-Id": "tenant-api"},
        json={"items": [{"id": "path", "type": "path", "value": "note.md"}]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "path items require allowed_root"


@pytest.mark.unit
def test_compiler_api_requires_scope_when_scopes_are_present() -> None:
    response = TestClient(app).post(
        "/api/compiler/ingest-manifest",
        headers={"X-Tenant-Id": "tenant-api", "X-Scopes": "world:dispatch"},
        json={"items": [{"id": "inline", "type": "text", "value": "hello"}]},
    )

    assert response.status_code == 403
    assert "context:write" in response.json()["detail"]
