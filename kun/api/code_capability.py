"""CodeCapability API.

This is the narrow runtime entrypoint for KUN's code capability layer. It
exposes read-only review, explicit sandbox run/check calls, and a guarded
single-file change workflow that defaults to dry-run.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kun.api.runtime import get_code_capability
from kun.context.storage import get_store
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.tenancy import current_tenant, require_scope
from kun.datamodel.events import Event
from kun.engineering.credit_assignment import (
    CodeChangeCreditInput,
    persist_code_change_credit_report,
)
from kun.qi.problem_queue import QiProblemSignal, persist_problem_signals
from kun.skills.code_capability import CodeCapability
from kun.skills.code_capability.executor import LintTool
from kun.skills.code_capability.reviewer import ReviewFinding, ReviewResult
from kun.skills.code_capability.skill_draft import build_code_change_skill_draft_asset
from kun.skills.code_capability.strategy_search import (
    CodeStrategySearchInput,
    code_strategy_tree_search_enabled_from_env,
    configured_code_strategy_search_budget_from_env,
    run_code_change_strategy_tree_search,
)
from kun.skills.code_capability.workflow import ChangeCheckSpec, ChangeWorkflowResult

router = APIRouter(prefix="/api/code-capability", tags=["code-capability"])
log = logging.getLogger(__name__)


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


class ChangeCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["lint", "test"] = "lint"
    target: str | None = Field(default=None, min_length=1, max_length=500)
    tool: LintTool = "ruff"
    timeout_sec: int = Field(default=60, ge=1, le=300)


class ProposeChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str = Field(default="", max_length=500)
    patch_text: str | None = Field(default=None, max_length=512_000)
    replacement_content: str | None = Field(default=None, max_length=512_000)
    allow_apply: bool = False
    checks: list[ChangeCheckRequest] | None = None

    @model_validator(mode="after")
    def _require_one_change_source(self) -> ProposeChangeRequest:
        has_patch = self.patch_text is not None and self.patch_text.strip() != ""
        has_replacement = self.replacement_content is not None
        if has_patch == has_replacement:
            raise ValueError("provide exactly one of patch_text or replacement_content")
        return self


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


@router.post("/propose-change")
async def propose_change(payload: ProposeChangeRequest, request: Request) -> dict[str, Any]:
    """Review, dry-run/apply, and check a single workspace-local code change."""

    _require_scope_when_enforced("code:execute")
    capability = _capability_or_503(request)
    checks = None
    if payload.checks is not None:
        checks = tuple(
            ChangeCheckSpec(
                kind=check.kind,
                target=check.target,
                tool=check.tool,
                timeout_sec=check.timeout_sec,
            )
            for check in payload.checks
        )
    result = await capability.workflow.propose_change(
        payload.path,
        patch_text=payload.patch_text,
        replacement_content=payload.replacement_content,
        allow_apply=payload.allow_apply,
        checks=checks,
    )
    body = _change_result_payload(result)
    await _record_change_observability(payload, request, result)
    if result.phase in {"input", "resolve"}:
        raise HTTPException(status_code=400, detail=body)
    return body


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


def _change_result_payload(result: ChangeWorkflowResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "path": result.path,
        "mode": result.mode,
        "phase": result.phase,
        "applied": result.applied,
        "rolled_back": result.rolled_back,
        "bytes_changed": result.bytes_changed,
        "diff": result.diff,
        "review": _review_result_payload(result.review) if result.review is not None else None,
        "write": _write_result_payload(result.write_result)
        if result.write_result is not None
        else None,
        "lint_results": [_lint_result_payload(lint) for lint in result.lint_results],
        "test_results": [_test_result_payload(test) for test in result.test_results],
        "debug": _debug_payload(result.debug) if result.debug is not None else None,
        "error": result.error,
        "rollback_hint": result.rollback_hint,
    }


async def _record_change_observability(
    payload: ProposeChangeRequest,
    request: Request,
    result: ChangeWorkflowResult,
) -> None:
    """Best-effort event + StateLedger write for CodeCapability outcomes."""

    try:
        tenant = current_tenant()
    except Exception:
        log.debug("code_capability.observability_missing_tenant", exc_info=True)
        return

    task_id = payload.task_id
    reason = payload.reason.strip()
    checks_passed = _change_checks_passed(result)
    diff_sha256 = hashlib.sha256(result.diff.encode("utf-8")).hexdigest() if result.diff else ""
    skill_draft_asset_id = await _store_code_skill_draft(
        tenant_id=tenant.tenant_id,
        task_id=task_id,
        result=result,
        checks_passed=checks_passed,
        reason=reason,
        diff_sha256=diff_sha256,
    )
    event_payload = {
        "task_id": task_id,
        "path": result.path,
        "mode": result.mode,
        "phase": result.phase,
        "ok": result.ok,
        "applied": result.applied,
        "rolled_back": result.rolled_back,
        "bytes_changed": result.bytes_changed,
        "checks_passed": checks_passed,
        "review_ok": result.review.ok if result.review is not None else None,
        "review_findings_count": len(result.review.findings) if result.review is not None else 0,
        "lint_count": len(result.lint_results),
        "lint_failed_count": sum(1 for lint in result.lint_results if not lint.ok),
        "test_count": len(result.test_results),
        "test_failed_count": sum(1 for test in result.test_results if not test.ok),
        "error": result.error[:500],
        "rollback_hint": result.rollback_hint[:500],
        "reason": reason,
        "diff_sha256": diff_sha256,
        "diff_bytes": len(result.diff.encode("utf-8")),
        "skill_draft_asset_id": skill_draft_asset_id,
    }
    ledger = getattr(request.app.state, "state_ledger", None)
    if task_id and ledger is not None and hasattr(ledger, "record_code_change"):
        try:
            ledger.record_code_change(
                task_id,
                tenant_id=tenant.tenant_id,
                path=result.path,
                mode=result.mode,
                phase=result.phase,
                ok=result.ok,
                applied=result.applied,
                rolled_back=result.rolled_back,
                checks_passed=checks_passed,
                reason=reason,
                bytes_changed=result.bytes_changed,
            )
        except Exception:
            log.warning(
                "code_capability.state_ledger_record_failed",
                extra={"task_id": task_id, "path": result.path},
                exc_info=True,
            )
    try:
        async with session_scope(tenant_id=tenant.tenant_id) as session:
            await emit(
                session,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="code.change.proposed",
                    payload=event_payload,
                    task_ref=task_id,
                ),
            )
    except Exception:
        log.warning(
            "code_capability.event_emit_failed",
            extra={"task_id": task_id, "path": result.path},
            exc_info=True,
        )
    if task_id:
        try:
            async with session_scope(tenant_id=tenant.tenant_id) as session:
                await persist_code_change_credit_report(
                    session,
                    tenant_id=tenant.tenant_id,
                    credit=CodeChangeCreditInput(
                        task_id=task_id,
                        path=result.path,
                        mode=result.mode,
                        phase=result.phase,
                        ok=result.ok,
                        applied=result.applied,
                        rolled_back=result.rolled_back,
                        checks_passed=checks_passed,
                        review_ok=result.review.ok if result.review is not None else None,
                        lint_failed_count=sum(1 for lint in result.lint_results if not lint.ok),
                        test_failed_count=sum(1 for test in result.test_results if not test.ok),
                        bytes_changed=result.bytes_changed,
                    ),
                )
        except Exception:
            log.warning(
                "code_capability.credit_persist_failed",
                extra={"task_id": task_id, "path": result.path},
                exc_info=True,
            )
    await _enqueue_code_change_problem_signal_if_needed(
        tenant_id=tenant.tenant_id,
        task_id=task_id,
        result=result,
        event_payload=event_payload,
        checks_passed=checks_passed,
    )


async def _enqueue_code_change_problem_signal_if_needed(
    *,
    tenant_id: str,
    task_id: str | None,
    result: ChangeWorkflowResult,
    event_payload: dict[str, Any],
    checks_passed: bool,
) -> None:
    """Let Qi learn from real CodeCapability failures, not just finished tasks."""

    if result.ok and not result.rolled_back and checks_passed:
        return
    severity = "error" if result.phase in {"input", "resolve", "review"} else "warning"
    if result.rolled_back:
        severity = "error"
    failed_checks = sum(1 for lint in result.lint_results if not lint.ok) + sum(
        1 for test in result.test_results if not test.ok
    )
    evidence = {
        **event_payload,
        "task_id": task_id,
        "queue_intent": "qi_code_failure_replay",
        "failure_phase": result.phase,
        "failed_checks": failed_checks,
        "review_findings": [
            {
                "severity": finding.severity,
                "rule": finding.rule,
                "message": finding.message,
                "path": finding.path,
                "line": finding.line,
            }
            for finding in (result.review.findings if result.review is not None else [])
        ][:20],
    }
    try:
        await persist_problem_signals(
            [
                QiProblemSignal.build(
                    tenant_id=tenant_id,
                    category="delivery",
                    severity=severity,
                    summary=f"CodeCapability change failed at {result.phase}: {result.path}",
                    source="code_capability.propose_change",
                    task_type="coding.code_capability",
                    evidence=evidence,
                )
            ]
        )
    except Exception:
        log.warning(
            "code_capability.qi_problem_signal_failed",
            extra={"task_id": task_id, "path": result.path, "phase": result.phase},
            exc_info=True,
        )


async def _store_code_skill_draft(
    *,
    tenant_id: str,
    task_id: str | None,
    result: ChangeWorkflowResult,
    checks_passed: bool,
    reason: str,
    diff_sha256: str,
) -> str:
    if not task_id or not result.ok or result.rolled_back:
        return ""
    strategy_search_records: list[dict[str, object]] = []
    if code_strategy_tree_search_enabled_from_env():
        record = await run_code_change_strategy_tree_search(
            CodeStrategySearchInput(
                task_id=task_id,
                path=result.path,
                mode=result.mode,
                phase=result.phase,
                checks_passed=checks_passed,
                review_ok=result.review.ok if result.review is not None else None,
                applied=result.applied,
                rolled_back=result.rolled_back,
                bytes_changed=result.bytes_changed,
                lint_failed_count=sum(1 for lint in result.lint_results if not lint.ok),
                test_failed_count=sum(1 for test in result.test_results if not test.ok),
                reason=reason,
                diff_sha256=diff_sha256,
            ),
            enabled=True,
            budget=configured_code_strategy_search_budget_from_env(),
        )
        strategy_search_records = [record.model_dump(mode="json")]
    asset = build_code_change_skill_draft_asset(
        tenant_id=tenant_id,
        task_id=task_id,
        path=result.path,
        mode=result.mode,
        phase=result.phase,
        checks_passed=checks_passed,
        review_ok=result.review.ok if result.review is not None else None,
        bytes_changed=result.bytes_changed,
        diff_sha256=diff_sha256,
        reason=reason,
        strategy_search_records=strategy_search_records,
    )
    if asset is None:
        return ""
    try:
        await get_store().put(asset)
        await _enqueue_code_strategy_search_signal_if_needed(
            tenant_id=tenant_id,
            task_id=task_id,
            result=result,
            skill_draft_asset_id=asset.asset_id,
            strategy_search_records=strategy_search_records,
        )
        return asset.asset_id
    except Exception:
        log.warning(
            "code_capability.skill_draft_store_failed",
            extra={"task_id": task_id, "path": result.path},
            exc_info=True,
        )
        return ""


async def _enqueue_code_strategy_search_signal_if_needed(
    *,
    tenant_id: str,
    task_id: str,
    result: ChangeWorkflowResult,
    skill_draft_asset_id: str,
    strategy_search_records: list[dict[str, object]],
) -> None:
    """Let Qi keep improving code workflows from successful strategy searches."""

    if not strategy_search_records:
        return
    try:
        await persist_problem_signals(
            [
                QiProblemSignal.build(
                    tenant_id=tenant_id,
                    category="delivery",
                    severity="info",
                    summary=(
                        "CodeCapability found a review-only strategy search "
                        f"candidate for {result.path}"
                    ),
                    source="code_capability.strategy_search",
                    task_type="coding.strategy_search",
                    evidence={
                        "task_id": task_id,
                        "path": result.path,
                        "mode": result.mode,
                        "phase": result.phase,
                        "applied": result.applied,
                        "rolled_back": result.rolled_back,
                        "bytes_changed": result.bytes_changed,
                        "skill_draft_asset_id": skill_draft_asset_id,
                        "strategy_search_records": strategy_search_records,
                        "queue_intent": "qi_code_strategy_search_review",
                        "production_action": False,
                    },
                )
            ]
        )
    except Exception:
        log.warning(
            "code_capability.strategy_search_signal_failed",
            extra={
                "task_id": task_id,
                "path": result.path,
                "skill_draft_asset_id": skill_draft_asset_id,
            },
            exc_info=True,
        )


def _change_checks_passed(result: ChangeWorkflowResult) -> bool:
    return all(lint.ok for lint in result.lint_results) and all(
        test.ok for test in result.test_results
    )


def _write_result_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "path": result.path,
        "bytes_written": result.bytes_written,
        "created": result.created,
        "error": result.error,
    }


def _lint_result_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "tool": result.tool,
        "issues": [
            {
                "path": issue.path,
                "line": issue.line,
                "column": issue.column,
                "message": issue.message,
            }
            for issue in result.issues
        ],
        "output": result.output,
        "error": result.error,
        "returncode": result.returncode,
        "duration_sec": result.duration_sec,
        "timed_out": result.timed_out,
    }


def _test_result_payload(result: Any) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "output": result.output,
        "error": result.error,
        "returncode": result.returncode,
        "duration_sec": result.duration_sec,
        "timed_out": result.timed_out,
    }


def _debug_payload(debug: Any) -> dict[str, Any]:
    return {
        "category": debug.category,
        "summary": debug.summary,
        "fix_hint": debug.fix_hint,
        "confidence": debug.confidence,
        "path": debug.path,
        "line": debug.line,
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
    "ChangeCheckRequest",
    "CheckRequest",
    "ProposeChangeRequest",
    "ReviewDiffRequest",
    "ReviewFileRequest",
    "RunPythonRequest",
    "router",
]
