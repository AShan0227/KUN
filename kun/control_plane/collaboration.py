"""Human and external-collaborator ticket queue for KUN V6."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import CollaborationTicket

TicketResolutionStatus = Literal["answered", "fallback_selected", "cancelled", "closed"]


def _now() -> datetime:
    return datetime.now(UTC)


class CollaborationResponse(BaseModel):
    """A recorded answer or fallback for one collaboration ticket."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    responder: str
    selected_option: str | None = None
    answer: str = ""
    status: TicketResolutionStatus = "answered"
    received_at: datetime = Field(default_factory=_now)
    resume_allowed: bool = True


class CollaborationQueueSummary(BaseModel):
    """Compact view consumed by progress reports and UIs."""

    model_config = ConfigDict(extra="forbid")

    open_ticket_ids: list[str] = Field(default_factory=list)
    waiting_ticket_ids: list[str] = Field(default_factory=list)
    escalated_ticket_ids: list[str] = Field(default_factory=list)
    overdue_ticket_ids: list[str] = Field(default_factory=list)
    answered_ticket_ids: list[str] = Field(default_factory=list)


class InMemoryCollaborationQueue:
    """Deterministic collaboration queue with SLA and fallback semantics."""

    def __init__(self) -> None:
        self.tickets: dict[str, CollaborationTicket] = {}
        self.responses: dict[str, CollaborationResponse] = {}

    def submit(self, ticket: CollaborationTicket) -> CollaborationTicket:
        if ticket.ticket_id in self.tickets:
            raise ValueError(f"collaboration ticket already exists: {ticket.ticket_id}")
        self.tickets[ticket.ticket_id] = ticket
        return ticket

    def mark_waiting(self, ticket_id: str) -> CollaborationTicket:
        ticket = self._ticket(ticket_id)
        updated = ticket.model_copy(update={"status": "waiting"})
        self.tickets[ticket_id] = updated
        return updated

    def escalate(self, ticket_id: str, *, reason: str) -> CollaborationTicket:
        if not reason.strip():
            raise ValueError("escalation reason is required")
        ticket = self._ticket(ticket_id)
        updated_policy = {**ticket.escalation_policy, "last_reason": reason}
        updated = ticket.model_copy(
            update={"status": "escalated", "escalation_policy": updated_policy}
        )
        self.tickets[ticket_id] = updated
        return updated

    def respond(self, response: CollaborationResponse) -> CollaborationTicket:
        ticket = self._ticket(response.ticket_id)
        if ticket.status in {"cancelled", "closed"}:
            raise ValueError(f"ticket {ticket.ticket_id} is already {ticket.status}")
        if (
            response.selected_option
            and ticket.decision_options
            and response.selected_option not in ticket.decision_options
        ):
            raise ValueError("selected_option is not in decision_options")
        if response.status == "answered" and not (response.answer or response.selected_option):
            raise ValueError("answered collaboration response needs answer or selected_option")
        self.responses[response.ticket_id] = response
        updated = ticket.model_copy(update={"status": response.status})
        self.tickets[response.ticket_id] = updated
        return updated

    def apply_fallback(self, ticket_id: str, *, responder: str = "control-plane") -> CollaborationTicket:
        ticket = self._ticket(ticket_id)
        fallback_option = str(
            ticket.fallback_policy.get("option")
            or ticket.recommended_option
            or "fallback"
        )
        return self.respond(
            CollaborationResponse(
                ticket_id=ticket_id,
                responder=responder,
                selected_option=fallback_option,
                status="fallback_selected",
                answer=str(ticket.fallback_policy.get("reason") or "fallback selected"),
                resume_allowed=ticket.resume_after_response,
            )
        )

    def overdue(self, *, now: datetime | None = None) -> list[CollaborationTicket]:
        active_now = now or _now()
        return [
            ticket
            for ticket in self.tickets.values()
            if ticket.status in {"open", "waiting", "escalated"} and ticket.deadline < active_now
        ]

    def summary(self, *, now: datetime | None = None) -> CollaborationQueueSummary:
        overdue_ids = {ticket.ticket_id for ticket in self.overdue(now=now)}
        return CollaborationQueueSummary(
            open_ticket_ids=sorted(
                ticket.ticket_id for ticket in self.tickets.values() if ticket.status == "open"
            ),
            waiting_ticket_ids=sorted(
                ticket.ticket_id for ticket in self.tickets.values() if ticket.status == "waiting"
            ),
            escalated_ticket_ids=sorted(
                ticket.ticket_id for ticket in self.tickets.values() if ticket.status == "escalated"
            ),
            overdue_ticket_ids=sorted(overdue_ids),
            answered_ticket_ids=sorted(self.responses),
        )

    def _ticket(self, ticket_id: str) -> CollaborationTicket:
        try:
            return self.tickets[ticket_id]
        except KeyError as exc:
            raise ValueError(f"unknown collaboration ticket {ticket_id}") from exc
