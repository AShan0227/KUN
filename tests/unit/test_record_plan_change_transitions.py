"""record_plan_change status transitions go through the state machine (audit F143).

record_plan_change used to write mission.status directly without
assert_transition_allowed (unlike every other transition site). It now validates,
so the (source → target) pairs it relies on must remain allowed; this guard test
locks that contract so a future change to _ALLOWED_TRANSITIONS can't silently make
record_plan_change emit an illegal transition.
"""

from __future__ import annotations

import pytest
from kun.control_plane.v6 import assert_transition_allowed


@pytest.mark.unit
@pytest.mark.parametrize(
    "source,target",
    [
        ("delivering", "changing_plan"),
        ("awaiting_acceptance", "changing_plan"),
        ("waiting_human", "queued"),
        ("waiting_external", "queued"),
        ("paused", "queued"),
    ],
)
def test_record_plan_change_transitions_remain_allowed(source: str, target: str) -> None:
    # Must not raise — these are exactly the transitions record_plan_change applies.
    assert_transition_allowed(source, target)


@pytest.mark.unit
def test_assert_transition_rejects_an_illegal_transition() -> None:
    # Sanity that the guard actually guards (delivering cannot jump straight to queued).
    with pytest.raises(Exception):
        assert_transition_allowed("delivering", "queued")
