"""Mission control tests."""

from __future__ import annotations

from datetime import UTC, datetime

from kun.core.ids import new_id, parse_kind
from kun.datamodel.mission import MissionSnapshot, MissionTaskLink
from kun.engineering.mission_control import (
    _build_mission_story,
    _resumable_tasks_stmt,
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


def test_build_mission_story_rolls_up_task_histories() -> None:
    now = datetime.now(UTC)
    snapshot = MissionSnapshot(
        mission_id="msn-1",
        tenant_id="tenant-a",
        title="运营产品",
        objective="持续拿到真实用户反馈",
        status="running",
        risk_level="medium",
        budget_used_usd=0.07,
        budget_cap_usd=1.0,
        tasks=[
            MissionTaskLink(
                task_id="task-1",
                role="research",
                status="done",
                resume_attempts=1,
            ),
            MissionTaskLink(
                task_id="task-2",
                role="delivery",
                status="queued",
                resume_attempts=0,
            ),
        ],
        created_at=now,
        updated_at=now,
    )
    histories = {
        "task-1": [
            {
                "event_id": "evt-1",
                "event_type": "decision.ticket",
                "occurred_at": now.isoformat(),
                "task_id": "task-1",
                "summary": "选了调研路径",
                "reason": "历史成功率更高",
                "cost_usd": 0.02,
                "decision_ticket_id": "dt-1",
                "decision_point": "strategy_selected",
                "phase": "plan",
                "selected_action": "research",
                "decision_status": "accepted",
                "payload": {},
            },
            {
                "event_id": "evt-2",
                "event_type": "task.done",
                "occurred_at": now.isoformat(),
                "task_id": "task-1",
                "summary": "任务完成",
                "reason": "已找到用户访谈线索",
                "cost_usd": 0.03,
                "payload": {},
            },
        ],
        "task-2": [],
    }

    story = _build_mission_story(
        snapshot,
        histories=histories,
        mission_events=[],
        history_limit_per_task=100,
    )

    assert story.task_count == 2
    assert story.done_task_count == 1
    assert story.event_count == 2
    assert story.decision_count == 1
    assert story.total_event_cost_usd == 0.05
    assert story.latest_reason in {"历史成功率更高", "已找到用户访谈线索"}
    assert story.tasks[0].task_id == "task-1"
