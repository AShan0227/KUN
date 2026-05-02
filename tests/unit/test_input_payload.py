from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import HTTPException
from kun.api.input_payload import Attachment, translate_binary_input, translate_chat_input
from kun.context.storage import InMemoryAssetStore, reset_store, set_store


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    )


@pytest.mark.asyncio
async def test_translate_chat_input_appends_text_attachment() -> None:
    translated = await translate_chat_input(
        "请总结附件",
        [Attachment(filename="note.txt", content_b64=_b64(b"hello attachment"))],
    )

    assert "请总结附件" in translated.message
    assert "[Attachment: note.txt]" in translated.message
    assert "hello attachment" in translated.message
    assert translated.descriptors[0]["kind"] == "plain_text"


@pytest.mark.asyncio
async def test_translate_chat_input_extracts_pdf_summary() -> None:
    translated = await translate_chat_input(
        "读这个 PDF",
        [Attachment(filename="brief.pdf", content_b64=_b64(_pdf_bytes()))],
    )

    assert "brief.pdf" in translated.message
    assert "kind: pdf" in translated.message
    assert translated.descriptors[0]["suggested_handler"] == "pdf_extract"


@pytest.mark.asyncio
async def test_translate_chat_input_rejects_image_until_ocr_exists() -> None:
    with pytest.raises(HTTPException) as exc:
        await translate_chat_input(
            "看图",
            [Attachment(filename="screen.png", content_b64=_b64(_png_bytes()))],
        )

    assert exc.value.status_code == 415
    assert "OCR" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_translate_binary_input_wraps_websocket_bytes() -> None:
    translated = await translate_binary_input(b"raw websocket text", filename="ws.txt")

    assert "[Attachment: ws.txt]" in translated.message
    assert "raw websocket text" in translated.message
    assert translated.descriptors[0]["filename"] == "ws.txt"


@pytest.mark.asyncio
async def test_translate_chat_input_can_compile_attachment_into_asset_store() -> None:
    store = InMemoryAssetStore()
    set_store(store)
    try:
        translated = await translate_chat_input(
            "记住这个材料",
            [Attachment(filename="note.md", content_b64=_b64(b"# Title\n\nUseful material"))],
            tenant_id="tenant-compiler-hot-path",
            store_compiled_assets=True,
        )

        descriptor = translated.descriptors[0]
        asset_id = descriptor["compiler_asset_id"]
        assert asset_id
        assert descriptor["compiler_status"] == "stored"
        assert descriptor["compiler_kind"] == "markdown"
        assert f"compiler_asset_id: {asset_id}" in translated.message
        assert "[Hermes v5.compiler]" in translated.message
        assert "canonical_material" in translated.message

        stored = await store.get(asset_id, tenant_id="tenant-compiler-hot-path")
        assert stored is not None
        assert stored.asset_kind == "knowledge"
        assert stored.l1_metadata["material_metadata"]["source"] == "chat_attachment"
    finally:
        reset_store()


@pytest.mark.asyncio
async def test_translate_chat_input_compiles_pdf_attachment_from_raw_bytes() -> None:
    store = InMemoryAssetStore()
    set_store(store)
    raw = _pdf_bytes()
    try:
        translated = await translate_chat_input(
            "记住这个 PDF",
            [Attachment(filename="brief.pdf", content_b64=_b64(raw))],
            tenant_id="tenant-compiler-hot-path",
            store_compiled_assets=True,
        )

        descriptor = translated.descriptors[0]
        asset_id = descriptor["compiler_asset_id"]
        assert asset_id
        assert descriptor["compiler_status"] == "stored"
        assert descriptor["compiler_kind"] == "pdf"
        assert "compiler_kind: pdf" in translated.message
        assert "[Hermes v5.compiler]" in translated.message

        stored = await store.get(asset_id, tenant_id="tenant-compiler-hot-path")
        assert stored is not None
        assert stored.l1_metadata["kind"] == "pdf"
        assert stored.l1_metadata["source"]["type"] == "bytes"
        assert stored.l1_metadata["provenance"]["input_sha256"] == hashlib.sha256(raw).hexdigest()
        assert stored.l1_metadata["material_metadata"]["input_kind"] == "pdf"
    finally:
        reset_store()
