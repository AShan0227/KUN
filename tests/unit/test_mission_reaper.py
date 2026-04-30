from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.engineering.mission_reaper import MissionReapResult, _stale_mission_tasks_stmt


@pytest.mark.unit
def test_stale_mission_tasks_stmt_locks_and_filters_tenant() -> None:
    stmt = _stale_mission_tasks_stmt(
        tenant_id="tenant-a",
        stale_before=datetime(2026, 4, 30, tzinfo=UTC),
        limit=10,
    )
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "mission_tasks.tenant_id = 'tenant-a'" in sql
    assert "runtime_states.last_updated <" in sql
    assert "FOR UPDATE" in sql


@pytest.mark.unit
def test_mission_reap_result_is_explicit() -> None:
    result = MissionReapResult(
        mission_id="msn-1",
        task_id="task-1",
        action="blocked",
        previous_runtime_status="running",
        resume_attempts=3,
        reason="stale task exceeded max resume attempts (3)",
    )

    assert result.action == "blocked"
    assert "max resume attempts" in result.reason
