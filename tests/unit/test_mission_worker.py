"""Mission resume worker tests."""

from __future__ import annotations

import pytest
from kun.datamodel.mission import ResumeRequest
from kun.engineering import mission_worker
from kun.engineering.mission_worker import MissionResumeWorker


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_is_honest_without_runner(monkeypatch) -> None:
    emitted: list[tuple[str, str]] = []

    async def fake_requests(**_kwargs):
        return [_request()]

    async def fake_emit(tenant_id, result):
        emitted.append((tenant_id, result.status))

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker().run_once(tenant_id="tenant-a")

    assert results[0].status == "skipped"
    assert "no mission resume runner" in results[0].reason
    assert emitted == [("tenant-a", "skipped")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_dispatches_when_runner_accepts(monkeypatch) -> None:
    accepted: list[str] = []
    emitted: list[str] = []

    async def fake_requests(**_kwargs):
        return [_request()]

    async def fake_emit(_tenant_id, result):
        emitted.append(result.status)

    async def runner(request: ResumeRequest) -> None:
        accepted.append(request.task_id)

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker(runner=runner).run_once(tenant_id="tenant-a")

    assert accepted == ["tk-1"]
    assert results[0].status == "dispatched"
    assert emitted == ["dispatched"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_marks_runner_failure(monkeypatch) -> None:
    async def fake_requests(**_kwargs):
        return [_request()]

    async def fake_emit(_tenant_id, _result):
        return None

    async def runner(_request: ResumeRequest) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker(runner=runner).run_once(tenant_id="tenant-a")

    assert results[0].status == "failed"
    assert "RuntimeError" in results[0].reason


def _request() -> ResumeRequest:
    return ResumeRequest(
        mission_id="msn-1",
        task_id="tk-1",
        runtime_status="queued",
        resume_attempts=1,
        reason="runtime_state_queued",
    )
