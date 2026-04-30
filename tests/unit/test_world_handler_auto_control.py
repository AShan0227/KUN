from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.world import handler_auto_control
from kun.world.handler_auto_control import run_world_handler_auto_quarantine
from kun.world.handler_health import WorldHandlerHealthCard


@pytest.mark.unit
async def test_auto_quarantine_recommends_high_failure_handler() -> None:
    report = await run_world_handler_auto_quarantine(
        tenant_id="tenant-a",
        dry_run=True,
        cards=[
            WorldHandlerHealthCard(
                action_type="email.send",
                status="blocked",
                registered=True,
                configured=True,
                external_dispatched=True,
                has_compensation=True,
                total_seen=4,
                failure_rate=0.5,
                recommendation="stop",
            )
        ],
    )

    assert report.dry_run is True
    assert report.applied_count == 0
    assert len(report.decisions) == 1
    assert report.decisions[0].action_type == "email.send"
    assert report.decisions[0].recommended_status == "review_required"
    assert report.decisions[0].can_auto_apply is False
    assert report.decisions[0].requires_human_confirmation is True
    assert report.decisions[0].risk_summary["external_dispatch_risk"] is True
    assert "失败率" in report.decisions[0].reason


@pytest.mark.unit
async def test_auto_quarantine_skips_already_quarantined_handler() -> None:
    report = await run_world_handler_auto_quarantine(
        tenant_id="tenant-a",
        dry_run=True,
        cards=[
            WorldHandlerHealthCard(
                action_type="email.send",
                status="blocked",
                registered=True,
                configured=True,
                external_dispatched=True,
                has_compensation=True,
                total_seen=4,
                failure_rate=0.5,
                control_status="quarantined",
                recommendation="stop",
            )
        ],
    )

    assert report.decisions == []


@pytest.mark.unit
async def test_auto_quarantine_recommends_external_handler_without_compensation() -> None:
    report = await run_world_handler_auto_quarantine(
        tenant_id="tenant-a",
        dry_run=True,
        cards=[
            WorldHandlerHealthCard(
                action_type="browser.execute",
                status="limited",
                registered=True,
                configured=True,
                external_dispatched=True,
                has_compensation=False,
                recommendation="manual only",
            )
        ],
    )

    assert len(report.decisions) == 1
    assert "补偿" in report.decisions[0].reason
    assert report.decisions[0].can_auto_apply is False
    assert "不会自动关掉" in report.decisions[0].reason


@pytest.mark.unit
async def test_auto_quarantine_apply_only_low_risk_handlers(monkeypatch) -> None:
    applied: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_session_scope(**_kwargs: object) -> AsyncIterator[object]:
        yield object()

    async def fake_set_world_handler_control(
        _session: object,
        *,
        tenant_id: str,
        action_type: str,
        status: str,
        **_kwargs: object,
    ) -> None:
        applied.append((action_type, status))

    monkeypatch.setattr(handler_auto_control, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        handler_auto_control,
        "set_world_handler_control",
        fake_set_world_handler_control,
    )

    report = await run_world_handler_auto_quarantine(
        tenant_id="tenant-a",
        dry_run=False,
        cards=[
            WorldHandlerHealthCard(
                action_type="local_file.write",
                status="blocked",
                registered=True,
                configured=True,
                external_dispatched=False,
                has_compensation=True,
                static_risk="low",
                dynamic_risk="low",
                total_seen=4,
                failure_rate=0.0,
                recommendation="stop",
            ),
            WorldHandlerHealthCard(
                action_type="email.send",
                status="limited",
                registered=True,
                configured=False,
                external_dispatched=True,
                has_compensation=False,
                static_risk="high",
                total_seen=0,
                recommendation="manual only",
            ),
        ],
    )

    assert applied == [("local_file.write", "quarantined")]
    assert report.applied_count == 1
    by_action = {decision.action_type: decision for decision in report.decisions}
    assert by_action["local_file.write"].applied is True
    assert by_action["local_file.write"].can_auto_apply is True
    assert by_action["email.send"].applied is False
    assert by_action["email.send"].recommended_status == "review_required"
    assert by_action["email.send"].risk_summary["missing_secrets"] is True
    assert by_action["email.send"].data_quality == "partial"
