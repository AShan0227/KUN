from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.control_plane import (
    CollaborationResponse,
    CollaborationTicket,
    InMemoryCollaborationQueue,
    InMemoryControlPlane,
    Mission,
    WorkItem,
)


def _ticket(*, deadline: datetime | None = None) -> CollaborationTicket:
    return CollaborationTicket(
        ticket_id="ticket-v6",
        mission_id="msn-v6",
        type="user_decision",
        role_needed="customer",
        why_needed="Need approval before external action.",
        decision_options=["approve", "hold"],
        recommended_option="hold",
        context_ref="ctx-v6",
        risk_if_skipped="External action may violate user intent.",
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
        fallback_policy={"option": "hold", "reason": "deadline expired"},
        output_contract="Decision option and rationale.",
    )


@pytest.mark.unit
def test_collaboration_queue_records_answer_and_resume_signal() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(_ticket())

    updated = queue.respond(
        CollaborationResponse(
            ticket_id="ticket-v6",
            responder="customer",
            selected_option="approve",
            answer="Approved for dry-run only.",
        )
    )

    assert updated.status == "answered"
    assert queue.responses["ticket-v6"].resume_allowed is True
    assert queue.summary().answered_ticket_ids == ["ticket-v6"]


@pytest.mark.unit
def test_collaboration_queue_rejects_invalid_decision_option() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(_ticket())

    with pytest.raises(ValueError, match="selected_option"):
        queue.respond(
            CollaborationResponse(
                ticket_id="ticket-v6",
                responder="customer",
                selected_option="ship_now",
            )
        )


@pytest.mark.unit
def test_collaboration_queue_detects_overdue_and_applies_fallback() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(_ticket(deadline=datetime(2026, 5, 17, tzinfo=UTC)))

    summary = queue.summary(now=datetime(2026, 5, 18, tzinfo=UTC))
    assert summary.overdue_ticket_ids == ["ticket-v6"]

    updated = queue.apply_fallback("ticket-v6")
    assert updated.status == "fallback_selected"
    assert queue.responses["ticket-v6"].selected_option == "hold"


@pytest.mark.unit
def test_control_plane_emits_due_collaboration_sla_reminder_once() -> None:
    runtime = InMemoryControlPlane()
    runtime.missions["msn-v6"] = Mission(
        mission_id="msn-v6",
        owner="kun",
        objective="Wait for required product constraints.",
        task_type="product_development",
        status="info_gap",
    )
    runtime.record_collaboration_ticket(
        _ticket(
            deadline=datetime.now(UTC) + timedelta(hours=24),
        ).model_copy(update={"sla_policy": {"reminder_after_hours": 6}}),
        actor="kun-intake",
    )

    first = runtime.emit_collaboration_sla_reminders(
        "msn-v6",
        now=datetime.now(UTC) + timedelta(hours=7),
    )
    second = runtime.emit_collaboration_sla_reminders(
        "msn-v6",
        now=datetime.now(UTC) + timedelta(hours=8),
    )

    assert len(first) == 1
    assert second == []
    assert first[0].payload["intent"] == "collaboration_reminder"
    assert first[0].payload["receiver"] == "customer"


@pytest.mark.unit
def test_collaboration_response_completes_matching_human_work_item() -> None:
    runtime = InMemoryControlPlane()
    runtime.missions["msn-v6"] = Mission(
        mission_id="msn-v6",
        owner="kun",
        objective="Resume product work after operator confirms workspace access.",
        task_type="product_development",
        status="waiting_human",
    )
    runtime.work_items["work-permission-preflight"] = WorkItem(
        work_item_id="work-permission-preflight",
        mission_id="msn-v6",
        task_plan_version="plan-v1",
        type="collaboration",
        owner="human-operator",
        phase="permission_preflight",
        status="waiting_human",
        expected_output="Operator resolves writable workspace boundary before implementation.",
    )
    runtime.record_collaboration_ticket(
        CollaborationTicket(
            ticket_id="ticket-workspace-access",
            mission_id="msn-v6",
            type="operator_action",
            role_needed="workspace operator / user",
            why_needed="Workspace must be writable before KUN resumes product work.",
            context_ref="workspace://project",
            risk_if_skipped="The daemon would keep creating runner_missing observations.",
            deadline=datetime.now(UTC) + timedelta(hours=1),
            output_contract="Writable workspace confirmation.",
        ),
        actor="kun",
    )

    runtime.record_collaboration_response(
        CollaborationResponse(
            ticket_id="ticket-workspace-access",
            responder="operator",
            answer="Workspace write access verified.",
            resume_allowed=True,
        ),
        actor="operator",
    )

    assert runtime.work_items["work-permission-preflight"].status == "done"
    assert runtime.missions["msn-v6"].status == "queued"
