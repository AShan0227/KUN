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
    _row_to_item,
    _sort_actions_for_anchor,
)
from kun.core.orm import PendingActionRow
from kun.world.gateway import WorldGatewayResult
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
def test_decision_update_can_persist_external_dispatch_confirmation() -> None:
    compiled = _decision_update_stmt(
        tenant_id="u-sylvan",
        action_id="act-1",
        new_status="approved",
        now=datetime.now(UTC),
        reason="looks good",
        external_dispatch_confirmed=True,
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    payload_patch = compiled.params["param_1"]

    assert "pending_actions.payload || " in sql
    assert payload_patch == {
        "decision_reason": "looks good",
        "external_dispatch_confirmed": True,
    }


@pytest.mark.unit
def test_approved_decision_message_discloses_guarded_execution() -> None:
    assert "guarded approval gate" in _decision_message("approved")


def _action(
    action_id: str,
    risk_level: str,
    minute: int,
) -> PendingActionRow:
    return cast(
        PendingActionRow,
        SimpleNamespace(
            action_id=action_id,
            task_ref="task-1",
            action_type="local_file.write",
            target_ref="reports/a.txt",
            status="pending_approval",
            risk_level=risk_level,
            payload={},
            created_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
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


@pytest.mark.unit
def test_row_to_item_embeds_gateway_preview() -> None:
    preview = WorldGatewayResult(
        action_id="a-1",
        gateway_mode="handler_preview",
        capability_status="supported_execute",
        requires_handler=False,
        user_summary="批准后会执行受控动作。",
        next_step="批准前先看 diff。",
        audit={"handler_id": "local_file.write.v1"},
        message="Preview only",
    )

    item = _row_to_item(_action("a-1", "low", 1), preview=preview)

    assert item.gateway_preview is not None
    assert item.gateway_preview["gateway_mode"] == "handler_preview"
    assert item.gateway_preview["user_summary"] == "批准后会执行受控动作。"
    assert item.gateway_preview["next_step"] == "批准前先看 diff。"
    assert item.gateway_preview["audit"]["handler_id"] == "local_file.write.v1"
