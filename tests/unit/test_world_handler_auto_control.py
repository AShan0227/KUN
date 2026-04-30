from __future__ import annotations

import pytest
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
    assert report.decisions[0].recommended_status == "quarantined"
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
