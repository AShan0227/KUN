from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException
from kun.api.input_payload import Attachment, translate_binary_input, translate_chat_input


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
