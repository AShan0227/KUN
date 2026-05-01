from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from kun.api.main import app
from kun.context.storage import InMemoryAssetStore, reset_store, set_store
from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue


@pytest.mark.unit
def test_compiler_api_ingests_manifest_for_current_tenant(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text(
        "# KUN\n\nCompiler API hot ingest stores this safe project note after review.",
        encoding="utf-8",
    )
    store = InMemoryAssetStore()
    set_store(store)
    reset_qi_problem_queue()
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
                        "value": "inline compiler api material with enough detail to pass review safely",
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
        assert payload["results"][2]["reason"].startswith("compiler_review_")
        stored_active = asyncio.run(store.list(tenant_id="tenant-api"))
        stored_evil = asyncio.run(store.list(tenant_id="evil-tenant"))
        assert len(stored_active) == 2
        assert stored_evil == []
        queued = get_qi_problem_queue().list("tenant-api")
        assert any(signal.source == "compiler.intake_review.package" for signal in queued)
    finally:
        reset_store()
        reset_qi_problem_queue()


@pytest.mark.unit
def test_compiler_api_does_not_store_low_quality_text(monkeypatch) -> None:
    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    store = InMemoryAssetStore()
    set_store(store)
    reset_qi_problem_queue()
    try:
        response = TestClient(app).post(
            "/api/compiler/ingest-manifest",
            headers={"X-Tenant-Id": "tenant-api", "X-User-Id": "user-api"},
            json={"items": [{"id": "short", "type": "text", "value": "too short"}]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["stored"] == 0
        assert payload["skipped"] == 1
        assert payload["results"][0]["reason"].startswith(
            "compiler_review_compiled_hold_for_review"
        )
        stored_active = asyncio.run(store.list(tenant_id="tenant-api"))
        assert stored_active == []
        queued = get_qi_problem_queue().list("tenant-api")
        assert len(queued) == 1
        assert queued[0].evidence["auto_ingest_allowed"] is False
    finally:
        reset_store()
        reset_qi_problem_queue()


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
