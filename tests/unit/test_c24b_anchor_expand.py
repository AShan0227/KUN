"""C24-b anchor-expand adapters for diagnostics and incident paths."""

from __future__ import annotations

import pytest
from kun.core.emergent_solution import EmergentSolutionLibrary
from kun.engineering.external_scan import ExternalInfoScanner
from kun.security.diagnose_runner import DiagnoseRequest, DiagnoseRunner
from kun.security.incident_response import IncidentEvent, IncidentResponseEngine


async def _collect(async_iter) -> list:
    items = []
    async for item in async_iter:
        items.append(item)
    return items


def _diagnose_request(hint: str) -> DiagnoseRequest:
    return DiagnoseRequest(
        request_id="diag-1",
        trigger="user_health_check_button",
        user_id="u1",
        tenant_id="t1",
        hint_text=hint,
    )


@pytest.mark.asyncio
async def test_scope_anchor_prioritizes_security_or_data_hit() -> None:
    runner = DiagnoseRunner()

    findings = await _collect(
        runner.scope_identify_anchor_then_expand(
            _diagnose_request("memory auth tenant model"),
            max_rounds=1,
        )
    )

    assert len(findings) == 1
    assert findings[0].subsystem == "security"


@pytest.mark.asyncio
async def test_scope_anchor_expands_multiple_findings_without_duplicates() -> None:
    runner = DiagnoseRunner()

    findings = await _collect(
        runner.scope_identify_anchor_then_expand(
            _diagnose_request("memory auth tenant model"),
            max_rounds=3,
        )
    )

    assert len(findings) == 3
    assert len({f.finding_id for f in findings}) == 3


@pytest.mark.asyncio
async def test_scope_anchor_default_finding_when_no_keyword() -> None:
    runner = DiagnoseRunner()

    findings = await _collect(
        runner.scope_identify_anchor_then_expand(
            _diagnose_request("nothing specific"),
            max_rounds=3,
        )
    )

    assert len(findings) == 1
    assert findings[0].subsystem == "engineering"


@pytest.mark.asyncio
async def test_external_scan_anchor_scans_one_source_per_round() -> None:
    library = EmergentSolutionLibrary()

    async def fetcher(task_type: str):
        return [{"url": f"https://example.test/{task_type}", "snippet": "useful"}]

    scanner = ExternalInfoScanner(
        library,
        fetchers={"github_issue": fetcher, "arxiv": fetcher},
        user_top_task_types_lookup=lambda _uid: ["coding", "writing"],
    )

    results = await _collect(scanner.scan_for_user_anchor_then_expand("u1", max_rounds=2))

    assert [r.sources_queried for r in results] == [1, 1]
    assert sum(r.candidates_added for r in results) == 2
    assert len(library.list_for_task_type("coding")) == 2


@pytest.mark.asyncio
async def test_external_scan_anchor_respects_telemetry_disabled() -> None:
    scanner = ExternalInfoScanner(
        EmergentSolutionLibrary(),
        fetchers={"github_issue": lambda _task_type: []},  # type: ignore[dict-item]
        user_top_task_types_lookup=lambda _uid: ["coding"],
        user_telemetry_enabled=lambda _uid: False,
    )

    assert await _collect(scanner.scan_for_user_anchor_then_expand("u1")) == []


@pytest.mark.asyncio
async def test_external_scan_anchor_respects_budget() -> None:
    library = EmergentSolutionLibrary()

    async def fetcher(_task_type: str):
        return [{"snippet": "x"}]

    scanner = ExternalInfoScanner(
        library,
        fetchers={"github_issue": fetcher, "arxiv": fetcher},
        user_top_task_types_lookup=lambda _uid: ["coding"],
        default_daily_limit=1,
    )

    results = await _collect(scanner.scan_for_user_anchor_then_expand("u1", max_rounds=2))

    assert [r.sources_queried for r in results] == [1, 0]
    assert len(library.list_for_task_type("coding")) == 1


@pytest.mark.asyncio
async def test_incident_actions_anchor_returns_first_action_only() -> None:
    engine = IncidentResponseEngine()
    event = IncidentEvent(
        incident_id="inc-1",
        severity="L3",
        category="security",
        title="prompt injection",
        affected_task_id="task-1",
    )

    actions = await _collect(engine.iter_response_actions_anchor_then_expand(event, max_rounds=1))

    assert [a.action_kind for a in actions] == ["log_only"]
    assert actions[0].target == "task-1"


@pytest.mark.asyncio
async def test_incident_actions_anchor_expands_in_matrix_order() -> None:
    engine = IncidentResponseEngine()
    event = IncidentEvent(
        incident_id="inc-1",
        severity="L3",
        category="security",
        title="prompt injection",
        affected_user_id="u1",
    )

    actions = await _collect(engine.iter_response_actions_anchor_then_expand(event, max_rounds=3))

    assert [a.action_kind for a in actions] == ["log_only", "notify_user", "pause_task"]


@pytest.mark.asyncio
async def test_incident_actions_preview_does_not_mutate_pattern_counts() -> None:
    engine = IncidentResponseEngine()
    event = IncidentEvent(
        incident_id="inc-1",
        severity="L1",
        category="cost",
        title="small spike",
        affected_user_id="u1",
    )

    await _collect(engine.iter_response_actions_anchor_then_expand(event, max_rounds=1))

    assert engine.get_pattern_counts() == {}


@pytest.mark.asyncio
async def test_incident_actions_can_apply_upgrade_when_requested() -> None:
    engine = IncidentResponseEngine()
    event = IncidentEvent(
        incident_id="inc-1",
        severity="L1",
        category="cost",
        title="small spike",
        affected_user_id="u1",
    )

    await _collect(
        engine.iter_response_actions_anchor_then_expand(
            event,
            max_rounds=1,
            apply_upgrade=True,
        )
    )

    assert engine.get_pattern_counts() == {("cost", "u1"): 1}
