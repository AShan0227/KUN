"""code-review builtin skill backed by CodeCapability's deterministic reviewer."""

from __future__ import annotations

import os
from pathlib import Path
from time import perf_counter
from typing import Any

from kun.skills.code_capability.reviewer import CodeReviewer, ReviewFinding
from kun.skills.dispatcher import SkillResult, register

_MAX_DIFF_CHARS = 256_000


async def execute(params: dict[str, Any]) -> SkillResult:
    """Review a diff or workspace-local file without writing or executing code."""

    start = perf_counter()
    diff = str(params.get("diff") or "")
    path = str(params.get("path") or "")
    if bool(diff.strip()) == bool(path.strip()):
        return _result(
            ok=False,
            error="provide exactly one of diff or path",
            start=start,
        )
    if diff and len(diff) > _MAX_DIFF_CHARS:
        return _result(
            ok=False,
            error=f"diff too large: max {_MAX_DIFF_CHARS} chars",
            start=start,
        )

    workspace_root = _workspace_root(params)
    reviewer = CodeReviewer(workspace_root=workspace_root)
    try:
        review = reviewer.review_diff(diff) if diff else reviewer.review_file(path)
    except ValueError as exc:
        return _result(ok=False, error=str(exc), start=start)

    return _result(
        ok=review.ok,
        output={
            "ok": review.ok,
            "findings": [_finding_payload(item) for item in review.findings],
            "review_only": True,
            "production_action": False,
            "file_written": False,
            "code_executed": False,
        },
        start=start,
        metadata={
            "review_only": True,
            "production_action": False,
            "workspace_root": str(workspace_root),
            "input_kind": "diff" if diff else "path",
        },
    )


def _workspace_root(params: dict[str, Any]) -> Path:
    raw = str(
        params.get("workspace_root") or os.getenv("KUN_CODE_CAPABILITY_WORKSPACE_ROOT") or "."
    )
    return Path(raw).expanduser().resolve()


def _finding_payload(finding: ReviewFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "message": finding.message,
        "rule": finding.rule,
        "path": finding.path,
        "line": finding.line,
    }


def _result(
    *,
    ok: bool,
    start: float,
    output: Any = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SkillResult:
    return SkillResult(
        skill_id="code-review",
        ok=ok,
        output=output,
        error=error,
        duration_sec=perf_counter() - start,
        metadata=metadata or {"review_only": True, "production_action": False},
    )


register("code-review", execute)

__all__ = ["execute"]
