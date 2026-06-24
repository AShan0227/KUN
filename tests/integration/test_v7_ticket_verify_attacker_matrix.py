"""V7 Phase X.H.TICKET-VERIFY — attacker matrix.

Before X.H, ``CapabilityLifecycleService.validate_transition`` checked
``user_approval_ticket_id`` only for truthiness (non-empty string). Any
caller — including a buggy admin script or an attacker who got hold of
the service — could pass ANY string and successfully transition
CANARY → PRODUCTION.

The self-audit on 2026-05-29 confirmed this honor-system gap. This file
is the attacker matrix that proves the X.H verifier closes it.

The matrix exercises each input the legacy code silently accepted:

  1. Wholly fake ticket id (never submitted to queue) → DENIED
  2. Real ticket but status='open' (no response yet) → DENIED
  3. Real ticket but status='waiting' → DENIED
  4. Real ticket but status='escalated' → DENIED
  5. Real ticket but status='cancelled' (was 'answered' first, then
     cancelled) → DENIED
  6. Real ticket but status='closed' (no answer) → DENIED
  7. Real ticket, answered, but selected_option='hold' (not approve)
     → DENIED
  8. Real ticket, fallback_selected with fallback option='approve'
     → ALLOWED (V7 §11.5 SLA fallback chain)
  9. Real ticket, fallback_selected with fallback option='hold' → DENIED
  10. Real ticket, properly answered with selected_option='approve'
      → ALLOWED (happy path)
  11. Verifier raises an unexpected exception → service translates to
      CapabilityLifecycleError (fail-closed)

Without a verifier wired, the service falls back to legacy behavior
(only non-empty check) for backward-compat with unit tests that pre-date
X.H. Production callers must wire the verifier; the X.H.PROD-ENTRY-WIRE
bundle integration covers this.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.control_plane.collaboration import (
    CollaborationResponse,
    InMemoryCollaborationQueue,
)
from kun.control_plane.v6 import CollaborationTicket
from kun.governance.capability_lifecycle import (
    CapabilityLifecycleError,
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
    TicketVerifier,
)
from kun.integration.collab_ticket_verifier import (
    InMemoryQueueTicketVerifier,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _make_ticket(
    *,
    ticket_id: str = "tk-attacker-1",
    deadline: datetime | None = None,
    fallback_option: str = "hold",
) -> CollaborationTicket:
    return CollaborationTicket(
        ticket_id=ticket_id,
        mission_id="msn-attacker",
        type="approval",
        role_needed="release_owner",
        why_needed="V7 §12.2 production flip approval",
        decision_options=["approve", "hold"],
        recommended_option="approve",
        context_ref="capability:cap-attacker",
        risk_if_skipped="opportunity cost",
        deadline=deadline or datetime.now(UTC) + timedelta(hours=1),
        fallback_policy={"option": fallback_option, "reason": "test fallback"},
        output_contract="approve|hold + rationale",
    )


def _service_with_verifier(verifier: TicketVerifier | None) -> CapabilityLifecycleService:
    return CapabilityLifecycleService(ticket_verifier=verifier)


# ============================================================
# Attack #1 — wholly fake ticket id
# ============================================================


async def test_attacker_fake_ticket_id_is_rejected() -> None:
    queue = InMemoryCollaborationQueue()
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)

    with pytest.raises(CapabilityLifecycleError, match="approval verification"):
        await service.transition(
            capability_id="cap-fake",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id="tk-this-never-existed",
            evidence_refs=["canary_metrics:cm-x"],
        )


# ============================================================
# Attack #2..#6 — real ticket but wrong status
# ============================================================


@pytest.mark.parametrize(
    "submit_and_mutate",
    [
        # 2. open — ticket exists but no response
        lambda q, tk: q.submit(_make_ticket(ticket_id=tk)),
        # 3. waiting — explicitly mark waiting
        lambda q, tk: (q.submit(_make_ticket(ticket_id=tk)), q.mark_waiting(tk)),
        # 4. escalated
        lambda q, tk: (
            q.submit(_make_ticket(ticket_id=tk)),
            q.escalate(tk, reason="primary out"),
        ),
        # 5. cancelled — answered first with cancelled status
        lambda q, tk: (
            q.submit(_make_ticket(ticket_id=tk)),
            q.respond(
                CollaborationResponse(
                    ticket_id=tk,
                    responder="ops",
                    selected_option="hold",
                    status="cancelled",
                    answer="cancelled",
                )
            ),
        ),
        # 6. closed — explicit closed response
        lambda q, tk: (
            q.submit(_make_ticket(ticket_id=tk)),
            q.respond(
                CollaborationResponse(
                    ticket_id=tk,
                    responder="ops",
                    selected_option="hold",
                    status="closed",
                    answer="closed",
                )
            ),
        ),
    ],
    ids=["open", "waiting", "escalated", "cancelled", "closed"],
)
async def test_attacker_wrong_status_is_rejected(submit_and_mutate) -> None:
    queue = InMemoryCollaborationQueue()
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)
    ticket_id = "tk-attacker-status"
    submit_and_mutate(queue, ticket_id)

    with pytest.raises(CapabilityLifecycleError, match="approval verification"):
        await service.transition(
            capability_id="cap-status-attack",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id=ticket_id,
            evidence_refs=["canary_metrics:cm-x"],
        )


# ============================================================
# Attack #7 — answered but selected_option != 'approve'
# ============================================================


async def test_attacker_answered_but_selected_hold_is_rejected() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(_make_ticket(ticket_id="tk-hold-answer"))
    queue.respond(
        CollaborationResponse(
            ticket_id="tk-hold-answer",
            responder="release_owner",
            selected_option="hold",  # not approve
            answer="we should hold and re-evaluate",
        )
    )
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)

    with pytest.raises(CapabilityLifecycleError, match="approval verification"):
        await service.transition(
            capability_id="cap-hold-attack",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id="tk-hold-answer",
            evidence_refs=["canary_metrics:cm-x"],
        )


# ============================================================
# Allow #8 — SLA fallback approve
# ============================================================


async def test_fallback_selected_approve_is_allowed() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _make_ticket(
            ticket_id="tk-fb-approve",
            deadline=datetime.now(UTC) - timedelta(hours=1),
            fallback_option="approve",
        )
    )
    queue.apply_fallback("tk-fb-approve")
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)

    record = await service.transition(
        capability_id="cap-fb-approve",
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-fb-approve",
        evidence_refs=["canary_metrics:cm-x"],
    )
    assert record.to_stage == CapabilityLifecycleStage.PRODUCTION
    assert record.user_approval_ticket_id == "tk-fb-approve"


# ============================================================
# Reject #9 — SLA fallback hold
# ============================================================


async def test_fallback_selected_hold_is_rejected() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(
        _make_ticket(
            ticket_id="tk-fb-hold",
            deadline=datetime.now(UTC) - timedelta(hours=1),
            fallback_option="hold",
        )
    )
    queue.apply_fallback("tk-fb-hold")
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)

    with pytest.raises(CapabilityLifecycleError, match="approval verification"):
        await service.transition(
            capability_id="cap-fb-hold",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id="tk-fb-hold",
            evidence_refs=["canary_metrics:cm-x"],
        )


# ============================================================
# Allow #10 — happy path
# ============================================================


async def test_genuinely_answered_approval_is_allowed() -> None:
    queue = InMemoryCollaborationQueue()
    queue.submit(_make_ticket(ticket_id="tk-happy"))
    queue.respond(
        CollaborationResponse(
            ticket_id="tk-happy",
            responder="release_owner_alice",
            selected_option="approve",
            answer="canary green 72h, approving prod flip",
        )
    )
    verifier = InMemoryQueueTicketVerifier(queue)
    service = _service_with_verifier(verifier)

    record = await service.transition(
        capability_id="cap-happy",
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-happy",
        evidence_refs=["canary_metrics:cm-x"],
    )
    assert record.user_approval_ticket_id == "tk-happy"


# ============================================================
# Verifier exception → fail-closed
# ============================================================


class _ExplodingVerifier(TicketVerifier):
    def verify_approval(self, ticket_id: str) -> bool:
        raise RuntimeError(f"upstream queue unreachable for {ticket_id}")


async def test_verifier_exception_fails_closed() -> None:
    service = _service_with_verifier(_ExplodingVerifier())
    with pytest.raises(CapabilityLifecycleError, match="verification failed"):
        await service.transition(
            capability_id="cap-explode",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            user_approval_ticket_id="tk-explode",
            evidence_refs=["canary_metrics:cm-x"],
        )


# ============================================================
# Backward compat — no verifier wired (legacy behavior)
# ============================================================


async def test_no_verifier_falls_back_to_legacy_non_empty_check() -> None:
    """Pre-X.H behavior preserved when verifier is None: only the
    non-empty check runs. This is intentional backward-compat — the
    bundle in X.H.PROD-ENTRY-WIRE is what guarantees production callers
    wire the verifier. Tests that pre-date X.H still pass."""
    service = _service_with_verifier(None)
    # Pre-X.H this would silently succeed; we preserve that contract for
    # legacy callers.
    record = await service.transition(
        capability_id="cap-legacy",
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-anything-goes-pre-x.h",
        evidence_refs=["canary_metrics:cm-x"],
    )
    assert record.user_approval_ticket_id == "tk-anything-goes-pre-x.h"
