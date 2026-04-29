"""Mission control tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kun.core.ids import new_id, parse_kind
from kun.datamodel.mission import (
    MissionBudgetSummary,
    MissionCheckpointSummary,
    MissionExecutionSummary,
    MissionTimeline,
    MissionTimelineEvent,
)
from kun.engineering import mission_control
from kun.engineering.mission_control import (
    _active_missions_for_review_stmt,
    _exhausted_resume_attempts_stmt,
    _mission_ledger_audit_from_summary_timeline,
    _mission_review_from_summary_timeline,
    _mission_timeline_events_stmt,
    _mission_timeline_from_event_rows,
    _resumable_tasks_stmt,
    _review_needs_attention,
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


def test_mission_timeline_events_stmt_claims_mission_and_task_events() -> None:
    sql = str(
        _mission_timeline_events_stmt(
            "tenant-a",
            mission_id="msn-1",
            task_ids=["tk-1", "tk-2"],
            limit=50,
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM events" in sql
    assert "events.tenant_id = 'tenant-a'" in sql
    assert "mission_id" in sql
    assert "'msn-1'" in sql
    assert "events.task_ref IN ('tk-1', 'tk-2')" in sql
    assert "task_id" in sql
    assert "task_ref" in sql


def test_active_missions_for_review_stmt_claims_active_missions() -> None:
    sql = str(
        _active_missions_for_review_stmt("tenant-a", limit=10).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM missions" in sql
    assert "missions.tenant_id = 'tenant-a'" in sql
    assert "missions.status IN ('planned', 'running', 'paused')" in sql
    assert "ORDER BY missions.updated_at" in sql


def test_mission_timeline_from_event_rows_rolls_up_cost_status_and_reason() -> None:
    first = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    second = datetime(2026, 4, 29, 10, 1, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            event_id="evt-2",
            event_type="mission.task.resume_completed",
            subject="kun.tenant-a.mission.mission.task.resume_completed",
            occurred_at=second,
            task_ref="tk-1",
            payload={
                "mission_id": "msn-1",
                "task_id": "tk-1",
                "outcome": {
                    "final_status": "done",
                    "cost_usd_actual": 0.1,
                    "cost_usd_equivalent": 0.25,
                },
            },
        ),
        SimpleNamespace(
            event_id="evt-1",
            event_type="mission.task.blocked",
            subject="kun.tenant-a.mission.mission.task.blocked",
            occurred_at=first,
            task_ref="tk-1",
            payload={
                "mission_id": "msn-1",
                "task_id": "tk-1",
                "status": "blocked",
                "reason": "max_resume_attempts_exhausted",
                "checkpoint": {"resume_attempts": 3},
            },
        ),
    ]

    timeline = _mission_timeline_from_event_rows(
        tenant_id="tenant-a",
        mission_id="msn-1",
        rows=rows,
    )

    assert timeline.event_count == 2
    assert timeline.status_counts == {"blocked": 1, "done": 1}
    assert timeline.recent_reasons == ["max_resume_attempts_exhausted"]
    assert timeline.total_cost_usd_actual == 0.1
    assert timeline.total_cost_usd_equivalent == 0.25
    assert timeline.events[0].event_id == "evt-1"
    assert timeline.events[0].checkpoint == {"resume_attempts": 3}
    assert timeline.events[1].status == "done"


def test_mission_review_rolls_up_risk_flags_and_next_checkpoint() -> None:
    generated_at = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    summary = MissionExecutionSummary(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="paused",
        budget=MissionBudgetSummary(
            budget_cap_usd=10.0,
            spent_actual_usd=6.0,
            spent_equivalent_usd=8.5,
            remaining_equivalent_usd=1.5,
            usage_fraction=0.85,
        ),
        task_status_counts={"blocked": 1, "running": 1},
        checkpoints=[
            MissionCheckpointSummary(
                task_id="tk-1",
                role="primary",
                status="blocked",
                resume_attempts=3,
                checkpoint={"phase": "retry"},
            )
        ],
        updated_at=generated_at,
    )
    timeline = MissionTimeline(
        mission_id="msn-1",
        tenant_id="tenant-a",
        event_count=2,
        recent_reasons=["max_resume_attempts_exhausted"],
    )

    review = _mission_review_from_summary_timeline(
        summary,
        timeline,
        milestone_id="mile-1",
        generated_at=generated_at,
    )

    assert review.risk_flags == [
        "budget_near_cap",
        "blocked_tasks_present",
        "retry_pressure",
    ]
    assert review.next_checkpoint == (
        "Resolve blocked task checkpoints before scheduling another resume."
    )
    assert review.checkpoint["budget"]["spent_equivalent_usd"] == 8.5
    assert review.checkpoint["timeline_event_count"] == 2
    assert _review_needs_attention(review) is True


def test_mission_ledger_audit_detects_unexplained_blocked_task() -> None:
    checked_at = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    summary = MissionExecutionSummary(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="paused",
        budget=MissionBudgetSummary(
            budget_cap_usd=5.0,
            spent_actual_usd=1.0,
            spent_equivalent_usd=1.0,
            remaining_equivalent_usd=4.0,
            usage_fraction=0.2,
        ),
        task_status_counts={"blocked": 1},
        checkpoints=[
            MissionCheckpointSummary(
                task_id="tk-1",
                role="primary",
                status="blocked",
                resume_attempts=3,
            )
        ],
        updated_at=checked_at,
    )
    timeline = MissionTimeline(
        mission_id="msn-1",
        tenant_id="tenant-a",
        event_count=1,
        status_counts={"queued": 1},
        total_cost_usd_equivalent=0.0,
        events=[
            MissionTimelineEvent(
                event_id="evt-1",
                event_type="mission.task.resume_requested",
                occurred_at=checked_at,
                subject="kun.tenant-a.mission.mission.task.resume_requested",
                mission_id="msn-1",
                task_id="tk-1",
                status="queued",
                payload={"mission_id": "msn-1", "task_id": "tk-1", "status": "queued"},
            )
        ],
    )

    audit = _mission_ledger_audit_from_summary_timeline(
        summary,
        timeline,
        checked_at=checked_at,
    )

    assert audit.status == "fail"
    assert audit.issue_count == 5
    assert audit.timeline_event_count == 1
    assert audit.checkpoint_count == 1
    assert {issue.code for issue in audit.issues} == {
        "review_missing",
        "terminal_status_missing_event",
        "terminal_checkpoint_missing",
        "blocked_reason_missing",
        "cost_events_missing",
    }


def test_mission_ledger_audit_passes_when_events_explain_summary() -> None:
    checked_at = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    summary = MissionExecutionSummary(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="running",
        budget=MissionBudgetSummary(
            budget_cap_usd=5.0,
            spent_actual_usd=0.2,
            spent_equivalent_usd=0.25,
            remaining_equivalent_usd=4.75,
            usage_fraction=0.05,
        ),
        task_status_counts={"running": 1},
        checkpoints=[
            MissionCheckpointSummary(
                task_id="tk-1",
                role="primary",
                status="running",
                checkpoint={"phase": "draft"},
            )
        ],
        updated_at=checked_at,
    )
    timeline = MissionTimeline(
        mission_id="msn-1",
        tenant_id="tenant-a",
        event_count=2,
        status_counts={"running": 1},
        total_cost_usd_equivalent=0.25,
        events=[
            MissionTimelineEvent(
                event_id="evt-1",
                event_type="mission.task.orchestrator_started",
                occurred_at=checked_at,
                subject="kun.tenant-a.mission.mission.task.orchestrator_started",
                mission_id="msn-1",
                task_id="tk-1",
                status="running",
                checkpoint={"phase": "draft"},
                payload={"mission_id": "msn-1", "task_id": "tk-1", "status": "running"},
            ),
            MissionTimelineEvent(
                event_id="evt-2",
                event_type="mission.review.recorded",
                occurred_at=checked_at,
                subject="kun.tenant-a.mission.mission.review.recorded",
                mission_id="msn-1",
                cost_usd_equivalent=0.25,
                payload={"mission_id": "msn-1", "cost_usd_equivalent": 0.25},
            ),
        ],
    )

    audit = _mission_ledger_audit_from_summary_timeline(
        summary,
        timeline,
        checked_at=checked_at,
    )

    assert audit.status == "pass"
    assert audit.issue_count == 0
    assert audit.issues == []
    assert audit.review_event_count == 1


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


@pytest.mark.asyncio
async def test_review_mission_writes_event_and_milestone(monkeypatch) -> None:
    now = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    mission = SimpleNamespace(
        mission_id="msn-1",
        tenant_id="tenant-a",
        updated_at=now,
    )
    emitted: list[str] = []
    added_rows: list[object] = []

    summary = MissionExecutionSummary(
        mission_id="msn-1",
        tenant_id="tenant-a",
        status="paused",
        budget=MissionBudgetSummary(
            budget_cap_usd=1.0,
            spent_actual_usd=1.1,
            spent_equivalent_usd=1.2,
            remaining_equivalent_usd=0.0,
            usage_fraction=1.2,
        ),
        task_status_counts={"blocked": 1},
        checkpoints=[
            MissionCheckpointSummary(
                task_id="tk-1",
                role="primary",
                status="blocked",
                resume_attempts=3,
            )
        ],
        updated_at=now,
    )
    timeline = MissionTimeline(
        mission_id="msn-1",
        tenant_id="tenant-a",
        event_count=1,
        recent_reasons=["max_resume_attempts_exhausted"],
    )

    class FakeSequenceResult:
        def scalar_one(self) -> int:
            return 4

    class FakeSession:
        async def get(self, _model, _pk):
            return mission

        async def execute(self, _stmt):
            return FakeSequenceResult()

        def add(self, row) -> None:
            added_rows.append(row)

        async def flush(self) -> None:
            pass

    @asynccontextmanager
    async def fake_session_scope(*, tenant_id: str):
        assert tenant_id == "tenant-a"
        yield FakeSession()

    async def fake_summary(*, tenant_id: str, mission_id: str):
        assert (tenant_id, mission_id) == ("tenant-a", "msn-1")
        return summary

    async def fake_timeline(*, tenant_id: str, mission_id: str, limit: int):
        assert (tenant_id, mission_id, limit) == ("tenant-a", "msn-1", 25)
        return timeline

    async def fake_emit(_session, event) -> None:
        emitted.append(event.event_type)

    monkeypatch.setattr(mission_control, "session_scope", fake_session_scope)
    monkeypatch.setattr(mission_control, "summarize_mission", fake_summary)
    monkeypatch.setattr(mission_control, "get_mission_timeline", fake_timeline)
    monkeypatch.setattr(mission_control, "emit", fake_emit)

    review = await mission_control.review_mission(
        tenant_id="tenant-a",
        mission_id="msn-1",
        timeline_limit=25,
    )

    assert review is not None
    assert review.risk_flags[:2] == ["budget_exceeded", "blocked_tasks_present"]
    assert emitted == ["mission.review.recorded"]
    assert len(added_rows) == 1
    milestone = added_rows[0]
    assert milestone.title == "Mission auto review"
    assert milestone.status == "blocked"
    assert milestone.sequence_no == 5
    assert milestone.checkpoint_json["next_checkpoint"] == (
        "Pause execution and approve more budget or reduce scope."
    )
