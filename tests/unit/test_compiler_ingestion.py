"""Compiler → Context AssetStore bridge tests."""

import pytest
from kun.compiler import (
    CompilerIngestor,
    LightweightMaterialCompiler,
    material_to_layered_asset,
)
from kun.context.assets import AssetLayer
from kun.context.storage import InMemoryAssetStore


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_text_stores_compiled_material_as_knowledge_asset() -> None:
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_text(
        "# KUN\n\nCompiler layer",
        tenant_id="tenant-compiler",
        source_uri="brief.md",
        declared_kind="markdown",
        layer=AssetLayer.L2_PROJECT,
    )

    assert result.stored is True
    assert result.asset_id == result.material.asset_id
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.asset_kind == "knowledge"
    assert stored.layer == AssetLayer.L2_PROJECT
    assert stored.l1_metadata["kind"] == "markdown"
    assert stored.l1_metadata["compiler_profile"]["name"] == "kun-v5-lightweight"
    assert stored.l2_summary.startswith("# KUN")
    assert "compiler" in stored.tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_bytes_stores_pdf_material_without_text_flattening() -> None:
    raw = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_bytes(
        raw,
        tenant_id="tenant-compiler",
        source_uri="attachment:brief.pdf",
        mime_type="application/pdf",
        layer=AssetLayer.L2_PROJECT,
        metadata={"source": "chat_attachment"},
    )

    assert result.stored is True
    assert result.material.kind == "pdf"
    assert result.material.source.type == "bytes"
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.l1_metadata["kind"] == "pdf"
    assert stored.l1_metadata["source"]["type"] == "bytes"
    assert stored.l1_metadata["material_metadata"]["source"] == "chat_attachment"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_rejected_path_is_not_stored(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_path(
        outside,
        tenant_id="tenant-compiler",
        allowed_root=root,
    )

    assert result.stored is False
    assert result.reason == "material_status_rejected"
    assert await store.list(tenant_id="tenant-compiler") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_url_stores_allowlisted_fetched_material() -> None:
    async def fetcher(_url: str, _max_bytes: int) -> tuple[str, bytes]:
        return "text/html", b"<h1>KUN URL</h1><p>compiled from web</p>"

    store = InMemoryAssetStore()
    compiler = LightweightMaterialCompiler(
        url_fetch_enabled=True,
        allowed_url_hosts={"docs.example.com"},
        url_fetcher=fetcher,
    )
    ingestor = CompilerIngestor(compiler=compiler, store=store)

    result = await ingestor.ingest_url(
        "https://docs.example.com/report.html",
        tenant_id="tenant-compiler",
        layer=AssetLayer.L2_PROJECT,
    )

    assert result.stored is True
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.l1_metadata["source"]["type"] == "url"
    assert stored.l1_metadata["material_metadata"]["url_fetch_enabled"] is True
    assert stored.layer == AssetLayer.L2_PROJECT
    assert "compiled from web" in (stored.l2_summary or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_material_to_layered_asset_skips_unsupported_url() -> None:
    material = await LightweightMaterialCompiler().compile_url(
        "https://example.com/data",
        tenant_id="tenant-compiler",
    )

    assert material.status == "placeholder"
    assert material_to_layered_asset(material) is None
