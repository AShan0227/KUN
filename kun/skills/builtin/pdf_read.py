"""pdf-read skill — extract text from a PDF, page by page.

Params:
  path: str (required) — relative to KUN_SKILL_FILE_ROOT
  pages: str (optional, e.g. "1-3,5") — page range; default = all
  max_chars: int (default 10000) — total cap; output is truncated and a flag set

Returns:
  {text, page_count, char_count, truncated}

Uses pypdf (pure-Python). For OCR scanned PDFs the agent should fall through
to ``python-exec`` with a real OCR pipeline.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kun.skills.builtin.file_io import _resolve
from kun.skills.dispatcher import SkillResult, register


def _parse_pages(spec: str, page_count: int) -> list[int]:
    """Parse a "1-3,5" page spec into 0-based indexes within page_count."""
    if not spec.strip():
        return list(range(page_count))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a)) - 1
            end = min(page_count, int(b))
            for i in range(start, end):
                out.add(i)
        elif part:
            i = int(part) - 1
            if 0 <= i < page_count:
                out.add(i)
    return sorted(out)


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or "").strip()
    pages_spec = str(params.get("pages") or "").strip()
    max_chars = max(100, min(200_000, int(params.get("max_chars") or 10_000)))

    if not rel:
        return SkillResult(skill_id="pdf-read", ok=False, error="path is required")

    try:
        target: Path = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id="pdf-read", ok=False, error=str(e))

    if not target.is_file():  # noqa: ASYNC240 — local file metadata, no real I/O block
        return SkillResult(skill_id="pdf-read", ok=False, error="not a file")

    try:
        from pypdf import PdfReader
    except ImportError:
        return SkillResult(
            skill_id="pdf-read",
            ok=False,
            error="pypdf not installed (run: uv add pypdf)",
        )

    try:
        reader = PdfReader(str(target))
        page_count = len(reader.pages)
        target_indices = _parse_pages(pages_spec, page_count)
        chunks: list[str] = []
        chars = 0
        truncated = False
        for i in target_indices:
            page_text = reader.pages[i].extract_text() or ""
            if chars + len(page_text) > max_chars:
                page_text = page_text[: max_chars - chars]
                chunks.append(page_text)
                chars += len(page_text)
                truncated = True
                break
            chunks.append(page_text)
            chars += len(page_text)
        text = "\n\n".join(chunks)
    except Exception as e:
        return SkillResult(
            skill_id="pdf-read",
            ok=False,
            error=f"pdf parse error: {e}",
            duration_sec=time.perf_counter() - started,
        )

    return SkillResult(
        skill_id="pdf-read",
        ok=True,
        output={
            "text": text,
            "page_count": page_count,
            "char_count": chars,
            "truncated": truncated,
        },
        duration_sec=time.perf_counter() - started,
        metadata={"pages_read": len(target_indices)},
    )


register("pdf-read", execute)
