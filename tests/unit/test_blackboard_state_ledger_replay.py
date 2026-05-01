from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from kun.api import blackboard_data_sources as sources
from kun.core.orm import EventRow, StateLedgerEntryRow, TaskRow
from kun.core.state_ledger import StateLedgerEntry, reset_state_ledger


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


def test_state_ledger_audit_flags_status_and_cost_drift() -> None:
    audit = sources._state_ledger_audit_from_snapshot_and_story(
        task_id="task-1",
        tenant_id="tenant-1",
        snapshot={
            "task_id": "task-1",
            "tenant_id": "tenant-1",
            "status": "running",
            "cost_so_far_usd": 0.25,
            "updated_at": "2026-04-30T00:01:00+00:00",
        },
        story={
            "task_id": "task-1",
            "event_count": 3,
            "decision_count": 1,
            "status": "done",
            "total_cost_usd": 0.1,
            "last_seen_at": "2026-04-30T00:00:00+00:00",
            "reconstruction_confidence": 0.7,
            "gaps": ["missing_task_created_event"],
        },
        snapshot_source="persistent",
    )

    assert audit["snapshot_found"] is True
    assert audit["replay_found"] is True
    assert audit["snapshot_source"] == "persistent"
    assert audit["status_matches"] is False
    assert audit["drift_detected"] is True
    assert audit["issues"] == ["status_drift", "cost_drift", "missing_task_created_event"]
    assert audit["cost_delta_usd"] == 0.15


def test_state_ledger_audit_is_honest_when_history_is_missing() -> None:
    audit = sources._state_ledger_audit_from_snapshot_and_story(
        task_id="task-1",
        tenant_id="tenant-1",
        snapshot={"task_id": "task-1", "status": "paused"},
        story={"task_id": "task-1", "event_count": 0, "status": "unknown"},
        snapshot_source="hot",
    )

    assert audit["snapshot_found"] is True
    assert audit["replay_found"] is False
    assert audit["drift_detected"] is False
    assert audit["issues"] == ["missing_durable_history"]


@pytest.mark.asyncio
async def test_state_ledger_main_source_reads_persistent_snapshot_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_state_ledger()
    entry = StateLedgerEntry(
        task_id="task-1",
        tenant_id="tenant-1",
        user_id="u-1",
        title="Persisted task",
        status="paused",
        pending_reason="loaded from durable ledger",
    )
    fake_session = _FakeSession(persistent_rows=[_state_ledger_row(entry)])

    monkeypatch.setattr(sources, "session_scope", lambda **_: _FakeScope(fake_session))

    result = await sources._state_ledger_source_async(
        tenant_id="tenant-1",
        user_id="u-1",
        task_id="task-1",
    )

    assert isinstance(result, dict)
    assert result["status"] == "paused"
    assert result["pending_reason"] == "loaded from durable ledger"


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
        _event_row(
            event_id="evt-credit",
            event_type="credit.assignment.completed",
            payload={
                "task_outcome": "success",
                "step_count": 1,
                "critical_path_step_ids": [1],
                "total_immediate_reward": 0.9,
                "resource_count": 2,
                "resource_kind_summaries": [
                    {
                        "resource_kind": "skill",
                        "total_delta": 0.5,
                        "mean_delta": 0.5,
                        "positive_count": 1,
                        "negative_count": 0,
                        "resource_count": 1,
                    },
                    {
                        "resource_kind": "context",
                        "total_delta": 0.2,
                        "mean_delta": 0.2,
                        "positive_count": 1,
                        "negative_count": 0,
                        "resource_count": 1,
                    },
                ],
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
    assert result[0]["credit_assignment_count"] == 1
    assert result[0]["top_credit_resource_kinds"] == ["skill", "context"]
    assert result[0]["critical_path_step_ids"] == [1]
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
    payload: dict[str, Any],
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


def _state_ledger_row(entry: StateLedgerEntry) -> StateLedgerEntryRow:
    return StateLedgerEntryRow(
        tenant_id=entry.tenant_id,
        task_id=entry.task_id,
        user_id=entry.user_id,
        project_id=entry.project_id,
        status=entry.status,
        snapshot_json=entry.model_dump(mode="json"),
        created_at=entry.started_at,
        updated_at=entry.updated_at,
    )


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeScalarResult(_FakeResult):
    def scalars(self) -> _FakeScalarResult:
        return self


class _FakeSession:
    def __init__(
        self,
        *,
        replay_rows: list[Any] | None = None,
        persistent_rows: list[Any] | None = None,
    ) -> None:
        self._persistent_rows = persistent_rows or []
        self._replay_rows = replay_rows
        self.saw_user_filter = False

    async def execute(self, stmt: Any) -> _FakeResult:
        sql = str(stmt)
        if "tasks.user_id" in sql:
            self.saw_user_filter = True
        if "state_ledger_entries" in sql:
            return _FakeScalarResult(self._persistent_rows)
        if "runtime_states" in sql:
            return _FakeResult([])
        return _FakeResult(self._replay_rows or [])


class _FakeScope:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None
