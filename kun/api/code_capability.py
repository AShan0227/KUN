"""CodeCapability API.

This is the narrow runtime entrypoint for KUN's code capability layer.  It
intentionally exposes read-only review plus explicit sandbox run/check calls;
write operations stay behind internal orchestration until the full automatic
coding workflow is ready.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from kun.api.runtime import get_code_capability
from kun.core.config import settings
from kun.core.tenancy import current_tenant, require_scope
from kun.skills.code_capability import CodeCapability
from kun.skills.code_capability.executor import LintTool
from kun.skills.code_capability.reviewer import ReviewFinding, ReviewResult

router = APIRouter(prefix="/api/code-capability", tags=["code-capability"])


class ReviewDiffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff: str = Field(min_length=1, max_length=256_000)


class ReviewFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)


class RunPythonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64_000)
    cwd: str | None = Field(default=None, max_length=500)
    timeout_sec: int = Field(default=30, ge=1, le=300)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["lint", "test"] = "lint"
    target: str = Field(min_length=1, max_length=500)
    tool: LintTool = "ruff"
    timeout_sec: int = Field(default=60, ge=1, le=300)


@router.post("/review-diff")
async def review_diff(payload: ReviewDiffRequest, request: Request) -> dict[str, Any]:
    """Run deterministic read-only review over a unified diff."""

    _require_scope_when_enforced("code:read")
    capability = _capability_or_503(request)
    return _review_result_payload(capability.reviewer.review_diff(payload.diff))


@router.post("/review-file")
async def review_file(payload: ReviewFileRequest, request: Request) -> dict[str, Any]:
    """Run deterministic read-only review over one workspace-local file."""

    _require_scope_when_enforced("code:read")
    capability = _capability_or_503(request)
    try:
        result = capability.reviewer.review_file(payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _review_result_payload(result)


@router.post("/run-python")
async def run_python(payload: RunPythonRequest, request: Request) -> dict[str, Any]:
    """Run a Python snippet through CodeExecutor's bounded soft sandbox."""

    _require_scope_when_enforced("code:execute")
    capability = _capability_or_503(request)
    cwd = Path(payload.cwd) if payload.cwd else None
    try:
        result = await capability.executor.execute_python(
            payload.code,
            cwd=cwd,
            timeout_sec=payload.timeout_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": result.ok,
        "command": result.command,
        "cwd": result.cwd,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "duration_sec": result.duration_sec,
        "truncated": result.truncated,
        "sandbox": result.sandbox,
    }


@router.post("/check")
async def check(payload: CheckRequest, request: Request) -> dict[str, Any]:
    """Run a workspace-local lint or pytest check through CodeExecutor."""

    _require_scope_when_enforced("code:execute")
    capability = _capability_or_503(request)
    try:
        if payload.kind == "test":
            test = await capability.executor.execute_test(
                payload.target,
                timeout_sec=payload.timeout_sec,
            )
            return {
                "kind": "test",
                "ok": test.ok,
                "passed": test.passed,
                "failed": test.failed,
                "skipped": test.skipped,
                "output": test.output,
                "error": test.error,
                "returncode": test.returncode,
                "duration_sec": test.duration_sec,
                "timed_out": test.timed_out,
            }
        lint = await capability.executor.execute_lint(
            Path(payload.target),
            tool=payload.tool,
            timeout_sec=payload.timeout_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "kind": "lint",
        "ok": lint.ok,
        "tool": lint.tool,
        "issues": [
            {
                "path": issue.path,
                "line": issue.line,
                "column": issue.column,
                "message": issue.message,
            }
            for issue in lint.issues
        ],
        "output": lint.output,
        "error": lint.error,
        "returncode": lint.returncode,
        "duration_sec": lint.duration_sec,
        "timed_out": lint.timed_out,
    }


def _capability_or_503(request: Request) -> CodeCapability:
    try:
        return get_code_capability(request.app)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _review_result_payload(result: ReviewResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "findings": [_finding_payload(finding) for finding in result.findings],
    }


def _finding_payload(finding: ReviewFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity,
        "message": finding.message,
        "rule": finding.rule,
        "path": finding.path,
        "line": finding.line,
    }


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


__all__ = [
    "CheckRequest",
    "ReviewDiffRequest",
    "ReviewFileRequest",
    "RunPythonRequest",
    "router",
]
