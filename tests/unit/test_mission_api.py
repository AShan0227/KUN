"""Mission API unit tests without a database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.api import missions as mission_api
from kun.core.tenancy import TenantContext, tenant_scope
from kun.datamodel.mission import MissionCreate, MissionMilestone, MissionSnapshot, ResumeRequest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_mission_passes_tenant_and_user(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create(payload, *, tenant_id: str, user_id: str | None = None):
        captured.update({"payload": payload, "tenant_id": tenant_id, "user_id": user_id})
        return _snapshot()

    monkeypatch.setattr(mission_api.mission_control, "create_mission", fake_create)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        result = await mission_api.create_mission(
            MissionCreate(title="运营产品", objective="持续推进商业化")
        )

    assert result.mission_id == "msn-1"
    assert captured["tenant_id"] == "tenant-a"
    assert captured["user_id"] == "user-a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attach_task_returns_404_for_missing_mission(monkeypatch) -> None:
    async def fake_attach(**_kwargs):
        raise KeyError("mission not found: msn-missing")

    monkeypatch.setattr(mission_api.mission_control, "attach_task_to_mission", fake_attach)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a")),
        pytest.raises(mission_api.HTTPException) as exc,
    ):
        await mission_api.attach_task(
            "msn-missing",
            mission_api.AttachTaskRequest(task_id="tk-1"),
        )

    assert exc.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_milestone_calls_service(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_record(milestone, *, tenant_id: str, mission_id: str):
        captured.update({"milestone": milestone, "tenant_id": tenant_id, "mission_id": mission_id})
        return _snapshot()

    monkeypatch.setattr(mission_api.mission_control, "record_milestone", fake_record)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        await mission_api.record_milestone(
            "msn-1",
            MissionMilestone(title="首个外部动作 dry-run", sequence_no=1),
        )

    assert captured["tenant_id"] == "tenant-a"
    assert captured["mission_id"] == "msn-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_resume_uses_tenant(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_resume(*, tenant_id: str, limit: int, max_attempts: int):
        captured.update({"tenant_id": tenant_id, "limit": limit, "max_attempts": max_attempts})
        return [
            ResumeRequest(
                mission_id="msn-1",
                task_id="tk-1",
                runtime_status="queued",
                resume_attempts=1,
                reason="runtime_state_queued",
            )
        ]

    monkeypatch.setattr(mission_api.mission_control, "request_resumable_tasks", fake_resume)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.request_resume(limit=2, max_attempts=4)

    assert result[0].task_id == "tk-1"
    assert captured == {"tenant_id": "tenant-a", "limit": 2, "max_attempts": 4}


def _snapshot() -> MissionSnapshot:
    now = datetime.now(UTC)
    return MissionSnapshot(
        mission_id="msn-1",
        tenant_id="tenant-a",
        user_id="user-a",
        title="运营产品",
        objective="持续推进商业化",
        status="planned",
        risk_level="medium",
        created_at=now,
        updated_at=now,
    )
