from __future__ import annotations

import pytest
from kun.control_plane.collaboration import CollaborationQueueSummary
from kun.control_plane.progress import build_user_progress_summary
from kun.control_plane.runtime import ControlPlaneProgressReport


def _progress(**overrides: object) -> ControlPlaneProgressReport:
    payload: dict[str, object] = {
        "mission_id": "msn-v6",
        "status": "running",
        "current_plan_version": "v1",
        "total_work_items": 3,
        "work_item_counts": {"done": 1, "running": 1, "queued": 1},
        "next_ready_work_item_ids": ["work-next"],
        "ledger_event_count": 4,
        "artifact_manifest_count": 1,
    }
    payload.update(overrides)
    return ControlPlaneProgressReport.model_validate(payload)


@pytest.mark.unit
def test_user_progress_summary_for_running_task_is_actionable() -> None:
    summary = build_user_progress_summary(_progress())

    assert summary.tone == "working"
    assert summary.safe_to_continue is True
    assert summary.ready_work_item_ids == ["work-next"]
    assert "继续执行" in summary.next_step


@pytest.mark.unit
def test_user_progress_summary_explains_human_wait() -> None:
    summary = build_user_progress_summary(
        _progress(
            status="waiting_human",
            open_collaboration_ticket_ids=["ticket-1"],
            next_ready_work_item_ids=[],
        )
    )

    assert summary.tone == "waiting"
    assert summary.human_needed is True
    assert summary.safe_to_continue is False
    assert summary.open_ticket_ids == ["ticket-1"]
    assert "回复" in summary.next_step


@pytest.mark.unit
def test_user_progress_summary_excludes_environment_failure_from_capability_failure() -> None:
    summary = build_user_progress_summary(
        _progress(
            status="repairing",
            latest_gate_verdict="fail",
            latest_failure_category="environment_failure",
            next_ready_work_item_ids=[],
        )
    )

    assert summary.tone == "blocked"
    assert summary.quality_gate_status == "invalid"
    assert summary.safe_to_continue is False
    assert "不能算作能力失败" in summary.blocking_reason


@pytest.mark.unit
def test_user_progress_summary_uses_collaboration_queue_summary() -> None:
    summary = build_user_progress_summary(
        _progress(open_collaboration_ticket_ids=[]),
        collaboration=CollaborationQueueSummary(
            open_ticket_ids=["ticket-open"],
            waiting_ticket_ids=["ticket-waiting"],
            escalated_ticket_ids=["ticket-escalated"],
        ),
    )

    assert summary.human_needed is True
    assert summary.open_ticket_ids == ["ticket-open", "ticket-waiting", "ticket-escalated"]
