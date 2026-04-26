"""傩 · 诊断面板 anchor-expand API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel

from kun.api.runtime import get_diagnose_runner
from kun.core.ids import new_id
from kun.core.tenancy import current_tenant
from kun.security.diagnose_runner import DiagnoseFinding, DiagnoseRequest

router = APIRouter()

DiagnoseSeverity = Literal["info", "warn", "error", "critical"]


class DiagnoseFindingItem(BaseModel):
    finding_id: str
    subsystem: str
    category: str
    severity: DiagnoseSeverity
    description: str
    root_cause: str = ""
    cause_method: str = "rule"


class DiagnoseFindingList(BaseModel):
    tenant_id: str
    findings: list[DiagnoseFindingItem]
    next_cursor: str | None = None
    has_more: bool = False
    remaining: int = 0
    round: int = 1
    max_rounds: int = 3


@router.get("/findings", response_model=DiagnoseFindingList)
async def list_diagnose_findings(
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")] = "u-sylvan",
    hint_text: str = Query(default=""),
    limit: int = Query(default=3, ge=1, le=50),
    expand_after: str | None = Query(default=None),
    max_rounds: int = Query(default=3, ge=1, le=3),
) -> DiagnoseFindingList:
    """Run a lightweight NUO diagnosis and return highest-severity findings first."""
    tenant = current_tenant()
    runner = get_diagnose_runner(request.app)
    report = await runner.run(
        DiagnoseRequest(
            request_id=new_id("diag"),
            trigger="user_health_check_button",
            user_id=x_user_id,
            tenant_id=tenant.tenant_id,
            hint_text=hint_text,
        )
    )

    findings = _sort_findings_for_anchor(report.findings)
    page, next_cursor, remaining, round_no = _page_findings_anchor(
        findings,
        limit=limit,
        expand_after=expand_after,
        max_rounds=max_rounds,
    )
    return DiagnoseFindingList(
        tenant_id=tenant.tenant_id,
        findings=[_finding_to_item(finding) for finding in page],
        next_cursor=next_cursor,
        has_more=remaining > 0 and round_no < max_rounds,
        remaining=remaining if round_no < max_rounds else 0,
        round=round_no,
        max_rounds=max_rounds,
    )


_SEVERITY_ORDER = {"critical": 0, "error": 1, "warn": 2, "info": 3}
_SUBSYSTEM_ORDER = {
    "security": 0,
    "data": 1,
    "watchtower": 2,
    "router_llm": 3,
    "engineering": 4,
    "context": 5,
}


def _sort_findings_for_anchor(findings: list[DiagnoseFinding]) -> list[DiagnoseFinding]:
    return sorted(
        findings,
        key=lambda finding: (
            _SEVERITY_ORDER.get(finding.severity, 99),
            _SUBSYSTEM_ORDER.get(finding.subsystem, 99),
            finding.finding_id,
        ),
    )


def _page_findings_anchor(
    findings: list[DiagnoseFinding],
    *,
    limit: int,
    expand_after: str | None,
    max_rounds: int,
) -> tuple[list[DiagnoseFinding], str | None, int, int]:
    if expand_after is None:
        start = 0
    else:
        idx = next(
            (i for i, finding in enumerate(findings) if finding.finding_id == expand_after), None
        )
        start = len(findings) if idx is None else idx + 1
    round_no = min(max(start // limit + 1, 1), max_rounds)
    if round_no > max_rounds:
        return ([], None, 0, round_no)
    page = findings[start : start + limit]
    next_cursor = page[-1].finding_id if page else None
    remaining = max(0, len(findings) - (start + len(page)))
    return (page, next_cursor, remaining, round_no)


def _finding_to_item(finding: DiagnoseFinding) -> DiagnoseFindingItem:
    return DiagnoseFindingItem(
        finding_id=finding.finding_id,
        subsystem=finding.subsystem,
        category=finding.category,
        severity=finding.severity,
        description=finding.description,
        root_cause=finding.root_cause,
        cause_method=finding.cause_method,
    )


__all__ = [
    "DiagnoseFindingItem",
    "DiagnoseFindingList",
    "list_diagnose_findings",
]
