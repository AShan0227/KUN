from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from kun.interface.input_translator import ContentExtractor, InputDescriptor, RealWorldTranslator

FIXTURES = Path(__file__).parents[1] / "fixtures" / "input_samples"


def png_bytes(width: int = 10, height: int = 10) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )


def mp3_bytes() -> bytes:
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hello")
    return buffer.getvalue()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_text_kind_json() -> None:
    descriptor = await RealWorldTranslator().detect_text_kind('{"task": "classify"}')

    assert descriptor.kind == "json"
    assert descriptor.suggested_handler == "direct_llm"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_text_kind_markdown() -> None:
    descriptor = await RealWorldTranslator().detect_text_kind("# Title\n\n- item")

    assert descriptor.kind == "markdown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_text_kind_code() -> None:
    descriptor = await RealWorldTranslator().detect_text_kind("def run(x):\n    return x + 1")

    assert descriptor.kind == "code"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_text_kind_sql() -> None:
    descriptor = await RealWorldTranslator().detect_text_kind("SELECT id FROM tasks WHERE done = 0")

    assert descriptor.kind == "sql"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_text_kind_plain_text() -> None:
    descriptor = await RealWorldTranslator().detect_text_kind(
        "please summarize this normal sentence"
    )

    assert descriptor.kind == "plain_text"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_pdf() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(pdf_bytes())

    assert descriptor.kind == "pdf"
    assert descriptor.suggested_handler == "pdf_extract"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_png() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(png_bytes())

    assert descriptor.kind == "image_png"
    assert descriptor.suggested_handler == "vision_llm"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_csv_fixture() -> None:
    descriptor = await RealWorldTranslator().detect(FIXTURES / "minimal.csv")

    assert descriptor.kind == "csv"
    assert descriptor.suggested_handler == "csv_query"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_mp3() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(mp3_bytes())

    assert descriptor.kind == "audio_mp3"
    assert descriptor.suggested_handler == "transcribe"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_zip() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(zip_bytes())

    assert descriptor.kind == "archive_zip"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_file_kind_unknown_binary() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(b"\x00\x01\x02\x03\x04\x05")

    assert descriptor.kind == "binary_unknown"
    assert descriptor.suggested_handler == "ask_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_extractor_text_first_500_chars() -> None:
    descriptor = InputDescriptor(
        kind="plain_text",
        mime_type="text/plain",
        confidence=1.0,
        suggested_handler="direct_llm",
    )

    summary = await ContentExtractor().extract_summary(descriptor, b"a" * 600)

    assert summary == "a" * 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_extractor_csv_header_and_preview() -> None:
    descriptor = await RealWorldTranslator().detect(FIXTURES / "minimal.csv")

    summary = await ContentExtractor().extract_summary(
        descriptor,
        (FIXTURES / "minimal.csv").read_bytes(),
    )

    assert "columns: name, value" in summary
    assert "alpha,1" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_extractor_image_dimensions() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(png_bytes(width=12, height=8))

    summary = await ContentExtractor().extract_summary(descriptor, png_bytes(width=12, height=8))

    assert "dimensions: 12x8" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_extractor_pdf_fallback_summary() -> None:
    descriptor = await RealWorldTranslator().detect_file_kind(pdf_bytes())

    summary = await ContentExtractor().extract_summary(descriptor, pdf_bytes())

    assert "PDF document" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_anchor_then_expand_yields_three_rounds() -> None:
    rounds = [item async for item in RealWorldTranslator().detect_anchor_then_expand(png_bytes())]

    assert [item.metadata["anchor_round"] for item in rounds] == [1, 2, 3]
    assert rounds[0].content_summary == ""
    assert "dimensions: 10x10" in rounds[1].content_summary
    assert rounds[2].metadata["deep_understanding"] == "not_configured"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_anchor_then_expand_uses_optional_deep_analyzer() -> None:
    async def analyzer(descriptor: InputDescriptor, raw: bytes) -> str:
        return f"deep:{descriptor.kind}:{len(raw)}"

    rounds = [
        item
        async for item in RealWorldTranslator(deep_analyzer=analyzer).detect_anchor_then_expand(
            b"normal text"
        )
    ]

    assert rounds[2].content_summary == "deep:plain_text:11"
    assert rounds[2].metadata["deep_understanding"] == "provided"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_anchor_then_expand_marks_deep_analyzer_failure() -> None:
    async def analyzer(_descriptor: InputDescriptor, _raw: bytes) -> str:
        raise RuntimeError("no model")

    rounds = [
        item
        async for item in RealWorldTranslator(deep_analyzer=analyzer).detect_anchor_then_expand(
            b"normal text"
        )
    ]

    assert rounds[2].metadata["deep_understanding"] == "unavailable"
    assert rounds[2].metadata["deep_error_type"] == "RuntimeError"
    assert rounds[2].content_summary == rounds[1].content_summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detect_anchor_then_expand_caller_can_stop_after_anchor() -> None:
    stream = RealWorldTranslator().detect_anchor_then_expand(b"normal text")
    first = await anext(stream)
    await stream.aclose()

    assert first.kind == "plain_text"
    assert first.metadata["anchor_round"] == 1
