from __future__ import annotations

from datetime import UTC, datetime

from kun.api import blackboard_data_sources as sources
from kun.core.orm import EventRow


def test_history_item_extracts_nested_decision_ticket() -> None:
    row = _event_row(
        event_type="watchtower.decision_plan.created",
        payload={
            "decision_ticket": {
                "ticket_id": "decision-1",
                "decision_point": "strategy_selected",
                "phase": "watchtower",
                "selected_action": "education:SMART",
                "status": "applied",
                "reason": "命中教育策略包",
            }
        },
    )

    item = sources._state_ledger_history_item_from_event(row)

    assert item["decision_ticket_id"] == "decision-1"
    assert item["decision_point"] == "strategy_selected"
    assert item["phase"] == "watchtower"
    assert item["selected_action"] == "education:SMART"
    assert item["decision_status"] == "applied"
    assert item["reason"] == "命中教育策略包"


def test_history_item_extracts_direct_decision_ticket_payload() -> None:
    row = _event_row(
        event_type="task.route.selected",
        payload={
            "ticket_id": "decision-2",
            "decision_point": "role_model_selected",
            "phase": "routing",
            "selected_action": "rt-default:execution",
            "status": "selected",
            "reason": "TaskRouter selected role_template=rt-default",
        },
    )

    item = sources._state_ledger_history_item_from_event(row)

    assert item["decision_ticket_id"] == "decision-2"
    assert item["decision_point"] == "role_model_selected"
    assert item["phase"] == "routing"
    assert item["selected_action"] == "rt-default:execution"
    assert item["decision_status"] == "selected"


def test_history_item_uses_step_delta_cost_not_accumulated_total() -> None:
    row = _event_row(
        event_type="task.step.completed",
        payload={
            "step_id": 2,
            "cost_delta_usd": 0.03,
            "accumulated_cost_usd": 0.2,
        },
    )

    item = sources._state_ledger_history_item_from_event(row)

    assert item["cost_usd"] == 0.03


def _event_row(
    *,
    event_type: str,
    payload: dict,
    event_id: str = "evt-1",
    task_ref: str = "task-1",
) -> EventRow:
    return EventRow(
        event_id=event_id,
        tenant_id="tenant-1",
        event_type=event_type,
        subject=f"kun.tenant-1.test.{event_type}",
        payload=payload,
        occurred_at=datetime(2026, 4, 30, tzinfo=UTC),
        task_ref=task_ref,
    )
