"""NUO action panel tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from kun.api.nuo.action_panel import (
    _decision_message,
    _decision_to_status,
    _decision_update_stmt,
    _page_actions_anchor,
    _sort_actions_for_anchor,
)
from kun.core.orm import PendingActionRow
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
    assert "side-effect executor" in _decision_message("approved")


def _action(
    action_id: str,
    risk_level: str,
    minute: int,
) -> PendingActionRow:
    return cast(
        PendingActionRow,
        SimpleNamespace(
            action_id=action_id,
            risk_level=risk_level,
            created_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        ),
    )


@pytest.mark.unit
def test_sort_actions_for_anchor_risk_first_stable_by_time() -> None:
    rows = [
        _action("a-low", "low", 1),
        _action("a-high-late", "high", 3),
        _action("a-critical", "critical", 2),
        _action("a-high-early", "high", 1),
        _action("a-medium", "medium", 0),
    ]

    sorted_rows = _sort_actions_for_anchor(rows)

    assert [row.action_id for row in sorted_rows] == [
        "a-critical",
        "a-high-early",
        "a-high-late",
        "a-medium",
        "a-low",
    ]


@pytest.mark.unit
def test_page_actions_anchor_first_page_returns_top_three() -> None:
    rows = [_action(f"a-{i}", "medium", i) for i in range(5)]

    page, next_cursor, remaining, round_no = _page_actions_anchor(
        rows,
        limit=3,
        expand_after=None,
        max_rounds=3,
    )

    assert [row.action_id for row in page] == ["a-0", "a-1", "a-2"]
    assert next_cursor == "a-2"
    assert remaining == 2
    assert round_no == 1


@pytest.mark.unit
def test_page_actions_anchor_expand_after_returns_next_three() -> None:
    rows = [_action(f"a-{i}", "medium", i) for i in range(8)]

    page, next_cursor, remaining, round_no = _page_actions_anchor(
        rows,
        limit=3,
        expand_after="a-2",
        max_rounds=3,
    )

    assert [row.action_id for row in page] == ["a-3", "a-4", "a-5"]
    assert next_cursor == "a-5"
    assert remaining == 2
    assert round_no == 2


@pytest.mark.unit
def test_page_actions_anchor_caps_has_more_at_max_rounds() -> None:
    rows = [_action(f"a-{i}", "medium", i) for i in range(12)]

    page, next_cursor, remaining, round_no = _page_actions_anchor(
        rows,
        limit=3,
        expand_after="a-5",
        max_rounds=3,
    )

    assert [row.action_id for row in page] == ["a-6", "a-7", "a-8"]
    assert next_cursor == "a-8"
    assert remaining == 3
    assert round_no == 3


@pytest.mark.unit
def test_page_actions_anchor_rejects_unknown_cursor() -> None:
    rows = [_action("a-1", "medium", 1)]

    with pytest.raises(ValueError, match="expand_after action not found"):
        _page_actions_anchor(
            rows,
            limit=3,
            expand_after="missing",
            max_rounds=3,
        )
