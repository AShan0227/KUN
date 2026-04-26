"""NUO diagnose panel anchor-expand tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.api.nuo.diagnose_panel import (
    _finding_to_item,
    _page_findings_anchor,
    _sort_findings_for_anchor,
    list_diagnose_findings,
)
from kun.core.tenancy import TenantContext, tenant_scope
from kun.security.diagnose_runner import DiagnoseFinding, DiagnoseReport


def _finding(
    finding_id: str,
    severity: str,
    subsystem: str = "engineering",
    category: str = "clean",
) -> DiagnoseFinding:
    return DiagnoseFinding(
        finding_id=finding_id,
        subsystem=subsystem,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        description=f"{finding_id} description",
        root_cause=f"{finding_id} cause",
    )


@pytest.mark.unit
def test_sort_findings_for_anchor_severity_then_subsystem() -> None:
    findings = [
        _finding("f-info", "info", "engineering"),
        _finding("f-error-data", "error", "data"),
        _finding("f-critical", "critical", "context"),
        _finding("f-error-security", "error", "security"),
        _finding("f-warn", "warn", "router_llm"),
    ]

    sorted_findings = _sort_findings_for_anchor(findings)

    assert [finding.finding_id for finding in sorted_findings] == [
        "f-critical",
        "f-error-security",
        "f-error-data",
        "f-warn",
        "f-info",
    ]


@pytest.mark.unit
def test_page_findings_anchor_first_page_returns_three() -> None:
    findings = [_finding(f"f-{i}", "warn") for i in range(5)]

    page, next_cursor, remaining, round_no = _page_findings_anchor(
        findings,
        limit=3,
        expand_after=None,
        max_rounds=3,
    )

    assert [finding.finding_id for finding in page] == ["f-0", "f-1", "f-2"]
    assert next_cursor == "f-2"
    assert remaining == 2
    assert round_no == 1


@pytest.mark.unit
def test_page_findings_anchor_expand_after_returns_next_three() -> None:
    findings = [_finding(f"f-{i}", "warn") for i in range(8)]

    page, next_cursor, remaining, round_no = _page_findings_anchor(
        findings,
        limit=3,
        expand_after="f-2",
        max_rounds=3,
    )

    assert [finding.finding_id for finding in page] == ["f-3", "f-4", "f-5"]
    assert next_cursor == "f-5"
    assert remaining == 2
    assert round_no == 2


@pytest.mark.unit
def test_page_findings_anchor_stale_cursor_returns_empty_page() -> None:
    findings = [_finding("f-1", "warn")]

    page, next_cursor, remaining, round_no = _page_findings_anchor(
        findings,
        limit=3,
        expand_after="stale",
        max_rounds=3,
    )

    assert page == []
    assert next_cursor is None
    assert remaining == 0
    assert round_no == 1


@pytest.mark.unit
def test_finding_to_item_preserves_diagnose_fields() -> None:
    finding = _finding("f-1", "critical", "security", "privacy")

    item = _finding_to_item(finding)

    assert item.finding_id == "f-1"
    assert item.severity == "critical"
    assert item.subsystem == "security"
    assert item.category == "privacy"
    assert item.root_cause == "f-1 cause"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_diagnose_findings_endpoint_shapes_anchor_page() -> None:
    class FakeRunner:
        async def run(self, request):
            return DiagnoseReport(
                request_id=request.request_id,
                started_at=request.triggered_at,
                completed_at=request.triggered_at,
                findings=[
                    _finding("f-low", "info"),
                    _finding("f-critical", "critical"),
                    _finding("f-warn", "warn"),
                    _finding("f-error", "error"),
                ],
            )

    app = SimpleNamespace(state=SimpleNamespace(diagnose_runner=FakeRunner()))
    request = SimpleNamespace(app=app)

    with tenant_scope(TenantContext(tenant_id="tenant-1")):
        response = await list_diagnose_findings(
            request,  # type: ignore[arg-type]
            x_user_id="user-1",
            hint_text="",
            limit=3,
            expand_after=None,
            max_rounds=3,
        )

    assert response.tenant_id == "tenant-1"
    assert [finding.finding_id for finding in response.findings] == [
        "f-critical",
        "f-error",
        "f-warn",
    ]
    assert response.next_cursor == "f-warn"
    assert response.has_more is True
    assert response.remaining == 1
