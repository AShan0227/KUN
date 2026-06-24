"""V7 Phase X.I-4 — bug_root_cause_cases lookup for Trifecta past line.

Before X.I-4 the Trifecta past line was effectively a stub: either no
hook (DISABLED), or a hook that returned hardcoded text. V7 §12.4.2
specifies the past line should query the ``bug_root_cause_cases`` table
(alembic 0008) and surface similar past cases as findings.

This module provides the read-side primitive. The TrifectaCoordinator
caller wires it as the past hook.
"""

from __future__ import annotations

import re
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.integration.bug_root_cause_lookup")


def _normalize_text(text: str) -> set[str]:
    """Lowercase tokens for naive keyword overlap scoring."""
    cleaned = re.sub(r"[^\w一-鿿\s]", " ", text.lower())
    return {tok for tok in cleaned.split() if len(tok) >= 2}


async def lookup_similar_root_cause_cases(
    *,
    tenant_id: str,
    recent_steps: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Query bug_root_cause_cases scored by keyword overlap with recent steps.

    Returns a list of dicts shaped for trifecta past-line findings:
        {
            "finding": "<text>",
            "case_id": "<case_id>",
            "root_cause_kind": "<kind>",
            "score": <0..1>,
        }
    """
    # Build the query keywords from the recent step summaries
    text_blob = " ".join(
        str(s.get("summary") or s.get("step") or "") for s in recent_steps
    )
    if not text_blob.strip():
        return []
    query = _normalize_text(text_blob)
    if not query:
        return []

    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import BugRootCaseRow

        async with session_scope(tenant_id=tenant_id) as s:
            stmt = (
                select(BugRootCaseRow)
                .where(BugRootCaseRow.tenant_id == tenant_id)
                .order_by(BugRootCaseRow.last_hit_at.desc())
                .limit(50)  # take up to 50 then score
            )
            rows = (await s.execute(stmt)).scalars().all()
    except Exception as e:
        log.warning(
            "bug_root_cause_lookup.query_failed",
            tenant_id=tenant_id,
            error=f"{type(e).__name__}: {e}",
        )
        return []

    scored: list[tuple[float, Any]] = []
    for row in rows:
        case_text = " ".join(
            [
                row.trace_signature or "",
                row.error_type or "",
                row.root_cause_kind or "",
                row.fix_pattern or "",
            ]
        )
        case_tokens = _normalize_text(case_text)
        if not case_tokens:
            continue
        overlap = len(query & case_tokens)
        if overlap == 0:
            continue
        score = overlap / max(len(query), 1)
        scored.append((score, row))

    scored.sort(key=lambda t: (t[0], t[1].hit_count), reverse=True)

    findings: list[dict[str, Any]] = []
    for score, row in scored[:limit]:
        findings.append(
            {
                "finding": (
                    f"Past case '{row.error_type}' ({row.root_cause_kind}): "
                    f"{row.fix_pattern[:200]}"
                ),
                "case_id": row.case_id,
                "root_cause_kind": row.root_cause_kind,
                "score": round(float(score), 3),
                "hit_count": int(row.hit_count),
            }
        )
    return findings


def make_bug_root_cause_past_hook(*, tenant_id: str = "default"):
    """Build a TrifectaCoordinator past_hook backed by bug_root_cause_cases.

    Returns an async callable with signature
        (task_id: str, recent_steps: list[dict]) -> tuple[list[dict], float, str | None]
    matching the TrifectaCoordinator PastLineHook contract.
    """

    async def _hook(
        task_id: str, recent_steps: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        try:
            findings = await lookup_similar_root_cause_cases(
                tenant_id=tenant_id,
                recent_steps=recent_steps,
                limit=3,
            )
            return (findings, 0.0, None)  # zero cost — pure DB read
        except Exception as e:
            return (
                [],
                0.0,
                f"bug_root_cause_lookup_error: {type(e).__name__}: {e}",
            )

    return _hook


__all__ = [
    "lookup_similar_root_cause_cases",
    "make_bug_root_cause_past_hook",
]
