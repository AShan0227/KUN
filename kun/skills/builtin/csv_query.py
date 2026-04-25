"""csv-query skill — load a CSV into an in-memory table and run SQL on it.

Backed by Python's stdlib + a tiny SQL evaluator (sqlite :memory:). No new
dependencies needed — keeps the stack light. For large CSVs (> 50 MiB) the
agent should switch to ``python-exec`` with pandas/duckdb directly.

Params:
  path: str (required) — relative to KUN_SKILL_FILE_ROOT
  sql: str (required) — query against table name "data"
  max_rows: int (default 200, max 5000)

Returns:
  {columns, rows}
"""

from __future__ import annotations

import csv
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from kun.skills.builtin.file_io import _resolve
from kun.skills.dispatcher import SkillResult, register

_MAX_ROWS = 5000
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MiB


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    rel = str(params.get("path") or "").strip()
    sql = str(params.get("sql") or "").strip()
    max_rows = max(1, min(_MAX_ROWS, int(params.get("max_rows") or 200)))

    if not rel or not sql:
        return SkillResult(skill_id="csv-query", ok=False, error="path and sql are required")

    try:
        target: Path = _resolve(rel)
    except ValueError as e:
        return SkillResult(skill_id="csv-query", ok=False, error=str(e))

    if not target.is_file():  # noqa: ASYNC240 — local file metadata, no real I/O block
        return SkillResult(skill_id="csv-query", ok=False, error="not a file")
    if target.stat().st_size > _MAX_FILE_SIZE:  # noqa: ASYNC240
        return SkillResult(skill_id="csv-query", ok=False, error="csv too large; use python-exec")

    # Load CSV into in-memory sqlite
    conn = sqlite3.connect(":memory:")
    try:
        with target.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return SkillResult(skill_id="csv-query", ok=False, error="empty csv")
            # Column names come from the CSV header (untrusted user file).
            # Sanitize aggressively — allow [A-Za-z0-9_] only and quote with
            # SQLite double quotes; this prevents an attacker-controlled
            # header from injecting DDL via the CREATE TABLE statement.
            safe_re = re.compile(r"[^A-Za-z0-9_]+")
            cols = [safe_re.sub("_", (c.strip() or f"col{i}"))[:64] for i, c in enumerate(header)]
            cols_q = ", ".join(f'"{c}"' for c in cols)
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(f"CREATE TABLE data ({cols_q})")
            conn.executemany(f"INSERT INTO data VALUES ({placeholders})", reader)  # noqa: S608
        conn.commit()

        cursor = conn.execute(sql)
        out_cols = [c[0] for c in (cursor.description or [])]
        rows = cursor.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
    except sqlite3.Error as e:
        return SkillResult(
            skill_id="csv-query",
            ok=False,
            error=f"sql error: {e}",
            duration_sec=time.perf_counter() - started,
        )
    finally:
        conn.close()

    return SkillResult(
        skill_id="csv-query",
        ok=True,
        output={"columns": out_cols, "rows": [list(r) for r in rows]},
        duration_sec=time.perf_counter() - started,
        metadata={"row_count": len(rows), "truncated": truncated},
    )


register("csv-query", execute)
