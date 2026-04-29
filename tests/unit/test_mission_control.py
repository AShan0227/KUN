"""Mission control tests."""

from __future__ import annotations

from kun.core.ids import new_id, parse_kind
from kun.engineering.mission_control import _resumable_tasks_stmt, derive_mission_status
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
