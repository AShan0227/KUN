"""Human-acceptance free-text parsing (audit F013).

Substring matching classified "do not accept" / "not approved" / "unacceptable"
as accepted (each contains "accept"/"approve"), wrongly closing the task. A
negated acceptance must never resolve to "accepted".
"""

from __future__ import annotations

import pytest
from kun.control_plane.collaboration import CollaborationResponse
from kun.control_plane.runtime import _acceptance_decision_from_collaboration_response


def _resp(answer: str = "", selected_option: str | None = None) -> CollaborationResponse:
    return CollaborationResponse(
        ticket_id="tk-1",
        responder="human-1",
        answer=answer,
        selected_option=selected_option,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        "I do not accept this delivery",
        "don't accept it yet",
        "cannot accept in current state",
        "this is unacceptable",
        "not acceptable, please redo",
        "the result is not approved",
        "I do not approve",
        "I disapprove of this output",
    ],
)
def test_negated_acceptance_is_never_accepted(answer: str) -> None:
    decision = _acceptance_decision_from_collaboration_response(_resp(answer=answer))
    assert decision == "rework_required"
    assert decision != "accepted"


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer,expected",
    [
        ("I accept this, looks great", "accepted"),
        ("approved, ship it", "accepted"),
        ("partially accepted, minor tweaks", "partial_accepted"),
        ("please reject this", "rejected"),
        ("needs rework before delivery", "rework_required"),
        ("", None),
    ],
)
def test_affirmative_and_other_decisions_preserved(answer: str, expected) -> None:
    assert _acceptance_decision_from_collaboration_response(_resp(answer=answer)) == expected


@pytest.mark.unit
def test_structured_selected_option_still_wins() -> None:
    # Even with a negating free-text answer, an explicit selected_option governs.
    decision = _acceptance_decision_from_collaboration_response(
        _resp(answer="do not accept", selected_option="accepted")
    )
    assert decision == "accepted"
