from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from kun.core.orm import TaskRow
from kun.engineering.pending_task_resume import (
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
