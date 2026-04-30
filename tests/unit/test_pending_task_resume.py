from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from kun.core.orm import TaskRow
from kun.engineering.pending_task_resume import (
    PendingTaskResumeResult,
    PendingTaskResumeWorker,
    _build_resume_prompt_from_task,
    _event_type_for_status,
    _resume_request_from_result_json,
)


@pytest.mark.unit
def test_resume_prompt_tells_runner_not_to_repeat_approved_side_effects() -> None:
    task = cast(
        TaskRow,
        SimpleNamespace(
            task_id="task-1",
            task_type="ops.email",
            risk_level="medium",
            success_criteria_short="send the approved customer update",
            spec_json={
                "goal_detail": "Prepare and deliver the customer update",
                "success_metrics": ["customer receives a clear update"],
                "constraints": ["do not resend if already sent"],
                "required_skills": ["writing"],
                "required_tools": ["email"],
            },
        ),
    )

    prompt = _build_resume_prompt_from_task(task)

    assert "Original task ID: task-1" in prompt
    assert "Approved side-effect actions have already passed" in prompt
    assert "Do not repeat an external side effect" in prompt


@pytest.mark.unit
def test_resume_event_type_tracks_original_task_status() -> None:
    assert _event_type_for_status("done") == "task.done"
    assert _event_type_for_status("paused") == "task.paused"
    assert _event_type_for_status("cancelled") == "task.cancelled"
    assert _event_type_for_status("failed") == "task.failed"


@pytest.mark.unit
def test_resume_request_from_result_json_requires_real_resume_marker() -> None:
    assert _resume_request_from_result_json({"resume_ready": False}) == {}
    assert _resume_request_from_result_json({"resume_ready": True})["status"] == "queued"
    assert (
        _resume_request_from_result_json(
            {
                "resume_ready": True,
                "resume_request": {
                    "needed": True,
                    "status": "running",
                    "reason": "already_claimed",
                },
            }
        )
        == {}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_task_resume_worker_runs_explicit_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_resume_once(
        *,
        tenant_id: str,
        task_id: str,
        orchestrator: object,
    ) -> PendingTaskResumeResult:
        assert orchestrator is fake_orchestrator
        calls.append((tenant_id, task_id))
        return PendingTaskResumeResult(
            source_task_id=task_id,
            continuation_task_id=f"{task_id}-cont",
            status="completed",
            final_status="done",
            message="ok",
        )

    fake_orchestrator = object()
    monkeypatch.setattr(
        "kun.engineering.pending_task_resume.resume_unblocked_task_once", fake_resume_once
    )

    worker = PendingTaskResumeWorker(fake_orchestrator, max_tasks_per_run=10)  # type: ignore[arg-type]
    results = await worker.run_once(tenant_id="tenant-a", task_ids=["task-1", "task-2"])

    assert calls == [("tenant-a", "task-1"), ("tenant-a", "task-2")]
    assert [result.continuation_task_id for result in results] == ["task-1-cont", "task-2-cont"]
