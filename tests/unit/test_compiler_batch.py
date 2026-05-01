import json

import pytest
from kun.compiler import (
    CompilerBatchIngestor,
    CompilerBatchItem,
    CompilerBatchManifest,
    CompilerIngestor,
    LightweightMaterialCompiler,
)
from kun.context.assets import AssetLayer
from kun.context.storage import InMemoryAssetStore


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_ingests_text_path_and_skips_url_by_default(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    note = root / "note.md"
    note.write_text("# KUN\n\nBatch compiler", encoding="utf-8")
    store = InMemoryAssetStore()
    batch = CompilerBatchIngestor(ingestor=CompilerIngestor(store=store))

    report = await batch.ingest_manifest(
        CompilerBatchManifest(
            tenant_id="tenant-batch",
            layer=AssetLayer.L2_PROJECT,
            allowed_root=str(root),
            items=[
                CompilerBatchItem(
                    id="inline",
                    type="text",
                    value="inline compiler note",
                    declared_kind="plain_text",
                ),
                CompilerBatchItem(id="path", type="path", value="note.md"),
                CompilerBatchItem(id="url", type="url", value="https://example.com/report.html"),
            ],
        )
    )

    assert report.total == 3
    assert report.stored == 2
    assert report.skipped == 1
    assert report.errors == 0
    assert [result.status for result in report.results] == ["stored", "stored", "skipped"]
    assert report.results[2].reason == "material_status_placeholder"
    stored = await store.list(tenant_id="tenant-batch")
    assert len(stored) == 2
    assert {asset.layer for asset in stored} == {AssetLayer.L2_PROJECT}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_path_requires_allowed_root() -> None:
    report = await CompilerBatchIngestor().ingest_manifest(
        CompilerBatchManifest(
            tenant_id="tenant-batch",
            items=[CompilerBatchItem(id="path", type="path", value="note.md")],
        )
    )

    assert report.stored == 0
    assert report.errors == 1
    assert report.results[0].status == "error"
    assert "allowed_root" in report.results[0].reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_url_stores_when_fetch_is_allowlisted() -> None:
    async def fetcher(_url: str, _max_bytes: int) -> tuple[str, bytes]:
        return "text/html", b"<h1>KUN docs</h1>"

    store = InMemoryAssetStore()
    compiler = LightweightMaterialCompiler(
        url_fetch_enabled=True,
        allowed_url_hosts={"docs.example.com"},
        url_fetcher=fetcher,
    )
    batch = CompilerBatchIngestor(
        ingestor=CompilerIngestor(compiler=compiler, store=store),
    )

    report = await batch.ingest_manifest(
        CompilerBatchManifest(
            tenant_id="tenant-batch",
            items=[
                CompilerBatchItem(
                    id="url",
                    type="url",
                    value="https://docs.example.com/index.html",
                )
            ],
        )
    )

    assert report.stored == 1
    assert report.results[0].source_uri == "https://docs.example.com/index.html"
    assert report.results[0].summary == "KUN docs"
    stored = await store.list(tenant_id="tenant-batch")
    assert len(stored) == 1
    assert stored[0].l1_metadata["source"]["type"] == "url"


def test_batch_manifest_accepts_json_shape() -> None:
    payload = {
        "tenant_id": "tenant-batch",
        "items": [
            {"id": "one", "type": "text", "value": "hello"},
            {"id": "two", "type": "url", "value": "https://example.com"},
        ],
    }

    manifest = CompilerBatchManifest.model_validate(json.loads(json.dumps(payload)))

    assert manifest.tenant_id == "tenant-batch"
    assert manifest.items[0].type == "text"
    assert manifest.items[1].type == "url"
