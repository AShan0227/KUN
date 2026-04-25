"""file-io skill — read/write/list files under a sandboxed root.

Params:
  op: "read" | "write" | "list" | "delete" (required)
  path: str (required) — relative to KUN_SKILL_FILE_ROOT
  content: str (required for "write")
  encoding: str (default "utf-8")

Returns:
  - read: {content, size}
  - write: {bytes_written}
  - list: [{name, is_dir, size}]
  - delete: {deleted}

Safety: every path is resolved relative to ``KUN_SKILL_FILE_ROOT`` (default
``/tmp/kun-skills/``). Path traversal (../..) is rejected. The root is
created on first call.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from kun.skills.dispatcher import SkillResult, register

_DEFAULT_ROOT = "/tmp/kun-skills"  # noqa: S108 — explicit sandbox root, mode 700
_MAX_READ = 5 * 1024 * 1024  # 5 MiB


def _root() -> Path:
    root = Path(os.getenv("KUN_SKILL_FILE_ROOT") or _DEFAULT_ROOT)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root.resolve()


def _resolve(rel: str) -> Path:
    """Resolve ``rel`` under the sandbox root, refusing escapes."""
    root = _root()
    candidate = (root / rel).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(f"path escapes sandbox: {rel}")
    return candidate


async def execute(params: dict[str, Any]) -> SkillResult:
    started = time.perf_counter()
    op = str(params.get("op") or "").strip().lower()
    rel = str(params.get("path") or "").strip()
    encoding = str(params.get("encoding") or "utf-8")

    if op not in {"read", "write", "list", "delete"}:
        return SkillResult(skill_id="file-io", ok=False, error="invalid op")
    if not rel and op != "list":
        return SkillResult(skill_id="file-io", ok=False, error="path is required")

    try:
        target = _resolve(rel or ".")
    except ValueError as e:
        return SkillResult(skill_id="file-io", ok=False, error=str(e))

    if op == "read":
        if not target.is_file():
            return SkillResult(skill_id="file-io", ok=False, error="not a file")
        size = target.stat().st_size
        if size > _MAX_READ:
            return SkillResult(
                skill_id="file-io",
                ok=False,
                error=f"file too large to read inline ({size} bytes); use offload",
            )
        content = target.read_text(encoding=encoding)
        return SkillResult(
            skill_id="file-io",
            ok=True,
            output={"content": content, "size": size},
            duration_sec=time.perf_counter() - started,
        )

    if op == "write":
        content = str(params.get("content") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return SkillResult(
            skill_id="file-io",
            ok=True,
            output={"bytes_written": len(content.encode(encoding))},
            duration_sec=time.perf_counter() - started,
        )

    if op == "list":
        if not target.exists() or not target.is_dir():
            return SkillResult(skill_id="file-io", ok=False, error="not a directory")
        entries = []
        for item in sorted(target.iterdir()):
            entries.append(
                {
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                }
            )
        return SkillResult(
            skill_id="file-io",
            ok=True,
            output=entries,
            duration_sec=time.perf_counter() - started,
        )

    # delete
    if not target.exists():
        return SkillResult(
            skill_id="file-io",
            ok=True,
            output={"deleted": False},
            duration_sec=time.perf_counter() - started,
        )
    if target.is_dir():
        return SkillResult(skill_id="file-io", ok=False, error="refusing to rmdir")
    target.unlink()
    return SkillResult(
        skill_id="file-io",
        ok=True,
        output={"deleted": True},
        duration_sec=time.perf_counter() - started,
    )


register("file-io", execute)
