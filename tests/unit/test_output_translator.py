"""OutputTranslator 单测 (V2.2 §23 后续 / Wire 12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kun.interface.output_translator import (
    EmailDraft,
    OutputTranslator,
    get_default_translator,
    reset_default_translator,
)


@pytest.fixture
def tmp_translator(tmp_path: Path) -> OutputTranslator:
    return OutputTranslator(workspace_root=tmp_path)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_default_translator()
    yield
    reset_default_translator()


# ---- text_plain ----


@pytest.mark.asyncio
async def test_text_plain_passthrough(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate("hello world", target_format="text_plain")
    assert d.format == "text_plain"
    assert d.mime_type == "text/plain"
    assert d.payload_text == "hello world"
    assert d.requires_user_approval is False


@pytest.mark.asyncio
async def test_text_plain_non_string(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate(42, target_format="text_plain")
    assert d.payload_text == "42"


# ---- markdown ----


@pytest.mark.asyncio
async def test_markdown_string_passthrough(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate("# hello", target_format="markdown")
    assert d.payload_text == "# hello"


@pytest.mark.asyncio
async def test_markdown_dict_renders_keys(tmp_translator: OutputTranslator) -> None:
    payload = {"title": "Q4 Plan", "items": ["A", "B"]}
    d = await tmp_translator.translate(payload, target_format="markdown")
    assert "**title**" in d.payload_text
    assert "Q4 Plan" in d.payload_text
    assert "- A" in d.payload_text
    assert "- B" in d.payload_text


# ---- pdf ----


@pytest.mark.asyncio
async def test_pdf_writes_placeholder_file(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate({"title": "x"}, target_format="pdf", filename_hint="q4-plan")
    assert d.format == "pdf"
    assert d.mime_type == "application/pdf"
    assert d.payload_bytes_ref
    p = Path(d.payload_bytes_ref)
    assert p.is_file()  # noqa: ASYNC240
    content = p.read_text(encoding="utf-8")  # noqa: ASYNC240
    assert "Placeholder PDF" in content
    assert "real_pdf_pending_skill" in d.metadata


# ---- docx ----


@pytest.mark.asyncio
async def test_docx_writes_placeholder_file(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate("# title", target_format="docx")
    assert d.format == "docx"
    assert Path(d.payload_bytes_ref).is_file()  # noqa: ASYNC240


# ---- csv ----


@pytest.mark.asyncio
async def test_csv_list_of_dicts(tmp_translator: OutputTranslator) -> None:
    rows = [{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]
    d = await tmp_translator.translate(rows, target_format="csv")
    assert d.format == "csv"
    lines = d.payload_text.split("\n")
    assert lines[0] == "name,age"
    assert "alice,30" in lines
    assert "bob,25" in lines
    assert d.metadata["rows"] == 2
    assert d.metadata["columns"] == 2


@pytest.mark.asyncio
async def test_csv_empty_list(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate([], target_format="csv")
    assert d.payload_text == ""
    assert d.metadata["rows"] == 0


@pytest.mark.asyncio
async def test_csv_invalid_payload_raises(tmp_translator: OutputTranslator) -> None:
    with pytest.raises(ValueError):
        await tmp_translator.translate(["not a dict"], target_format="csv")


@pytest.mark.asyncio
async def test_csv_escapes_commas_in_values(tmp_translator: OutputTranslator) -> None:
    rows = [{"text": "hello, world"}]
    d = await tmp_translator.translate(rows, target_format="csv")
    # 逗号应该被换成分号 (避免 CSV 解析歧义)
    assert "hello; world" in d.payload_text


# ---- xlsx ----


@pytest.mark.asyncio
async def test_xlsx_writes_placeholder_file(tmp_translator: OutputTranslator) -> None:
    rows = [{"a": 1}]
    d = await tmp_translator.translate(rows, target_format="xlsx")
    assert d.format == "xlsx"
    assert Path(d.payload_bytes_ref).is_file()  # noqa: ASYNC240


# ---- json ----


@pytest.mark.asyncio
async def test_json_dict_serializes(tmp_translator: OutputTranslator) -> None:
    d = await tmp_translator.translate({"k": "v", "n": 1}, target_format="json")
    assert d.format == "json"
    parsed = json.loads(d.payload_text)
    assert parsed == {"k": "v", "n": 1}


# ---- email_draft ----


@pytest.mark.asyncio
async def test_email_draft_from_object(tmp_translator: OutputTranslator) -> None:
    draft = EmailDraft(
        to=["alice@example.com"],
        subject="Hello",
        body="Hi there!",
    )
    d = await tmp_translator.translate(draft, target_format="email_draft")
    assert d.format == "email_draft"
    assert d.requires_user_approval is True  # 邮件真发要审批
    assert "alice@example.com" in d.payload_text
    assert "Hello" in d.payload_text


@pytest.mark.asyncio
async def test_email_draft_from_dict(tmp_translator: OutputTranslator) -> None:
    payload = {"to": ["x@y.com"], "subject": "S", "body": "B"}
    d = await tmp_translator.translate(payload, target_format="email_draft")
    assert d.requires_user_approval is True


@pytest.mark.asyncio
async def test_email_draft_invalid_raises(tmp_translator: OutputTranslator) -> None:
    with pytest.raises(ValueError):
        await tmp_translator.translate("not a dict", target_format="email_draft")


# ---- 错误格式 ----


@pytest.mark.asyncio
async def test_unsupported_format_raises(tmp_translator: OutputTranslator) -> None:
    with pytest.raises(ValueError):
        await tmp_translator.translate({}, target_format="bogus")  # type: ignore[arg-type]


# ---- singleton ----


def test_get_default_translator_returns_singleton() -> None:
    a = get_default_translator()
    b = get_default_translator()
    assert a is b


def test_reset_default_translator() -> None:
    a = get_default_translator()
    reset_default_translator()
    b = get_default_translator()
    assert a is not b
