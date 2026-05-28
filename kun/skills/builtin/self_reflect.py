"""self-reflect skill — KUN self-introspection (read/write under tight whitelist).

Designed for tasks where KUN needs to read its own dev_logs / seeds / source
and produce distillation artifacts back into the repo. dogfood v6 exposed
the gap: ``file-io`` is sandboxed to ``/tmp/kun-skills`` for user-data
processing, which can't see the KUN repo. Instead of widening file-io
(risky — its delete op could touch repo files), we introduce a dedicated
skill that:

  - reads from an explicit whitelist of repo subdirs (no escape via `..`)
  - writes ONLY to a single output dir (created on first call)
  - has NO delete op (preserves invariants like "existing dev_logs are read-only")
  - supports ``offset`` + ``limit`` on read (Claude Code's Read tool pattern —
    avoid blowing context on large files)

Params:
  op: "read" | "list" | "write" (required) — no "delete"
  path: str (required) — relative to KUN repo root (resolved via KUN_REPO_ROOT
        env var or auto-detected by walking up from this file)
  content: str (required for "write")
  offset: int (optional, "read" only) — start line, 1-based
  limit: int (optional, "read" only) — number of lines to read

Returns:
  - read: {content, line_count, truncated}
  - list: [{name, is_dir, size}]
  - write: {bytes_written, full_path}

Whitelist:
  READ_DIRS:  docs/, seeds/, kun/, tests/, scripts/, alembic/
  WRITE_DIR:  docs/dist-output/ (the ONLY writable destination)

Any path that resolves outside these (or contains "..") is refused with
``path not in whitelist``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from kun.core.logging import get_logger
from kun.skills.dispatcher import SkillResult, register

log = get_logger("kun.skills.self_reflect")

# Read whitelist (relative to repo root). All these dirs are read-only.
_READ_DIRS: tuple[str, ...] = (
    "docs",
    "seeds",
    "kun",
    "tests",
    "scripts",
    "alembic",
)

# Write whitelist — ONLY this directory accepts writes. Auto-created.
_WRITE_DIR = "docs/dist-output"

# Max bytes to read in one call (prevent context blow-up on huge files).
_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MiB

# Max bytes per write (safety cap).
_MAX_WRITE_BYTES = 1 * 1024 * 1024  # 1 MiB


def _repo_root() -> Path:
    """Find the KUN repo root.

    Resolution priority:
      1. ``KUN_REPO_ROOT`` env var (if set)
      2. Walk up from this file (../../.. = kun → repo root)

    Cached per process call via Path resolution (cheap).
    """
    env = os.getenv("KUN_REPO_ROOT")
    if env:
        return Path(env).resolve()
    # this file: kun/skills/builtin/self_reflect.py → up 3 → repo root
    return Path(__file__).resolve().parents[3]


def _resolve_read(rel: str) -> Path:
    """Resolve ``rel`` under repo root, refuse if not under a whitelisted read dir.

    Raises ``ValueError`` with explicit message if outside whitelist.
    """
    if ".." in Path(rel).parts:
        raise ValueError(f"path traversal not allowed: {rel}")
    root = _repo_root()
    candidate = (root / rel).resolve()
    # Must be inside repo root
    try:
        rel_from_root = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes repo root: {rel}") from exc
    # Must be under a whitelisted top-level dir (or be one itself)
    top = rel_from_root.parts[0] if rel_from_root.parts else ""
    if top not in _READ_DIRS:
        raise ValueError(
            f"path not in read whitelist (allowed: {_READ_DIRS}): {rel}"
        )
    return candidate


def _resolve_write(rel: str) -> Path:
    """Resolve ``rel`` under ``docs/dist-output/``, refuse anything else.

    Creates the output dir if it doesn't exist.
    """
    if ".." in Path(rel).parts:
        raise ValueError(f"path traversal not allowed: {rel}")
    root = _repo_root()
    out_root = (root / _WRITE_DIR).resolve()
    out_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    # If rel starts with _WRITE_DIR, strip the prefix so caller can use either
    # "docs/dist-output/foo.md" or just "foo.md".
    rel_p = Path(rel)
    if rel_p.parts and rel_p.parts[0] == "docs" and len(rel_p.parts) > 1 and rel_p.parts[1] == "dist-output":
        rel = str(Path(*rel_p.parts[2:]))
    candidate = (out_root / rel).resolve()
    try:
        candidate.relative_to(out_root)
    except ValueError as exc:
        raise ValueError(
            f"writes only allowed under {_WRITE_DIR}/: {rel}"
        ) from exc
    return candidate


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    op = str(params.get("op") or "").strip().lower()
    rel = str(params.get("path") or "").strip()
    encoding = str(params.get("encoding") or "utf-8")

    if op not in {"read", "list", "write"}:
        return SkillResult(
            skill_id="self-reflect",
            ok=False,
            error=f"invalid op: {op!r} (allowed: read/list/write)",
        )
    if not rel and op != "list":
        return SkillResult(skill_id="self-reflect", ok=False, error="path is required")

    try:
        target = _resolve_write(rel) if op == "write" else _resolve_read(rel or ".")
    except ValueError as e:
        return SkillResult(skill_id="self-reflect", ok=False, error=str(e))

    if op == "read":
        if not target.is_file():
            return SkillResult(
                skill_id="self-reflect", ok=False, error=f"not a file: {rel}"
            )
        size = target.stat().st_size
        # Offset/limit semantics (Claude Code Read pattern)
        offset_raw = params.get("offset")
        limit_raw = params.get("limit")
        offset = int(offset_raw) if isinstance(offset_raw, (int, str)) and str(offset_raw).isdigit() else 0
        limit = int(limit_raw) if isinstance(limit_raw, (int, str)) and str(limit_raw).isdigit() else 0
        if size > _MAX_READ_BYTES and limit == 0:
            return SkillResult(
                skill_id="self-reflect",
                ok=False,
                error=(
                    f"file too large to read inline ({size} bytes); "
                    f"use offset+limit (1-based line range) to chunk"
                ),
            )
        try:
            full = target.read_text(encoding=encoding)
        except UnicodeDecodeError as e:
            return SkillResult(
                skill_id="self-reflect",
                ok=False,
                error=f"decode error ({encoding}): {e}",
            )
        lines = full.splitlines(keepends=True)
        truncated = False
        if limit > 0:
            start = max(0, offset - 1) if offset > 0 else 0
            end = start + limit
            truncated = end < len(lines)
            content = "".join(lines[start:end])
        else:
            content = full
        log.info(
            "self_reflect.read",
            path=rel,
            bytes=len(content.encode(encoding)),
            line_count=len(lines),
            offset=offset,
            limit=limit,
        )
        return SkillResult(
            skill_id="self-reflect",
            ok=True,
            output={
                "content": content,
                "line_count": len(lines),
                "truncated": truncated,
            },
            duration_sec=time.perf_counter() - started,
        )

    if op == "list":
        if not target.exists():
            return SkillResult(
                skill_id="self-reflect", ok=False, error=f"path not found: {rel}"
            )
        if not target.is_dir():
            return SkillResult(
                skill_id="self-reflect", ok=False, error=f"not a directory: {rel}"
            )
        entries = []
        for entry in sorted(target.iterdir()):
            try:
                stat = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": stat.st_size if entry.is_file() else 0,
                    }
                )
            except OSError:
                continue  # skip unreadable entries (permission etc.)
        log.info("self_reflect.list", path=rel, count=len(entries))
        return SkillResult(
            skill_id="self-reflect",
            ok=True,
            output=entries,
            duration_sec=time.perf_counter() - started,
        )

    # write
    content = params.get("content")
    if not isinstance(content, str):
        return SkillResult(
            skill_id="self-reflect",
            ok=False,
            error="content must be a string for write op",
        )
    body = content.encode(encoding)
    if len(body) > _MAX_WRITE_BYTES:
        return SkillResult(
            skill_id="self-reflect",
            ok=False,
            error=f"content too large ({len(body)} bytes; max {_MAX_WRITE_BYTES})",
        )
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target.write_bytes(body)
    log.info(
        "self_reflect.write",
        path=str(target),
        bytes_written=len(body),
    )
    return SkillResult(
        skill_id="self-reflect",
        ok=True,
        output={"bytes_written": len(body), "full_path": str(target)},
        duration_sec=time.perf_counter() - started,
    )


register("self-reflect", execute)
