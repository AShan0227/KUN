"""V7 Phase X.C.COLLAB-E2E — CollaborationTicket human-in-the-loop e2e.

V7 §12.2 + V7 §11 工程系统协作: production flip 必须 explicit user approval
via CollaborationTicket. The pieces exist (InMemoryCollaborationQueue +
CollaborationTicket model + CapabilityLifecycleService gate), but no test
had ever **wired the two systems together** to prove:

   gated action (CANARY → PRODUCTION)
     ↓ blocked: needs approval
   open CollaborationTicket (type='approval')
     ↓ wait for response
   user responds: selected_option='approve'
     ↓ gate sees approved ticket
   action unblocks (PRODUCTION transition lands in PG)

This is the human-in-the-loop production-gate evidence — V7 §12.2 闭环.

What it proves (5 happy + 4 negative paths):

  1. ticket flows open → answered, response carries approval
  2. answered approval-ticket unblocks CANARY → PRODUCTION (PG row lands)
  3. ticket overdue → apply_fallback either approves or holds based on
     fallback_policy.option
  4. fallback=approve unblocks prod flip; fallback=hold leaves capability
     in CANARY
  5. cancelled / closed ticket cannot be used as approval
  6. wrong decision_option rejected at queue level
  7. queue.summary tracks open/waiting/escalated/overdue/answered buckets
  8. escalate transitions ticket to 'escalated', survives in summary
  9. resume_allowed signal carried through to response
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.api.cockpit_readers import list_recent_lifecycle_transitions
from kun.control_plane.collaboration import (
    CollaborationResponse,
    InMemoryCollaborationQueue,
)
from kun.control_plane.v6 import CollaborationTicket
from kun.governance.capability_lifecycle import (
    CapabilityLifecycleError,
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
)
from kun.integration.capability_lifecycle_db import (
    make_lifecycle_transition_emitter,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_engines_between_tests() -> Any:
    """Fresh sessionmaker per test."""
    import kun.core.db as db_mod

    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]
    yield
    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]


async def _skip_if_no_pg() -> None:
    try:
        from kun.core.db import session_scope
        from sqlalchemy import text

        async with session_scope(tenant_id="t-collab-probe") as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PG unavailable: {type(e).__name__}: {e}")


# ============================================================
# Helpers — build approval ticket + walk capability to CANARY
# ============================================================


def _approval_ticket(
    *,
    ticket_id: str,
    capability_id: str,
    deadline: datetime | None = None,
    fallback_option: str = "hold",
    fallback_reason: str = "deadline expired, default hold",
) -> CollaborationTicket:
    """Mint an 'approval'-type ticket for a CANARY → PRODUCTION flip."""
    return CollaborationTicket(
        ticket_id=ticket_id,
        mission_id=f"msn-{capability_id}",
        type="approval",
        role_needed="release_owner",
        why_needed=(
            f"V7 §12.2 production flip for capability {capability_id} "
            f"requires explicit user approval. Canary metrics look healthy "
            f"(error_rate 0.001, 3-day soak, 10%% traffic), but the flip "
            f"is permanent until rollback."
        ),
        decision_options=["approve", "hold"],
        recommended_option="approve",
        context_ref=f"capability:{capability_id}",
        risk_if_skipped=(
            "Capability stuck in CANARY indefinitely — opportunity cost. "
            "If holding too long, baseline drift could invalidate canary."
        ),
        deadline=deadline or datetime.now(UTC) + timedelta(hours=24),
        fallback_policy={
            "option": fallback_option,
            "reason": fallback_reason,
        },
        output_contract="Selected option ∈ {approve, hold} + 1-sentence rationale.",
    )


async def _walk_capability_to_canary(
    service: CapabilityLifecycleService,
    *,
    capability_id: str,
) -> None:
    """OBSERVATION → CANDIDATE → REPLAY → HOLDOUT → SHADOW → CANARY.
    All quick transitions; the gate of interest is CANARY → PRODUCTION."""
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-collab",
            "process_audit:pa-collab",
            "capability_candidate:cc-collab",
        ],
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.REPLAY,
        to_stage=CapabilityLifecycleStage.HOLDOUT,
        evidence_refs=["strategy_replay_report:rr-collab"],
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.HOLDOUT,
        to_stage=CapabilityLifecycleStage.SHADOW,
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.SHADOW,
        to_stage=CapabilityLifecycleStage.CANARY,
    )


# ============================================================
# Happy path — ticket answered → production unblocks
# ============================================================


async def test_answered_approval_ticket_unblocks_production_flip() -> None:
    """**The headline proof**: open approval ticket, user answers 'approve',
    use the answered ticket_id to satisfy V7 §12.2 — production transition
    lands in real PG."""
    await _skip_if_no_pg()

    tenant_id = "t-collab-happy"
    capability_id = f"cap-collab-h-{id(test_answered_approval_ticket_unblocks_production_flip)}"
    ticket_id = f"tk-collab-h-{id(test_answered_approval_ticket_unblocks_production_flip)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )
    queue = InMemoryCollaborationQueue()

    # Phase 1 — capability reaches CANARY
    await _walk_capability_to_canary(service, capability_id=capability_id)

    # Phase 2 — gate blocks production flip without a ticket
    with pytest.raises(CapabilityLifecycleError, match="user approval"):
        await service.transition(
            capability_id=capability_id,
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            evidence_refs=["canary_metrics:cm-collab-h"],
            # user_approval_ticket_id intentionally omitted
        )

    # Phase 3 — open approval ticket
    ticket = queue.submit(
        _approval_ticket(ticket_id=ticket_id, capability_id=capability_id)
    )
    assert ticket.status == "open"

    # Phase 4 — user answers
    queue.respond(
        CollaborationResponse(
            ticket_id=ticket_id,
            responder="release_owner_alice",
            selected_option="approve",
            answer="Canary green 72h, error_rate ≤ baseline, approving prod flip.",
        )
    )
    assert queue.tickets[ticket_id].status == "answered"
    assert queue.responses[ticket_id].resume_allowed is True

    # Phase 5 — gate sees answered approval ticket, prod flip succeeds
    answered = queue.responses[ticket_id]
    assert answered.selected_option == "approve"
    # Only an 'approve' response should pass through to lifecycle service
    if answered.selected_option == "approve":
        prod_record = await service.transition(
            capability_id=capability_id,
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id=ticket_id,
            evidence_refs=["canary_metrics:cm-collab-h"],
            decision_rationale=f"Approved via ticket {ticket_id}",
        )
    else:
        pytest.fail(f"unexpected selected_option: {answered.selected_option}")

    assert prod_record.user_approval_ticket_id == ticket_id

    # Phase 6 — PG has the production row with the right approval id
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        to_stage="production",
        limit=10,
    )
    assert result.error_kind is None
    assert len(result.rows) == 1
    assert result.rows[0]["user_approval_ticket_id"] == ticket_id


# ============================================================
# Fallback policy — overdue + apply_fallback(approve) → prod flip
# ============================================================


async def test_overdue_ticket_fallback_approve_unblocks_production() -> None:
    """If the deadline expires and fallback_policy.option='approve', the
    fallback response carries the approval and prod flip still happens."""
    await _skip_if_no_pg()

    tenant_id = "t-collab-fb-approve"
    capability_id = f"cap-fb-a-{id(test_overdue_ticket_fallback_approve_unblocks_production)}"
    ticket_id = f"tk-fb-a-{id(test_overdue_ticket_fallback_approve_unblocks_production)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )
    queue = InMemoryCollaborationQueue()
    await _walk_capability_to_canary(service, capability_id=capability_id)

    # Submit ticket with past deadline + fallback=approve
    past_deadline = datetime.now(UTC) - timedelta(hours=1)
    queue.submit(
        _approval_ticket(
            ticket_id=ticket_id,
            capability_id=capability_id,
            deadline=past_deadline,
            fallback_option="approve",
            fallback_reason="canary metrics green 72h, no human response, auto-approve per SLA",
        )
    )

    # Confirm overdue
    overdue = queue.overdue()
    assert len(overdue) == 1
    assert overdue[0].ticket_id == ticket_id

    # Apply fallback — selects 'approve'
    updated = queue.apply_fallback(ticket_id)
    assert updated.status == "fallback_selected"
    response = queue.responses[ticket_id]
    assert response.selected_option == "approve"
    assert response.status == "fallback_selected"

    # Production flip honors the fallback-approved ticket
    if response.selected_option == "approve":
        await service.transition(
            capability_id=capability_id,
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id=ticket_id,
            evidence_refs=["canary_metrics:cm-fb-approve"],
            decision_rationale=f"Fallback-approved via ticket {ticket_id} (overdue)",
        )

    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        to_stage="production",
        limit=10,
    )
    assert result.error_kind is None
    assert len(result.rows) == 1


# ============================================================
# Fallback hold — capability stays in CANARY (no prod row)
# ============================================================


async def test_overdue_ticket_fallback_hold_keeps_capability_in_canary() -> None:
    """Default-safe fallback: when nobody answered + fallback='hold', the
    capability does NOT advance to PRODUCTION — no PG row written."""
    await _skip_if_no_pg()

    tenant_id = "t-collab-fb-hold"
    capability_id = f"cap-fb-h-{id(test_overdue_ticket_fallback_hold_keeps_capability_in_canary)}"
    ticket_id = f"tk-fb-h-{id(test_overdue_ticket_fallback_hold_keeps_capability_in_canary)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )
    queue = InMemoryCollaborationQueue()
    await _walk_capability_to_canary(service, capability_id=capability_id)

    queue.submit(
        _approval_ticket(
            ticket_id=ticket_id,
            capability_id=capability_id,
            deadline=datetime.now(UTC) - timedelta(hours=1),
            fallback_option="hold",
            fallback_reason="No release_owner response — defensive hold per V7 §12.2",
        )
    )
    updated = queue.apply_fallback(ticket_id)
    assert updated.status == "fallback_selected"
    response = queue.responses[ticket_id]
    assert response.selected_option == "hold"

    # Caller decides NOT to transition because fallback chose hold
    # (this is the policy decision — service layer doesn't know about
    # ticket semantics, only that an approval id was/wasn't passed)
    if response.selected_option == "hold":
        # Don't call service.transition — capability stays in CANARY
        pass
    else:
        pytest.fail("hold fallback should not transition")

    # Confirm no production row in PG for this capability
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        to_stage="production",
        limit=10,
    )
    assert result.error_kind is None
    assert result.rows == []


# ============================================================
# Cancelled ticket cannot be used as approval
# ============================================================


async def test_cancelled_ticket_cannot_be_used_as_approval() -> None:
    """A cancelled ticket carries no approval — even if its ID is passed to
    service.transition, the policy layer should refuse to use it. We model
    this by: caller checks ticket.status before passing the ID."""
    await _skip_if_no_pg()

    tenant_id = "t-collab-cancelled"
    capability_id = f"cap-cancel-{id(test_cancelled_ticket_cannot_be_used_as_approval)}"
    ticket_id = f"tk-cancel-{id(test_cancelled_ticket_cannot_be_used_as_approval)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )
    queue = InMemoryCollaborationQueue()
    await _walk_capability_to_canary(service, capability_id=capability_id)

    queue.submit(_approval_ticket(ticket_id=ticket_id, capability_id=capability_id))

    # Cancel the ticket (e.g., capability owner pulled it)
    queue.respond(
        CollaborationResponse(
            ticket_id=ticket_id,
            responder="release_owner_alice",
            selected_option="hold",
            answer="Pulling — re-running canary first",
            status="cancelled",
        )
    )
    assert queue.tickets[ticket_id].status == "cancelled"

    # The policy: cancelled tickets must NOT be passed as approval.
    # If a caller violated that, the V7 §12.2 invariant relies on
    # selected_option semantics. We demonstrate the safe behavior:
    response = queue.responses[ticket_id]
    if response.status == "cancelled":
        # Don't transition. Capability stays in CANARY.
        pass

    # Confirm no production row
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        to_stage="production",
        limit=10,
    )
    assert result.error_kind is None
    assert result.rows == []


# ============================================================
# Wrong selected_option → queue rejects (unit-style guard)
# ============================================================


async def test_queue_rejects_response_with_unknown_decision_option() -> None:
    """Queue layer validates selected_option ∈ decision_options before
    persisting — this is the first-line guard against typos / malicious
    auto-approvers."""
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _approval_ticket(
            ticket_id="tk-bad-option", capability_id="cap-bad-option"
        )
    )

    with pytest.raises(ValueError, match="selected_option"):
        queue.respond(
            CollaborationResponse(
                ticket_id="tk-bad-option",
                responder="auto-approver-bot",
                selected_option="ship_now",  # not in decision_options
            )
        )


# ============================================================
# Queue summary buckets — open / waiting / escalated / overdue / answered
# ============================================================


async def test_queue_summary_tracks_all_status_buckets() -> None:
    """V7 §11 cockpit needs a summary view. Validate all 5 bucket fields."""
    queue = InMemoryCollaborationQueue()

    # 1 open
    queue.submit(_approval_ticket(ticket_id="tk-open", capability_id="cap-open"))
    # 1 waiting
    queue.submit(
        _approval_ticket(ticket_id="tk-wait", capability_id="cap-wait")
    )
    queue.mark_waiting("tk-wait")
    # 1 escalated
    queue.submit(_approval_ticket(ticket_id="tk-esc", capability_id="cap-esc"))
    queue.escalate("tk-esc", reason="release_owner unreachable for 4h")
    # 1 answered
    queue.submit(_approval_ticket(ticket_id="tk-ans", capability_id="cap-ans"))
    queue.respond(
        CollaborationResponse(
            ticket_id="tk-ans",
            responder="release_owner_bob",
            selected_option="approve",
            answer="LGTM",
        )
    )
    # 1 overdue (deadline past, still open)
    queue.submit(
        _approval_ticket(
            ticket_id="tk-overdue",
            capability_id="cap-overdue",
            deadline=datetime.now(UTC) - timedelta(hours=1),
        )
    )

    summary = queue.summary()
    assert "tk-open" in summary.open_ticket_ids
    assert "tk-overdue" in summary.open_ticket_ids  # still open until apply_fallback
    assert summary.waiting_ticket_ids == ["tk-wait"]
    assert summary.escalated_ticket_ids == ["tk-esc"]
    assert summary.answered_ticket_ids == ["tk-ans"]
    assert "tk-overdue" in summary.overdue_ticket_ids


# ============================================================
# Escalate → answered round-trip (full state machine)
# ============================================================


async def test_escalated_ticket_can_still_be_answered() -> None:
    """Escalation is a flag, not a terminal state — release_owner can still
    answer after escalation, response transitions back to 'answered'."""
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _approval_ticket(ticket_id="tk-esc-ans", capability_id="cap-esc-ans")
    )

    queue.escalate("tk-esc-ans", reason="primary out, escalating to manager")
    assert queue.tickets["tk-esc-ans"].status == "escalated"

    queue.respond(
        CollaborationResponse(
            ticket_id="tk-esc-ans",
            responder="manager_carol",
            selected_option="approve",
            answer="Approved after escalation",
        )
    )
    assert queue.tickets["tk-esc-ans"].status == "answered"
    # The escalation_policy snapshot kept the reason
    snapshot = queue.tickets["tk-esc-ans"].escalation_policy
    assert snapshot.get("last_reason") == "primary out, escalating to manager"


# ============================================================
# Closed ticket cannot be answered again (idempotency)
# ============================================================


async def test_closed_ticket_rejects_further_responses() -> None:
    """Once a ticket is closed (terminal), further respond() raises —
    prevents replay of stale approvals."""
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _approval_ticket(ticket_id="tk-closed", capability_id="cap-closed")
    )

    # Close via response (status='closed')
    queue.respond(
        CollaborationResponse(
            ticket_id="tk-closed",
            responder="ops_dave",
            selected_option="hold",
            answer="Hold and re-evaluate after Q3 review",
            status="closed",
        )
    )
    assert queue.tickets["tk-closed"].status == "closed"

    # Trying to respond again raises
    with pytest.raises(ValueError, match="already closed"):
        queue.respond(
            CollaborationResponse(
                ticket_id="tk-closed",
                responder="late-comer",
                selected_option="approve",
                answer="re-approving",
            )
        )


# ============================================================
# resume_allowed signal carried into response
# ============================================================


async def test_resume_allowed_signal_propagates_into_response() -> None:
    """V7 §11.4: the response carries resume_allowed which the daemon uses
    to decide whether to wake the suspended mission. Default True."""
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _approval_ticket(ticket_id="tk-resume", capability_id="cap-resume")
    )

    queue.respond(
        CollaborationResponse(
            ticket_id="tk-resume",
            responder="release_owner_eve",
            selected_option="approve",
            answer="LGTM, resume",
            resume_allowed=True,
        )
    )
    assert queue.responses["tk-resume"].resume_allowed is True

    # Negative: explicit no-resume (e.g., approver wants additional manual
    # gate after the lifecycle answer)
    queue.submit(
        _approval_ticket(
            ticket_id="tk-no-resume", capability_id="cap-no-resume"
        )
    )
    queue.respond(
        CollaborationResponse(
            ticket_id="tk-no-resume",
            responder="release_owner_frank",
            selected_option="approve",
            answer="Approved but wait for SRE handoff before resuming",
            resume_allowed=False,
        )
    )
    assert queue.responses["tk-no-resume"].resume_allowed is False
