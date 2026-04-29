"""Mission resume worker tests."""

from __future__ import annotations

from typing import cast

import pytest
from kun.core.tenancy import TenantContext, tenant_scope
from kun.datamodel.mission import ResumeRequest
from kun.engineering import mission_worker
from kun.engineering.mission_worker import (
    MissionOrchestratorRunner,
    MissionResumeResult,
    MissionResumeWorker,
    MissionRunnerOutcome,
)
from kun.engineering.orchestrator import Orchestrator, TaskResult


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_is_honest_without_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, str]] = []

    async def fake_requests(**_kwargs: object) -> list[ResumeRequest]:
        return [_request()]

    async def fake_emit(tenant_id: str, result: MissionResumeResult) -> None:
        emitted.append((tenant_id, result.status))

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker().run_once(tenant_id="tenant-a")

    assert results[0].status == "skipped"
    assert "no mission resume runner" in results[0].reason
    assert emitted == [("tenant-a", "skipped")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_dispatches_when_runner_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted: list[str] = []
    emitted: list[str] = []

    async def fake_requests(**_kwargs: object) -> list[ResumeRequest]:
        return [_request()]

    async def fake_emit(_tenant_id: str, result: MissionResumeResult) -> None:
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
async def test_resume_worker_reports_completed_runner_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []

    async def fake_requests(**_kwargs: object) -> list[ResumeRequest]:
        return [_request()]

    async def fake_emit(_tenant_id: str, result: MissionResumeResult) -> None:
        emitted.append(result.status)

    async def runner(_request: ResumeRequest) -> MissionRunnerOutcome:
        return MissionRunnerOutcome(
            executed_task_id="tk-exec",
            final_status="done",
            answer_preview="finished",
        )

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker(runner=runner).run_once(tenant_id="tenant-a")

    assert results[0].status == "completed"
    assert results[0].outcome is not None
    assert results[0].outcome.executed_task_id == "tk-exec"
    assert emitted == ["completed"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_worker_marks_runner_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_requests(**_kwargs: object) -> list[ResumeRequest]:
        return [_request()]

    async def fake_emit(_tenant_id: str, _result: MissionResumeResult) -> None:
        return None

    async def runner(_request: ResumeRequest) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mission_worker, "request_resumable_tasks", fake_requests)
    monkeypatch.setattr(mission_worker, "_emit_resume_result", fake_emit)

    results = await MissionResumeWorker(runner=runner).run_once(tenant_id="tenant-a")

    assert results[0].status == "failed"
    assert "RuntimeError" in results[0].reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_runner_marks_and_records_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeOrchestrator:
        async def run_mission_continuation(
            self,
            request: ResumeRequest,
            resume_prompt: str,
            *,
            output_kind: str,
        ) -> TaskResult:
            assert request.task_id == "tk-1"
            prompt = resume_prompt
            calls.append((prompt, output_kind))
            return TaskResult(
                task_id="tk-exec",
                status="done",
                answer="mission task finished",
                cost_usd_equivalent=0.12,
                tokens_in=10,
                tokens_out=20,
                duration_sec=1.5,
            )

    async def fake_prompt(tenant_id: str, request: ResumeRequest) -> str:
        assert tenant_id == "tenant-a"
        assert request.task_id == "tk-1"
        return "resume prompt"

    async def fake_started(tenant_id: str, request: ResumeRequest) -> None:
        calls.append((f"started:{tenant_id}", request.task_id))

    async def fake_record(
        tenant_id: str,
        request: ResumeRequest,
        outcome: MissionRunnerOutcome,
    ) -> None:
        calls.append((f"recorded:{tenant_id}", outcome.final_status))
        assert request.mission_id == "msn-1"
        assert outcome.executed_task_id == "tk-exec"

    monkeypatch.setattr(mission_worker, "_build_orchestrator_resume_prompt", fake_prompt)
    monkeypatch.setattr(mission_worker, "_mark_execution_started", fake_started)
    monkeypatch.setattr(mission_worker, "_record_execution_outcome", fake_record)

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        outcome = await MissionOrchestratorRunner(cast(Orchestrator, FakeOrchestrator())).__call__(
            _request()
        )

    assert outcome.final_status == "done"
    assert outcome.cost_usd_equivalent == 0.12
    assert calls == [
        ("started:tenant-a", "tk-1"),
        ("resume prompt", "mission_worker"),
        ("recorded:tenant-a", "done"),
    ]


def _request() -> ResumeRequest:
    return ResumeRequest(
        mission_id="msn-1",
        task_id="tk-1",
        runtime_status="queued",
        resume_attempts=1,
        reason="runtime_state_queued",
    )
