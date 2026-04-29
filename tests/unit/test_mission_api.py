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
    MissionBlockedResult,
    MissionBudgetSummary,
    MissionCreate,
    MissionExecutionSummary,
    MissionLedgerAudit,
    MissionMilestone,
    MissionReaperResult,
    MissionReview,
    MissionSnapshot,
    MissionTimeline,
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
def test_mission_timeline_route_is_not_treated_as_mission_id(monkeypatch) -> None:
    async def fake_timeline(*, tenant_id: str, mission_id: str, limit: int):
        return MissionTimeline(
            mission_id=mission_id,
            tenant_id=tenant_id,
            event_count=1,
            events=[],
        )

    async def fake_get_mission(*_args, **_kwargs):
        raise AssertionError("timeline should not call get_mission")

    monkeypatch.setattr(mission_api.mission_control, "get_mission_timeline", fake_timeline)
    monkeypatch.setattr(mission_api.mission_control, "get_mission", fake_get_mission)

    app = FastAPI()
    app.include_router(mission_api.router)
    client = TestClient(app)

    response = client.get("/api/missions/msn-1/timeline?limit=2")

    assert response.status_code == 200
    assert response.json()["mission_id"] == "msn-1"
    assert response.json()["event_count"] == 1


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_reaper_once_uses_tenant_and_cutoffs(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_reaper(
        *,
        tenant_id: str,
        queued_stale_after_sec: int,
        running_stale_after_sec: int,
        limit: int,
    ):
        captured.update(
            {
                "tenant_id": tenant_id,
                "queued": queued_stale_after_sec,
                "running": running_stale_after_sec,
                "limit": limit,
            }
        )
        return [
            MissionReaperResult(
                mission_id="msn-1",
                task_id="tk-1",
                previous_status="running",
                reason="stale_running_runtime",
                stale_for_sec=7200,
            )
        ]

    monkeypatch.setattr(mission_api.mission_control, "reap_stale_mission_tasks", fake_reaper)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.run_reaper_once(
            queued_stale_after_sec=120,
            running_stale_after_sec=240,
            limit=3,
        )

    assert result[0].task_id == "tk-1"
    assert result[0].status == "failed"
    assert captured == {"tenant_id": "tenant-a", "queued": 120, "running": 240, "limit": 3}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_block_exhausted_once_uses_tenant_and_max_attempts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_blocked(*, tenant_id: str, max_attempts: int, limit: int):
        captured.update({"tenant_id": tenant_id, "max_attempts": max_attempts, "limit": limit})
        return [
            MissionBlockedResult(
                mission_id="msn-1",
                task_id="tk-1",
                previous_status="queued",
                runtime_status="queued",
                reason="max_resume_attempts_exhausted",
                resume_attempts=3,
                max_attempts=max_attempts,
            )
        ]

    monkeypatch.setattr(mission_api.mission_control, "block_exhausted_mission_tasks", fake_blocked)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.block_exhausted_once(max_attempts=3, limit=8)

    assert result[0].status == "blocked"
    assert result[0].reason == "max_resume_attempts_exhausted"
    assert captured == {"tenant_id": "tenant-a", "max_attempts": 3, "limit": 8}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_mission_summary_returns_budget_rollup(monkeypatch) -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    async def fake_summary(*, tenant_id: str, mission_id: str):
        captured.update({"tenant_id": tenant_id, "mission_id": mission_id})
        return MissionExecutionSummary(
            mission_id=mission_id,
            tenant_id=tenant_id,
            status="running",
            budget=MissionBudgetSummary(
                budget_cap_usd=10.0,
                spent_actual_usd=1.25,
                spent_equivalent_usd=2.5,
                remaining_equivalent_usd=7.5,
                usage_fraction=0.25,
            ),
            task_status_counts={"running": 1},
            checkpoints=[],
            updated_at=now,
        )

    monkeypatch.setattr(mission_api.mission_control, "summarize_mission", fake_summary)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.get_mission_summary("msn-1")

    assert result.budget.spent_equivalent_usd == 2.5
    assert captured == {"tenant_id": "tenant-a", "mission_id": "msn-1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_mission_summary_returns_404_when_missing(monkeypatch) -> None:
    async def fake_summary(*, tenant_id: str, mission_id: str):
        return None

    monkeypatch.setattr(mission_api.mission_control, "summarize_mission", fake_summary)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a")),
        pytest.raises(mission_api.HTTPException) as exc,
    ):
        await mission_api.get_mission_summary("msn-missing")

    assert exc.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_mission_timeline_returns_event_rollup(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_timeline(*, tenant_id: str, mission_id: str, limit: int):
        captured.update({"tenant_id": tenant_id, "mission_id": mission_id, "limit": limit})
        return MissionTimeline(
            mission_id=mission_id,
            tenant_id=tenant_id,
            event_count=1,
            status_counts={"done": 1},
            total_cost_usd_equivalent=0.25,
            events=[],
        )

    monkeypatch.setattr(mission_api.mission_control, "get_mission_timeline", fake_timeline)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.get_mission_timeline("msn-1", limit=25)

    assert result.event_count == 1
    assert result.status_counts == {"done": 1}
    assert captured == {"tenant_id": "tenant-a", "mission_id": "msn-1", "limit": 25}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_mission_timeline_returns_404_when_missing(monkeypatch) -> None:
    async def fake_timeline(*, tenant_id: str, mission_id: str, limit: int):
        return None

    monkeypatch.setattr(mission_api.mission_control, "get_mission_timeline", fake_timeline)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a")),
        pytest.raises(mission_api.HTTPException) as exc,
    ):
        await mission_api.get_mission_timeline("msn-missing")

    assert exc.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_review_once_uses_tenant_and_limits(monkeypatch) -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    async def fake_review_active(
        *,
        tenant_id: str,
        limit: int,
        timeline_limit: int,
        min_interval_sec: int,
    ):
        captured.update(
            {
                "tenant_id": tenant_id,
                "limit": limit,
                "timeline_limit": timeline_limit,
                "min_interval_sec": min_interval_sec,
            }
        )
        return [
            MissionReview(
                mission_id="msn-1",
                tenant_id=tenant_id,
                milestone_id="mile-1",
                status="running",
                generated_at=now,
                budget=MissionBudgetSummary(),
                next_checkpoint="Continue scheduled resume and reaper loops.",
            )
        ]

    monkeypatch.setattr(mission_api.mission_control, "review_active_missions", fake_review_active)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.run_review_once(
            limit=3,
            timeline_limit=25,
            min_interval_sec=120,
        )

    assert result[0].milestone_id == "mile-1"
    assert captured == {
        "tenant_id": "tenant-a",
        "limit": 3,
        "timeline_limit": 25,
        "min_interval_sec": 120,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_returns_recorded_review(monkeypatch) -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    async def fake_review(*, tenant_id: str, mission_id: str, timeline_limit: int):
        captured.update(
            {"tenant_id": tenant_id, "mission_id": mission_id, "timeline_limit": timeline_limit}
        )
        return MissionReview(
            mission_id=mission_id,
            tenant_id=tenant_id,
            milestone_id="mile-1",
            status="running",
            generated_at=now,
            budget=MissionBudgetSummary(),
            next_checkpoint="Continue scheduled resume and reaper loops.",
        )

    monkeypatch.setattr(mission_api.mission_control, "review_mission", fake_review)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.review_mission("msn-1", timeline_limit=25)

    assert result.milestone_id == "mile-1"
    assert captured == {
        "tenant_id": "tenant-a",
        "mission_id": "msn-1",
        "timeline_limit": 25,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_mission_returns_404_when_missing(monkeypatch) -> None:
    async def fake_review(*, tenant_id: str, mission_id: str, timeline_limit: int):
        return None

    monkeypatch.setattr(mission_api.mission_control, "review_mission", fake_review)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a")),
        pytest.raises(mission_api.HTTPException) as exc,
    ):
        await mission_api.review_mission("msn-missing")

    assert exc.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_audit_mission_returns_ledger_audit(monkeypatch) -> None:
    captured: dict[str, object] = {}
    now = datetime.now(UTC)

    async def fake_audit(*, tenant_id: str, mission_id: str, timeline_limit: int):
        captured.update(
            {"tenant_id": tenant_id, "mission_id": mission_id, "timeline_limit": timeline_limit}
        )
        return MissionLedgerAudit(
            mission_id=mission_id,
            tenant_id=tenant_id,
            status="warn",
            checked_at=now,
            budget=MissionBudgetSummary(),
            timeline_event_count=1,
        )

    monkeypatch.setattr(mission_api.mission_control, "audit_mission_ledger", fake_audit)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        result = await mission_api.audit_mission("msn-1", timeline_limit=50)

    assert result.status == "warn"
    assert result.timeline_event_count == 1
    assert captured == {
        "tenant_id": "tenant-a",
        "mission_id": "msn-1",
        "timeline_limit": 50,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_audit_mission_returns_404_when_missing(monkeypatch) -> None:
    async def fake_audit(*, tenant_id: str, mission_id: str, timeline_limit: int):
        return None

    monkeypatch.setattr(mission_api.mission_control, "audit_mission_ledger", fake_audit)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a")),
        pytest.raises(mission_api.HTTPException) as exc,
    ):
        await mission_api.audit_mission("msn-missing")

    assert exc.value.status_code == 404


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
