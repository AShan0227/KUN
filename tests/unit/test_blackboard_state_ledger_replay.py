from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from kun.api import blackboard_data_sources as sources
from kun.core.orm import EventRow, TaskRow
from kun.core.state_ledger import reset_state_ledger


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


@pytest.mark.asyncio
async def test_state_ledger_main_source_replays_events_when_hot_and_runtime_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_state_ledger()
    task = _task_row(user_id="u-1")
    events = [
        _event_row(
            event_id="evt-started",
            event_type="task.started",
            payload={"reason": "started from durable event"},
        ),
        _event_row(
            event_id="evt-ticket",
            event_type="task.route.selected",
            payload={
                "ticket_id": "decision-3",
                "decision_point": "llm_model_selected",
                "phase": "routing",
                "selected_action": "openai:gpt-5:balanced",
                "status": "selected",
                "cost_delta_usd": 0.04,
            },
        ),
    ]
    fake_session = _FakeSession(replay_rows=[(event, task) for event in events])

    monkeypatch.setattr(sources, "session_scope", lambda **_: _FakeScope(fake_session))

    result = await sources._state_ledger_source_async(
        tenant_id="tenant-1",
        user_id="u-1",
        task_id=None,
    )

    assert isinstance(result, list)
    assert result[0]["task_id"] == "task-1"
    assert result[0]["status"] == "running"
    assert result[0]["decision_ticket_ids"] == ["decision-3"]
    assert result[0]["cost_so_far_usd"] == 0.04
    assert fake_session.saw_user_filter


@pytest.mark.asyncio
async def test_replayed_story_does_not_sum_accumulated_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_state_ledger()
    task = _task_row(user_id="u-1")
    fake_session = _FakeSession(
        replay_rows=[
            (
                _event_row(
                    event_id="evt-done",
                    event_type="task.done",
                    payload={"accumulated_cost_usd": 10.0},
                ),
                task,
            )
        ]
    )

    monkeypatch.setattr(sources, "session_scope", lambda **_: _FakeScope(fake_session))

    result = await sources._state_ledger_source_async(
        tenant_id="tenant-1",
        user_id="u-1",
        task_id="task-1",
    )

    assert isinstance(result, dict)
    assert result["status"] == "done"
    assert result["cost_so_far_usd"] == 0.0


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


def _task_row(*, user_id: str) -> TaskRow:
    return TaskRow(
        task_id="task-1",
        tenant_id="tenant-1",
        fingerprint=f"fp-{user_id}",
        task_type="analysis",
        risk_level="low",
        complexity_score=0.3,
        user_id=user_id,
        project_id="project-1",
        estimated_cost_usd=1.0,
        estimated_duration_sec=30.0,
        success_criteria_short="Replay this task",
        spec_json={"goal": "Replay durable events"},
        created_at=datetime(2026, 4, 30, tzinfo=UTC),
    )


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, *, replay_rows: list[Any]) -> None:
        self._replay_rows = replay_rows
        self.saw_user_filter = False

    async def execute(self, stmt: Any) -> _FakeResult:
        sql = str(stmt)
        if "tasks.user_id" in sql:
            self.saw_user_filter = True
        if "runtime_states" in sql:
            return _FakeResult([])
        return _FakeResult(self._replay_rows)


class _FakeScope:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None
