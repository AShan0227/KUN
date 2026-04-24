"""NUO action panel tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.api.nuo.action_panel import _decision_message, _decision_to_status, _decision_update_stmt
from sqlalchemy.dialects import postgresql


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


@pytest.mark.unit
def test_decision_update_is_atomic_pending_only() -> None:
    sql = str(
        _decision_update_stmt(
            tenant_id="u-sylvan",
            action_id="act-1",
            new_status="approved",
            now=datetime.now(UTC),
        ).compile(dialect=postgresql.dialect())
    )

    assert "UPDATE pending_actions" in sql
    assert "pending_actions.status = " in sql
    assert "RETURNING pending_actions.action_id" in sql


@pytest.mark.unit
def test_approved_decision_message_discloses_executor_gap() -> None:
    assert "waiting for the side-effect executor" in _decision_message("approved")
