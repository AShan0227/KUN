"""NUO action panel tests."""

from __future__ import annotations

import pytest
from kun.api.nuo.action_panel import _decision_to_status


@pytest.mark.unit
@pytest.mark.parametrize(
    ("decision", "status"),
    [
        ("approve", "approved"),
        ("reject", "rejected"),
        ("cancel", "cancelled"),
    ],
)
def test_decision_to_status(decision, status) -> None:
    assert _decision_to_status(decision) == status
