from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from kun.engineering.coordination_remediation import run_coordination_remediation
from kun.engineering.system_coordination import coordination_issues_from_rows


def _low_risk_stale_issue() -> list[Any]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return coordination_issues_from_rows(
        pending_rows=[
            SimpleNamespace(
                action_id="act-low",
                task_ref="task-1",
                action_type="email.draft",
                status="approved",
                updated_at=now - timedelta(minutes=10),
            )
        ],
        runtime_rows=[],
        control_rows=[],
        now=now,
        stale_after=timedelta(minutes=5),
    )


def _high_risk_stale_issue() -> list[Any]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return coordination_issues_from_rows(
        pending_rows=[
            SimpleNamespace(
                action_id="act-high",
                task_ref="task-2",
                action_type="email.send",
                status="approved",
                updated_at=now - timedelta(minutes=10),
            )
        ],
        runtime_rows=[],
        control_rows=[],
        now=now,
        stale_after=timedelta(minutes=5),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coordination_remediation_dry_run_does_not_execute(monkeypatch) -> None:
    async def fake_collect_coordination_issues(**kwargs: Any) -> list[Any]:
        return _low_risk_stale_issue()

    async def fail_execute(**kwargs: Any) -> None:
        raise AssertionError("dry-run must not execute")

    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.collect_coordination_issues",
        fake_collect_coordination_issues,
    )
    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.execute_approved_action_once",
        fail_execute,
    )

    report = await run_coordination_remediation(tenant_id="t-1", mode="dry_run")

    assert report.issues == 1
    assert report.planned == 1
    assert report.executed == 0
    assert report.production_action is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coordination_remediation_executes_only_low_risk(monkeypatch) -> None:
    from kun.engineering.action_executor import ActionExecutionResult

    calls: list[str] = []

    async def fake_collect_coordination_issues(**kwargs: Any) -> list[Any]:
        return _low_risk_stale_issue()

    async def fake_execute(tenant_id: str, action_id: str) -> ActionExecutionResult:
        calls.append(action_id)
        return ActionExecutionResult(
            action_id=action_id,
            task_ref="task-1",
            action_status="executed",
            task_status="queued",
            message="Action executed and task queued.",
        )

    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.collect_coordination_issues",
        fake_collect_coordination_issues,
    )
    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.execute_approved_action_once",
        fake_execute,
    )

    report = await run_coordination_remediation(tenant_id="t-1", mode="auto_low_risk")

    assert calls == ["act-low"]
    assert report.executed == 1
    assert report.production_action is True
    assert report.attempts[0].execution is not None
    assert report.attempts[0].execution["task_status"] == "queued"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coordination_remediation_blocks_high_risk(monkeypatch) -> None:
    async def fake_collect_coordination_issues(**kwargs: Any) -> list[Any]:
        return _high_risk_stale_issue()

    async def fail_execute(**kwargs: Any) -> None:
        raise AssertionError("high-risk actions must stay manual")

    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.collect_coordination_issues",
        fake_collect_coordination_issues,
    )
    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.execute_approved_action_once",
        fail_execute,
    )

    report = await run_coordination_remediation(tenant_id="t-1", mode="auto_low_risk")

    assert report.blocked == 1
    assert report.executed == 0
    assert report.production_action is False
    assert "人工" in report.attempts[0].reason
