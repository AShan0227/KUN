"""Shared implementation for C17 starter-pack skills."""

from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kun.skills.builtin.file_io import _resolve
from kun.skills.builtin.pdf_read import execute as pdf_read_execute
from kun.skills.dispatcher import SkillResult

_TAG_RE = re.compile(r"<[^>]+>")
_TIMEOUT = 15.0


async def web_summarize(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    url = str(params.get("url") or "").strip()
    max_chars = max(200, min(5000, int(params.get("max_chars") or 1200)))
    if not url:
        return SkillResult(skill_id="web_summarize", ok=False, error="url is required")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = _strip_html(resp.text)
    except httpx.HTTPError as e:
        return SkillResult(skill_id="web_summarize", ok=False, error=f"http error: {e}")
    summary = _squeeze(text)[:max_chars]
    return SkillResult(
        skill_id="web_summarize",
        ok=True,
        output={"summary": summary, "source_url": url, "truncated": len(text) > max_chars},
        duration_sec=time.perf_counter() - started,
    )


async def pdf_extract(params: dict[str, Any]) -> SkillResult:
    result = await pdf_read_execute(params)
    return result.model_copy(update={"skill_id": "pdf_extract"})


async def image_describe(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or "").strip()
    if not rel:
        return SkillResult(skill_id="image_describe", ok=False, error="path is required")
    try:
        target = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id="image_describe", ok=False, error=str(e))
    if not target.is_file():
        return SkillResult(skill_id="image_describe", ok=False, error="not a file")
    return SkillResult(
        skill_id="image_describe",
        ok=True,
        output={
            "description": f"Image file {target.name} ({target.suffix.lower() or 'unknown type'}), {target.stat().st_size} bytes.",
            "path": rel,
        },
        duration_sec=time.perf_counter() - started,
        metadata={"vision_model_used": False},
    )


async def code_lint(params: dict[str, Any]) -> SkillResult:
    return _run_command_skill("code_lint", ["uv", "run", "ruff", "check"], params)


async def code_format(params: dict[str, Any]) -> SkillResult:
    return _run_command_skill("code_format", ["uv", "run", "ruff", "format"], params)


async def git_diff_review(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    diff = str(params.get("diff") or "")
    files = re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.M)
    additions = len(re.findall(r"^\+(?!\+\+)", diff, flags=re.M))
    deletions = len(re.findall(r"^-(?!--)", diff, flags=re.M))
    notes: list[str] = []
    if additions + deletions == 0:
        notes.append("No code changes detected.")
    if any("test" in file.lower() for file in files):
        notes.append("Tests changed; verify behavior and fixtures carefully.")
    if deletions > additions * 2 and deletions > 20:
        notes.append("Large deletion-heavy diff; check accidental removals.")
    return SkillResult(
        skill_id="git_diff_review",
        ok=True,
        output={"files": files, "additions": additions, "deletions": deletions, "notes": notes},
        duration_sec=time.perf_counter() - started,
    )


async def sql_query(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or "").strip()
    sql = str(params.get("sql") or "").strip()
    if not rel or not sql:
        return SkillResult(skill_id="sql_query", ok=False, error="path and sql are required")
    if not _is_readonly_sql(sql):
        return SkillResult(
            skill_id="sql_query", ok=False, error="only read-only SELECT/WITH queries are allowed"
        )
    try:
        db_path = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id="sql_query", ok=False, error=str(e))
    if not db_path.is_file():
        return SkillResult(skill_id="sql_query", ok=False, error="not a file")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cursor = conn.execute(sql)
            columns = [column[0] for column in (cursor.description or [])]
            rows = [list(row) for row in cursor.fetchmany(500)]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return SkillResult(skill_id="sql_query", ok=False, error=f"sql error: {e}")
    return SkillResult(
        skill_id="sql_query",
        ok=True,
        output={"columns": columns, "rows": rows},
        duration_sec=time.perf_counter() - started,
    )


async def csv_analyze(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or "").strip()
    if not rel:
        return SkillResult(skill_id="csv_analyze", ok=False, error="path is required")
    try:
        target = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id="csv_analyze", ok=False, error=str(e))
    if not target.is_file():
        return SkillResult(skill_id="csv_analyze", ok=False, error="not a file")
    with target.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    columns = reader.fieldnames or []
    numeric: dict[str, dict[str, float]] = {}
    for column in columns:
        values = [_as_float(row.get(column)) for row in rows]
        numbers = [value for value in values if value is not None]
        if numbers:
            numeric[column] = {
                "min": min(numbers),
                "max": max(numbers),
                "avg": sum(numbers) / len(numbers),
            }
    return SkillResult(
        skill_id="csv_analyze",
        ok=True,
        output={"row_count": len(rows), "columns": columns, "numeric": numeric},
        duration_sec=time.perf_counter() - started,
    )


async def markdown_to_docx(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    markdown = str(params.get("markdown") or params.get("content") or "")
    output_path = str(params.get("output_path") or "output.docx")
    if not markdown:
        return SkillResult(skill_id="markdown_to_docx", ok=False, error="markdown is required")
    try:
        target = _resolve(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_minimal_docx(target, markdown)
    except ValueError as e:
        return SkillResult(skill_id="markdown_to_docx", ok=False, error=str(e))
    return SkillResult(
        skill_id="markdown_to_docx",
        ok=True,
        output={"path": output_path, "bytes_written": target.stat().st_size},
        duration_sec=time.perf_counter() - started,
    )


async def markdown_to_pdf(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    markdown = str(params.get("markdown") or params.get("content") or "")
    output_path = str(params.get("output_path") or "output.pdf")
    if not markdown:
        return SkillResult(skill_id="markdown_to_pdf", ok=False, error="markdown is required")
    try:
        target = _resolve(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_minimal_pdf(target, markdown)
    except ValueError as e:
        return SkillResult(skill_id="markdown_to_pdf", ok=False, error=str(e))
    return SkillResult(
        skill_id="markdown_to_pdf",
        ok=True,
        output={"path": output_path, "bytes_written": target.stat().st_size},
        duration_sec=time.perf_counter() - started,
    )


async def translate(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    text = str(params.get("text") or "")
    target_language = str(params.get("target_language") or params.get("to") or "zh")
    if not text:
        return SkillResult(skill_id="translate", ok=False, error="text is required")
    return SkillResult(
        skill_id="translate",
        ok=True,
        output={
            "translated_text": text,
            "target_language": target_language,
            "note": "rule-based placeholder; wire LLM translator for production quality",
        },
        duration_sec=time.perf_counter() - started,
    )


async def regex_explain(params: dict[str, Any]) -> SkillResult:
    pattern = str(params.get("pattern") or "")
    if not pattern:
        return SkillResult(skill_id="regex_explain", ok=False, error="pattern is required")
    try:
        re.compile(pattern)
    except re.error as e:
        return SkillResult(skill_id="regex_explain", ok=False, error=f"invalid regex: {e}")
    return SkillResult(
        skill_id="regex_explain", ok=True, output={"explanation": _explain_regex(pattern)}
    )


async def cron_explain(params: dict[str, Any]) -> SkillResult:
    expr = str(params.get("expr") or params.get("cron") or "").strip()
    parts = expr.split()
    if len(parts) != 5:
        return SkillResult(skill_id="cron_explain", ok=False, error="cron must have 5 fields")
    minute, hour, day, month, weekday = parts
    return SkillResult(
        skill_id="cron_explain",
        ok=True,
        output={
            "explanation": f"minute={minute}, hour={hour}, day_of_month={day}, month={month}, day_of_week={weekday}",
            "fields": {
                "minute": minute,
                "hour": hour,
                "day_of_month": day,
                "month": month,
                "day_of_week": weekday,
            },
        },
    )


async def json_validate(params: dict[str, Any]) -> SkillResult:
    raw = params.get("json")
    schema = params.get("schema")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        return SkillResult(skill_id="json_validate", ok=False, error=f"invalid json: {e}")
    errors = _validate_basic_schema(value, schema if isinstance(schema, dict) else {})
    return SkillResult(
        skill_id="json_validate",
        ok=not errors,
        output={"valid": not errors, "errors": errors},
    )


async def time_zone_convert(params: dict[str, Any]) -> SkillResult:
    value = str(params.get("datetime") or params.get("time") or "")
    from_tz = str(params.get("from_tz") or "UTC")
    to_tz = str(params.get("to_tz") or "UTC")
    if not value:
        return SkillResult(skill_id="time_zone_convert", ok=False, error="datetime is required")
    try:
        source = datetime.fromisoformat(value)
        if source.tzinfo is None:
            source = source.replace(tzinfo=ZoneInfo(from_tz))
        converted = source.astimezone(ZoneInfo(to_tz))
    except Exception as e:
        return SkillResult(skill_id="time_zone_convert", ok=False, error=str(e))
    return SkillResult(
        skill_id="time_zone_convert",
        ok=True,
        output={"datetime": converted.isoformat(), "timezone": to_tz},
    )


def _run_command_skill(skill_id: str, base_cmd: list[str], params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or ".").strip()
    timeout = max(1, min(120, int(params.get("timeout_sec") or 30)))
    try:
        target = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id=skill_id, ok=False, error=str(e))
    cmd = [*base_cmd, str(target)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return SkillResult(skill_id=skill_id, ok=False, error=str(e))
    return SkillResult(
        skill_id=skill_id,
        ok=proc.returncode == 0,
        output={"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode},
        duration_sec=time.perf_counter() - started,
    )


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text))


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_readonly_sql(sql: str) -> bool:
    stripped = sql.strip().lower()
    return (stripped.startswith("select") or stripped.startswith("with")) and not re.search(
        r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b",
        stripped,
    )


def _as_float(value: str | None) -> float | None:
    try:
        return float(value or "")
    except ValueError:
        return None


def _write_minimal_docx(path: Path, markdown: str) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{html.escape(line)}</w:t></w:r></w:p>" for line in markdown.splitlines()
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", f"{_DOCX_PREFIX}{body}{_DOCX_SUFFIX}")


def _write_minimal_pdf(path: Path, markdown: str) -> None:
    safe = markdown.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe[:1200]}) Tj ET"
    pdf = (
        "%PDF-1.4\n"
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        f"5 0 obj << /Length {len(stream)} >> stream\n{stream}\nendstream endobj\n"
        "xref\n0 6\n0000000000 65535 f \n"
        "trailer << /Root 1 0 R /Size 6 >>\nstartxref\n0\n%%EOF\n"
    )
    path.write_bytes(pdf.encode("utf-8"))


def _explain_regex(pattern: str) -> str:
    pieces: list[str] = []
    if "^" in pattern:
        pieces.append("matches from the start")
    if "$" in pattern:
        pieces.append("matches until the end")
    if "\\d" in pattern:
        pieces.append("contains digits")
    if "\\w" in pattern:
        pieces.append("contains word characters")
    if ".*" in pattern:
        pieces.append("allows any characters")
    return "; ".join(pieces) or "valid regular expression"


def _validate_basic_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = schema.get("required")
    if isinstance(value, dict) and isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in value:
                errors.append(f"missing required key: {key}")
    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        for key, rule in properties.items():
            if key not in value or not isinstance(rule, dict):
                continue
            expected = rule.get("type")
            if isinstance(expected, str) and not _type_matches(value[key], expected):
                errors.append(f"{key} expected {expected}")
    return errors


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_DOCX_PREFIX = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>"""
_DOCX_SUFFIX = "</w:body></w:document>"
