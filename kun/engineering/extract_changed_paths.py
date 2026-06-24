"""V7 Phase X.O — extract changed file paths from executor messages.

Used by the production WS entry (kun/engineering/orchestrator.py) to
feed real ``actual_production_entry_changes`` into LongTaskOrchestrator.
run_long_task so X.I-3-FIX's ProductionEntryDiffChecker actually sees
a non-empty actual list — not just always 'no_declaration'.

The X.O self-audit (using docs/templates/hidden-orphan-audit-prompt.md)
found that commit 510ebe1 wired the checker into the class but did NOT
wire the data-flow argument at the production entry. This module is the
data-source.

Strategy:
  - Walk the ExecutorLoop's final_messages
  - For every tool_call whose name suggests a file write (self_reflect.write,
    file_io.write, edit_file, etc.), extract the ``path`` argument
  - Resolve to a repo-relative path so comparison against
    TaskSpec.production_entry_changes_required is direct
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Tools known to mutate files; if a future skill adds another writer, it
# must be added here.
_KNOWN_WRITE_TOOL_NAMES = frozenset(
    {
        "self_reflect.write",
        "self-reflect.write",
        "file_io.write",
        "file-io.write",
        "edit_file",
        "Edit",
        "Write",
    }
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _to_repo_relative(path_str: str) -> str:
    if not path_str:
        return ""
    p = Path(path_str)
    root = _repo_root()
    try:
        if p.is_absolute():
            return str(p.relative_to(root))
    except ValueError:
        # path is absolute but not under repo root — return as-is
        return str(p)
    # path is relative — normalize via repo root
    candidate = (root / p).resolve()
    try:
        return str(candidate.relative_to(root))
    except ValueError:
        return str(p)


def extract_changed_paths_from_messages(
    final_messages: list[dict[str, Any]],
) -> list[str]:
    """Walk the executor's final messages for tool_calls that wrote files.

    Returns a deduplicated list of repo-relative paths in stable order.
    """
    seen: set[str] = set()
    out: list[str] = []

    for msg in final_messages or []:
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            name = (tc.get("name") or tc.get("function", {}).get("name") or "")
            if name not in _KNOWN_WRITE_TOOL_NAMES:
                continue
            args = tc.get("arguments") or tc.get("function", {}).get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                continue
            path = (
                args.get("path")
                or args.get("file_path")
                or args.get("filepath")
                or ""
            )
            if not path or not isinstance(path, str):
                continue
            rel = _to_repo_relative(path)
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


__all__ = ["extract_changed_paths_from_messages"]
