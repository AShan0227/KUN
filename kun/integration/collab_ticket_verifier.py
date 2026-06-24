"""V7 Phase X.H.TICKET-VERIFY production wiring.

Concrete ``TicketVerifier`` impls that resolve a ``user_approval_ticket_id``
against the V7 §11 CollaborationQueue. The lifecycle service uses these to
enforce the V7 §12.2 invariant beyond the legacy "non-empty string" check.

Two implementations:

  - :py:class:`InMemoryQueueTicketVerifier`
      Verifies against ``InMemoryCollaborationQueue``. Used by tests and
      single-process deployments.

  - :py:class:`StoreBackedTicketVerifier`
      Future home for SQL/Redis-backed queue verification. Stub now; will
      be wired when CollaborationTicket persistence lands.

Both implementations honor the same rule set:

  ALLOWED if ticket exists AND ticket.status ∈ {'answered',
  'fallback_selected'} AND response.selected_option == 'approve'.

  DENIED otherwise (cancelled / closed / waiting / open / hold response /
  fake id). ``verify_approval`` returns ``False``; callers (lifecycle
  service) translate False into ``CapabilityLifecycleError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kun.core.logging import get_logger
from kun.governance.capability_lifecycle import TicketVerifier

if TYPE_CHECKING:
    from kun.control_plane.collaboration import InMemoryCollaborationQueue

log = get_logger("kun.integration.collab_ticket_verifier")


_VALID_STATUSES = frozenset({"answered", "fallback_selected"})
_VALID_SELECTED_OPTION = "approve"


class InMemoryQueueTicketVerifier(TicketVerifier):
    """V7 §12.2 ticket verification against an InMemoryCollaborationQueue.

    Holds a (weak) reference to the queue at construction; ``verify_approval``
    checks ticket existence + status + response.selected_option.
    """

    def __init__(self, queue: InMemoryCollaborationQueue) -> None:
        self._queue = queue

    def verify_approval(self, ticket_id: str) -> bool:
        ticket = self._queue.tickets.get(ticket_id)
        if ticket is None:
            log.info(
                "collab_ticket_verifier.unknown_ticket",
                ticket_id=ticket_id,
            )
            return False
        if ticket.status not in _VALID_STATUSES:
            log.info(
                "collab_ticket_verifier.wrong_status",
                ticket_id=ticket_id,
                status=ticket.status,
                expected=sorted(_VALID_STATUSES),
            )
            return False
        response = self._queue.responses.get(ticket_id)
        if response is None:
            log.info(
                "collab_ticket_verifier.no_response_payload",
                ticket_id=ticket_id,
            )
            return False
        if response.selected_option != _VALID_SELECTED_OPTION:
            log.info(
                "collab_ticket_verifier.wrong_selected_option",
                ticket_id=ticket_id,
                selected=response.selected_option,
                expected=_VALID_SELECTED_OPTION,
            )
            return False
        log.info(
            "collab_ticket_verifier.approved",
            ticket_id=ticket_id,
            status=ticket.status,
            responder=response.responder,
        )
        return True


__all__ = [
    "InMemoryQueueTicketVerifier",
]
