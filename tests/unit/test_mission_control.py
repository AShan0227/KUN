"""Mission control tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kun.core.ids import new_id, parse_kind
from kun.engineering import mission_control
from kun.engineering.mission_control import (
    _exhausted_resume_attempts_stmt,
    _resumable_tasks_stmt,
    _stale_mission_tasks_stmt,
    derive_mission_status,
)
from sqlalchemy.dialects import postgresql


def test_mission_ids_are_parseable() -> None:
    mission_id = new_id("mission")
    milestone_id = new_id("milestone")

    assert mission_id.startswith("msn-")
    assert milestone_id.startswith("mile-")
    assert parse_kind(mission_id) == "mission"
    assert parse_kind(milestone_id) == "milestone"


def test_derive_mission_status_from_task_statuses() -> None:
    assert derive_mission_status([]) is None
    assert derive_mission_status(["done", "done"]) == "done"
    assert derive_mission_status(["queued", "done"]) == "running"
    assert derive_mission_status(["paused", "done"]) == "paused"
    assert derive_mission_status(["failed", "cancelled"]) == "failed"
    assert derive_mission_status(["cancelled", "cancelled"]) == "cancelled"


def test_resumable_tasks_stmt_claims_queued_mission_tasks() -> None:
    sql = str(
        _resumable_tasks_stmt("tenant-a", limit=5, max_attempts=3).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM mission_tasks" in sql
    assert "JOIN runtime_states" in sql
    assert "JOIN missions" in sql
    assert "runtime_states.status = 'queued'" in sql
    assert "mission_tasks.status IN ('planned', 'queued', 'running', 'paused', 'blocked')" in sql
    assert "mission_tasks.resume_attempts < 3" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_exhausted_resume_attempts_stmt_claims_tasks_at_max_attempts() -> None:
    sql = str(
        _exhausted_resume_attempts_stmt("tenant-a", max_attempts=3, limit=5).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM mission_tasks" in sql
    assert "JOIN runtime_states" in sql
    assert "runtime_states.status = 'queued'" in sql
    assert "mission_tasks.status IN ('planned', 'queued', 'running', 'paused')" in sql
    assert "mission_tasks.resume_attempts >= 3" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_stale_mission_tasks_stmt_claims_stuck_queued_and_running_tasks() -> None:
    sql = str(
        _stale_mission_tasks_stmt(
            "tenant-a",
            queued_stale_after_sec=900,
            running_stale_after_sec=3600,
            limit=10,
            now=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM mission_tasks" in sql
    assert "JOIN runtime_states" in sql
    assert "mission_tasks.status IN ('queued', 'running')" in sql
    assert "runtime_states.status = 'queued'" in sql
    assert "runtime_states.status = 'running'" in sql
    assert "runtime_states.last_updated < '2026-04-29 09:45:00+00:00'" in sql
    assert "runtime_states.last_updated < '2026-04-29 09:00:00+00:00'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.asyncio
async def test_block_exhausted_mission_tasks_writes_blocked_event_and_checkpoint(
    monkeypatch,
) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    emitted: list[str] = []
    mission = SimpleNamespace(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="running",
        updated_at=now,
        started_at=None,
        finished_at=None,
    )
    mission_task = SimpleNamespace(
        mission_id="msn-1",
        task_id="tk-1",
        status="queued",
        checkpoint_json={"phase": "retry"},
        resume_attempts=3,
        updated_at=now,
    )
    runtime = SimpleNamespace(
        status="queued",
        blob={},
        last_updated=now,
    )

    class FakeRows:
        def all(self):
            return [(mission_task, runtime)]

    class FakeScalars:
        def all(self):
            return ["blocked"]

    class FakeStatuses:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        def __init__(self) -> None:
            self.execute_count = 0
            self.flushed = False

        async def execute(self, _stmt):
            self.execute_count += 1
            if self.execute_count == 1:
                return FakeRows()
            return FakeStatuses()

        async def get(self, _model, _pk):
            return mission

        async def flush(self) -> None:
            self.flushed = True

    session = FakeSession()

    @asynccontextmanager
    async def fake_session_scope(*, tenant_id: str):
        assert tenant_id == "tenant-a"
        yield session

    async def fake_emit(_session, event) -> None:
        emitted.append(event.event_type)

    monkeypatch.setattr(mission_control, "session_scope", fake_session_scope)
    monkeypatch.setattr(mission_control, "emit", fake_emit)

    results = await mission_control.block_exhausted_mission_tasks(
        tenant_id="tenant-a",
        max_attempts=3,
        limit=5,
    )

    assert results[0].status == "blocked"
    assert mission_task.status == "blocked"
    assert runtime.status == "paused"
    assert mission.status == "paused"
    assert session.flushed is True
    assert emitted == ["mission.task.blocked"]
    assert mission_task.checkpoint_json["last_blocked"]["reason"] == (
        "max_resume_attempts_exhausted"
    )
    assert runtime.blob["mission_blocked"]["max_attempts"] == 3


@pytest.mark.asyncio
async def test_summarize_mission_rolls_up_budget_and_checkpoints(monkeypatch) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    mission = SimpleNamespace(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="running",
        budget_cap_usd=10.0,
        updated_at=now,
    )
    mission_task_a = SimpleNamespace(
        task_id="tk-1",
        role="primary",
        status="done",
        checkpoint_json={"phase": "draft"},
        resume_attempts=2,
        last_resume_requested_at=now,
    )
    runtime_a = SimpleNamespace(
        status="done",
        blob={"runner": "ok"},
        last_updated=now,
        accumulated_cost_usd_actual=9.0,
        accumulated_cost_usd_equivalent=9.0,
    )
    result_a = SimpleNamespace(cost_usd_actual=1.25, cost_usd_equivalent=2.5)
    mission_task_b = SimpleNamespace(
        task_id="tk-2",
        role="followup",
        status="running",
        checkpoint_json={},
        resume_attempts=1,
        last_resume_requested_at=None,
    )
    runtime_b = SimpleNamespace(
        status="running",
        blob={},
        last_updated=now,
        accumulated_cost_usd_actual=0.5,
        accumulated_cost_usd_equivalent=0.75,
    )

    class FakeResult:
        def all(self):
            return [
                (mission_task_a, runtime_a, result_a),
                (mission_task_b, runtime_b, None),
            ]

    class FakeSession:
        async def get(self, _model, _pk):
            return mission

        async def execute(self, _stmt):
            return FakeResult()

    @asynccontextmanager
    async def fake_session_scope(*, tenant_id: str):
        assert tenant_id == "tenant-a"
        yield FakeSession()

    monkeypatch.setattr(mission_control, "session_scope", fake_session_scope)

    summary = await mission_control.summarize_mission(
        tenant_id="tenant-a",
        mission_id="msn-1",
    )

    assert summary is not None
    assert summary.task_status_counts == {"done": 1, "running": 1}
    assert summary.budget.spent_actual_usd == 1.75
    assert summary.budget.spent_equivalent_usd == 3.25
    assert summary.budget.remaining_equivalent_usd == 6.75
    assert summary.budget.usage_fraction == 0.325
    assert summary.checkpoints[0].checkpoint == {"phase": "draft", "runtime": {"runner": "ok"}}
    assert summary.checkpoints[1].cost_usd_equivalent == 0.75
