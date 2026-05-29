"""V7 Phase X.I — Production-path traceability primitive.

Shared core used by:
  - X.I-1 Auditor Angle 8 (does this capability reach a production entry?)
  - X.I-2 Gate reachability check (block REPLAY without production caller)
  - X.I-3 MD entry-changes-required field validation
  - X.I-4 Trifecta past line bug_root_cause_cases lookup

The X.H self-audit found "feature wired to class but not production
entry" was a recurring failure mode (3 consecutive releases). The fix
at X.H was a single-file AST audit. X.I generalizes that into a
primitive any auditor / gate / planner can call.

Production entries are listed in ``docs/PRODUCTION_ENTRIES.md``. This
module parses production code and returns whether a symbol (class,
function, attribute name) is reachable from at least one entry.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from kun.core.logging import get_logger

log = get_logger("kun.governance.production_path_traceability")


# Single source of truth for production entries. Future entries are
# added to docs/PRODUCTION_ENTRIES.md AND this list.
_DEFAULT_PRODUCTION_ENTRIES: tuple[str, ...] = (
    "kun/engineering/orchestrator.py",
    "kun/control_plane/daemon.py",
    "kun/engineering/idle_batch.py",  # idle_batch_worker called from kun/api/main.py
    "kun/api/main.py",
    "kun/api/ws.py",
    "kun/api/chat.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class ReachabilityResult:
    """One symbol's reachability report (V7 §13.6 frozen IO)."""

    symbol: str
    reachable: bool
    entries_hit: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def is_orphan(self) -> bool:
        return not self.reachable


def _entry_paths(extra_entries: list[str] | None = None) -> list[Path]:
    root = _repo_root()
    bases = list(_DEFAULT_PRODUCTION_ENTRIES) + list(extra_entries or [])
    return [root / b for b in bases if (root / b).exists()]


def check_symbol_reachable(
    symbol: str,
    *,
    extra_entries: list[str] | None = None,
) -> ReachabilityResult:
    """Return whether ``symbol`` (a class / function / attribute name) is
    actually instantiated / called / referenced by any registered
    production entry.

    Detection strategy (intentionally simple, deterministic):
      - Read each production entry file
      - Parse to AST
      - Walk for ``ast.Call`` / ``ast.Name`` / ``ast.Attribute`` matching
        ``symbol`` (top-level name OR last-component of dotted attribute)
      - Comments / docstrings / strings are excluded (AST already drops them)
    """
    if not symbol or not symbol.strip():
        return ReachabilityResult(
            symbol=symbol,
            reachable=False,
            rationale="empty symbol",
        )

    hits: list[str] = []
    rationale_parts: list[str] = []
    paths = _entry_paths(extra_entries)
    if not paths:
        return ReachabilityResult(
            symbol=symbol,
            reachable=False,
            rationale="no production entries on disk",
        )

    for path in paths:
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception as e:
            log.warning(
                "production_path_traceability.parse_failed",
                file=str(path),
                error=f"{type(e).__name__}: {e}",
            )
            continue

        if _ast_references_symbol(tree, symbol):
            hits.append(str(path.relative_to(_repo_root())))

    if hits:
        rationale_parts.append(
            f"symbol {symbol!r} found in {len(hits)} production entry file(s)"
        )

    return ReachabilityResult(
        symbol=symbol,
        reachable=bool(hits),
        entries_hit=hits,
        rationale="; ".join(rationale_parts)
        or f"symbol {symbol!r} not found at any of {len(paths)} entries",
    )


def _ast_references_symbol(tree: ast.AST, symbol: str) -> bool:
    """True iff the AST walks a Call / Name / Attribute referring to symbol."""
    target = symbol.strip()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == target:
            return True
        if isinstance(node, ast.Attribute) and node.attr == target:
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == target:
                return True
            if isinstance(func, ast.Attribute) and func.attr == target:
                return True
    return False


# ============================================================
# Lightweight grep fallback (used when AST parsing fails or for
# non-Python production entries — future REST / cron config files)
# ============================================================


def grep_symbol_in_entries(
    symbol: str,
    *,
    extra_entries: list[str] | None = None,
) -> list[str]:
    """Find production entries where ``symbol`` appears as a word boundary
    text match. Use as a sanity check alongside AST: AST may miss when
    a future entry isn't Python or the symbol is dynamically resolved."""
    word = re.compile(rf"\b{re.escape(symbol)}\b")
    hits: list[str] = []
    for path in _entry_paths(extra_entries):
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:  # noqa: S112 — best-effort grep, log not needed
            continue
        if word.search(src):
            hits.append(str(path.relative_to(_repo_root())))
    return hits


__all__ = [
    "ReachabilityResult",
    "check_symbol_reachable",
    "grep_symbol_in_entries",
]
