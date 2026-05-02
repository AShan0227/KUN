"""code-propose-change builtin skill backed by CodeCapability workflow.

This skill gives the agent loop a real programming path without pretending it
is a fully autonomous engineer.  It defaults to dry-run, reviews the proposed
change first, and only allows real writes when an operator explicitly enables
``KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
from time import perf_counter
from typing import Any

from kun.skills.code_capability import CodeCapability
from kun.skills.code_capability.workflow import ChangeCheckSpec, ChangeWorkflowResult
from kun.skills.dispatcher import SkillResult, register

_MAX_PATCH_CHARS = 512_000
_MAX_CHECKS = 3


async def execute(params: dict[str, Any]) -> SkillResult:
    start = perf_counter()
    path = str(params.get("path") or "").strip()
    if not path:
        return _result(ok=False, error="path is required", start=start)

    patch_text = params.get("patch_text")
    replacement_content = params.get("replacement_content")
    has_patch = isinstance(patch_text, str) and patch_text.strip() != ""
    has_replacement = isinstance(replacement_content, str)
    if has_patch == has_replacement:
        return _result(
            ok=False,
            error="provide exactly one of patch_text or replacement_content",
            start=start,
        )
    if has_patch and len(str(patch_text)) > _MAX_PATCH_CHARS:
        return _result(
            ok=False,
            error=f"patch_text too large: max {_MAX_PATCH_CHARS} chars",
            start=start,
        )
    if has_replacement and len(str(replacement_content)) > _MAX_PATCH_CHARS:
        return _result(
            ok=False,
            error=f"replacement_content too large: max {_MAX_PATCH_CHARS} chars",
            start=start,
        )

    requested_apply = bool(params.get("allow_apply") is True)
    allow_apply = requested_apply and _apply_enabled()
    if requested_apply and not allow_apply:
        return _result(
            ok=False,
            error=(
                "code-propose-change skill refuses real writes unless "
                "KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY=1"
            ),
            start=start,
            metadata={
                "review_only": True,
                "production_action": False,
                "file_written": False,
                "code_executed": False,
                "apply_requested": True,
                "apply_allowed": False,
            },
        )

    try:
        checks = _parse_checks(params.get("checks"))
    except ValueError as exc:
        return _result(ok=False, error=str(exc), start=start)

    capability = CodeCapability(workspace_root=_workspace_root(params))
    result = await capability.workflow.propose_change(
        path,
        patch_text=str(patch_text) if has_patch else None,
        replacement_content=str(replacement_content) if has_replacement else None,
        allow_apply=allow_apply,
        checks=tuple(checks) if checks else None,
    )
    payload = _change_payload(result)
    return _result(
        ok=result.ok,
        output={
            **payload,
            "review_only": not allow_apply,
            "production_action": False,
            "message": (
                "已完成受控代码改动 dry-run；没有写真实工作区。"
                if not allow_apply
                else "已按显式开关写入真实工作区；请继续跑仓库级验证。"
            ),
        },
        error=result.error or None if result.ok else result.error or "code change failed",
        start=start,
        metadata={
            "review_only": not allow_apply,
            "production_action": False,
            "file_written": bool(result.applied and not result.rolled_back),
            "code_executed": bool(checks),
            "apply_requested": requested_apply,
            "apply_allowed": allow_apply,
            "mode": result.mode,
            "phase": result.phase,
        },
    )


def _workspace_root(params: dict[str, Any]) -> Path:
    raw = str(
        params.get("workspace_root") or os.getenv("KUN_CODE_CAPABILITY_WORKSPACE_ROOT") or "."
    )
    return Path(raw).expanduser().resolve()


def _apply_enabled() -> bool:
    return os.getenv("KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY", "0") == "1"


def _parse_checks(raw: Any) -> list[ChangeCheckSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("checks must be a list")
    if len(raw) > _MAX_CHECKS:
        raise ValueError(f"checks too many: max {_MAX_CHECKS}")
    out: list[ChangeCheckSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each check must be an object")
        kind = str(item.get("kind") or "lint")
        if kind not in {"lint", "test"}:
            raise ValueError("check.kind must be lint or test")
        tool = str(item.get("tool") or "ruff")
        if tool not in {"ruff", "black", "mypy"}:
            raise ValueError("check.tool must be ruff, black, or mypy")
        out.append(
            ChangeCheckSpec(
                kind=kind,  # type: ignore[arg-type]
                target=str(item.get("target")) if item.get("target") is not None else None,
                tool=tool,  # type: ignore[arg-type]
                timeout_sec=int(item.get("timeout_sec") or 60),
            )
        )
    return out


def _change_payload(result: ChangeWorkflowResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "path": result.path,
        "mode": result.mode,
        "phase": result.phase,
        "applied": result.applied,
        "rolled_back": result.rolled_back,
        "bytes_changed": result.bytes_changed,
        "diff": result.diff,
        "review": {
            "ok": result.review.ok,
            "findings": [
                {
                    "severity": finding.severity,
                    "message": finding.message,
                    "rule": finding.rule,
                    "path": finding.path,
                    "line": finding.line,
                }
                for finding in result.review.findings
            ],
        }
        if result.review is not None
        else None,
        "lint_results": [
            {
                "ok": lint.ok,
                "tool": lint.tool,
                "issues": len(lint.issues),
                "returncode": lint.returncode,
                "timed_out": lint.timed_out,
            }
            for lint in result.lint_results
        ],
        "test_results": [
            {
                "ok": test.ok,
                "passed": test.passed,
                "failed": test.failed,
                "skipped": test.skipped,
                "returncode": test.returncode,
                "timed_out": test.timed_out,
            }
            for test in result.test_results
        ],
        "debug": {
            "category": result.debug.category,
            "summary": result.debug.summary,
            "fix_hint": result.debug.fix_hint,
            "confidence": result.debug.confidence,
        }
        if result.debug is not None
        else None,
        "error": result.error,
        "rollback_hint": result.rollback_hint,
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
        skill_id="code-propose-change",
        ok=ok,
        output=output,
        error=error,
        duration_sec=perf_counter() - start,
        metadata=metadata
        or {
            "review_only": True,
            "production_action": False,
            "file_written": False,
            "code_executed": False,
        },
    )


register("code-propose-change", execute)

__all__ = ["execute"]
