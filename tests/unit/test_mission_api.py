"""Mission API unit tests without a database."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api import missions as mission_api
from kun.core.tenancy import TenantContext, tenant_scope
from kun.datamodel.mission import (
    MissionCreate,
    MissionMilestone,
    MissionNextStep,
    MissionReview,
    MissionSnapshot,
    MissionStory,
    ResumeRequest,
)
from kun.engineering.mission_worker import MissionResumeResult


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
async def test_update_next_step_calls_service(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_update(next_step, *, tenant_id: str, mission_id: str):
        captured.update({"next_step": next_step, "tenant_id": tenant_id, "mission_id": mission_id})
        return _snapshot()

    monkeypatch.setattr(mission_api.mission_control, "update_mission_next_step", fake_update)

    payload = MissionNextStep(summary="继续推进首个客户访谈", reason="当前任务已完成")
    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        await mission_api.update_next_step("msn-1", payload)

    assert captured["tenant_id"] == "tenant-a"
    assert captured["mission_id"] == "msn-1"
    assert captured["next_step"] == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_review_calls_service(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_review(review, *, tenant_id: str, mission_id: str):
        captured.update({"review": review, "tenant_id": tenant_id, "mission_id": mission_id})
        return _snapshot()

    monkeypatch.setattr(mission_api.mission_control, "record_mission_review", fake_review)

    payload = MissionReview(summary="进展正常", next_step=MissionNextStep(summary="找下一个线索"))
    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        await mission_api.record_review("msn-1", payload)

    assert captured["tenant_id"] == "tenant-a"
    assert captured["mission_id"] == "msn-1"
    assert captured["review"] == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_mission_story_calls_service(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_story(*, tenant_id: str, mission_id: str, history_limit_per_task: int):
        captured.update(
            {
                "tenant_id": tenant_id,
                "mission_id": mission_id,
                "history_limit_per_task": history_limit_per_task,
            }
        )
        return MissionStory(
            mission_id=mission_id,
            title="运营产品",
            objective="持续推进商业化",
            status="running",
            risk_level="medium",
        )

    monkeypatch.setattr(mission_api.mission_control, "get_mission_story", fake_story)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.get_mission_story("msn-1", history_limit_per_task=25)

    assert result.mission_id == "msn-1"
    assert captured == {
        "tenant_id": "tenant-a",
        "mission_id": "msn-1",
        "history_limit_per_task": 25,
    }


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


@pytest.mark.unit
def test_resume_requests_route_is_not_treated_as_mission_id(monkeypatch) -> None:
    async def fake_resume(*, tenant_id: str, limit: int, max_attempts: int):
        return [
            ResumeRequest(
                mission_id="msn-1",
                task_id="tk-1",
                runtime_status="queued",
                resume_attempts=1,
                reason=f"{tenant_id}:{limit}:{max_attempts}",
            )
        ]

    async def fake_get_mission(*_args, **_kwargs):
        raise AssertionError("resume-requests should not call get_mission")

    monkeypatch.setattr(mission_api.mission_control, "request_resumable_tasks", fake_resume)
    monkeypatch.setattr(mission_api.mission_control, "get_mission", fake_get_mission)

    app = FastAPI()
    app.include_router(mission_api.router)
    client = TestClient(app)

    response = client.post("/api/missions/resume-requests?limit=2&max_attempts=4")

    assert response.status_code == 200
    assert response.json()[0]["task_id"] == "tk-1"
    assert response.json()[0]["reason"] == "u-sylvan:2:4"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_resume_worker_once_uses_installed_worker() -> None:
    captured: dict[str, object] = {}

    class FakeWorker:
        async def run_once(self, *, tenant_id: str, limit: int, max_attempts: int):
            captured.update({"tenant_id": tenant_id, "limit": limit, "max_attempts": max_attempts})
            return [
                MissionResumeResult(
                    mission_id="msn-1",
                    task_id="tk-1",
                    status="skipped",
                    reason="no mission resume runner attached",
                )
            ]

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(mission_resume_worker=FakeWorker()))
    )

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.run_resume_worker_once(
            request,
            limit=4,
            max_attempts=5,
        )

    assert result[0].status == "skipped"
    assert captured == {"tenant_id": "tenant-a", "limit": 4, "max_attempts": 5}


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
