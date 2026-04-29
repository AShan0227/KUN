"""C17 starter-pack executable skills."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from kun.skills.builtin import BUILTIN_MANIFESTS
from kun.skills.builtin import starter_pack_utils as spu
from kun.skills.dispatcher import autoload_builtins, dispatch, list_registered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_web_summarize_fetches_and_strips_html(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        text = "<html><body><h1>Hello</h1><p>World</p></body></html>"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("kun.skills.builtin.starter_pack_utils.httpx.AsyncClient", FakeClient)

    result = await spu.web_summarize({"url": "https://example.test"})

    assert result.ok is True
    assert "Hello World" in result.output["summary"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pdf_extract_reports_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))

    result = await spu.pdf_extract({"path": "missing.pdf"})

    assert result.ok is False
    assert result.skill_id == "pdf_extract"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_image_describe_reports_file_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))
    (tmp_path / "a.png").write_bytes(b"png")

    result = await spu.image_describe({"path": "a.png"})

    assert result.ok is True
    assert "a.png" in result.output["description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_lint_runs_ruff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")

    result = await spu.code_lint({"path": "ok.py", "timeout_sec": 30})

    assert result.ok is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_format_runs_ruff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))
    target = tmp_path / "messy.py"
    target.write_text("x=1\n", encoding="utf-8")

    result = await spu.code_format({"path": "messy.py", "timeout_sec": 30})

    assert result.ok is True
    assert "x = 1" in target.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_git_diff_review_counts_changes() -> None:
    diff = """diff --git a/a.py b/a.py
+++ b/a.py
+print("new")
-print("old")
"""

    result = await spu.git_diff_review({"diff": diff})

    assert result.ok is True
    assert result.output["additions"] == 1
    assert result.output["deletions"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sql_query_allows_only_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("CREATE TABLE items (name text)")
    conn.execute("INSERT INTO items VALUES ('kun')")
    conn.commit()
    conn.close()

    result = await spu.sql_query({"path": "db.sqlite", "sql": "select name from items"})
    denied = await spu.sql_query({"path": "db.sqlite", "sql": "delete from items"})

    assert result.ok is True
    assert result.output["rows"] == [["kun"]]
    assert denied.ok is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_csv_analyze_summarizes_numeric_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))
    (tmp_path / "data.csv").write_text("name,score\na,1\nb,3\n", encoding="utf-8")

    result = await spu.csv_analyze({"path": "data.csv"})

    assert result.ok is True
    assert result.output["row_count"] == 2
    assert result.output["numeric"]["score"]["avg"] == 2.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_markdown_to_docx_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))

    result = await spu.markdown_to_docx({"markdown": "# KUN", "output_path": "out.docx"})

    assert result.ok is True
    assert (tmp_path / "out.docx").read_bytes().startswith(b"PK")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_markdown_to_pdf_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUN_SKILL_FILE_ROOT", str(tmp_path))

    result = await spu.markdown_to_pdf({"markdown": "# KUN", "output_path": "out.pdf"})

    assert result.ok is True
    assert (tmp_path / "out.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_translate_returns_placeholder_translation() -> None:
    result = await spu.translate({"text": "hello", "target_language": "zh"})

    assert result.ok is True
    assert result.output["translated_text"] == "hello"
    assert result.output["target_language"] == "zh"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regex_explain_validates_and_explains() -> None:
    result = await spu.regex_explain({"pattern": r"^\d+$"})

    assert result.ok is True
    assert "digits" in result.output["explanation"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cron_explain_parses_five_fields() -> None:
    result = await spu.cron_explain({"expr": "0 9 * * 1"})

    assert result.ok is True
    assert result.output["fields"]["hour"] == "9"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_validate_checks_required_and_types() -> None:
    result = await spu.json_validate(
        {
            "json": '{"name":"kun","score":"bad"}',
            "schema": {
                "required": ["name", "score"],
                "properties": {"score": {"type": "number"}},
            },
        }
    )

    assert result.ok is False
    assert "score expected number" in result.output["errors"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_time_zone_convert_outputs_target_zone() -> None:
    result = await spu.time_zone_convert(
        {"datetime": "2026-01-01T00:00:00", "from_tz": "UTC", "to_tz": "Asia/Shanghai"}
    )

    assert result.ok is True
    assert result.output["datetime"].startswith("2026-01-01T08:00:00")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autoload_registers_expanded_starter_pack() -> None:
    autoload_builtins()
    registered = set(list_registered())
    new_skills = {
        "web_summarize",
        "pdf_extract",
        "image_describe",
        "code_lint",
        "code_format",
        "git_diff_review",
        "sql_query",
        "csv_analyze",
        "markdown_to_docx",
        "markdown_to_pdf",
        "translate",
        "regex_explain",
        "cron_explain",
        "json_validate",
        "time_zone_convert",
    }

    assert new_skills <= registered
    assert new_skills <= set(BUILTIN_MANIFESTS)
    dispatched = await dispatch("cron_explain", {"expr": "0 9 * * 1"})
    assert dispatched.ok is True
