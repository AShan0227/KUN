from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from kun.engineering.system_coordination import (
    coordination_issues_from_rows,
    summarize_coordination_issues,
)


def test_coordination_flags_approved_action_not_executed() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    issues = coordination_issues_from_rows(
        pending_rows=[
            SimpleNamespace(
                action_id="act-1",
                task_ref="task-1",
                action_type="email.draft",
                status="approved",
                updated_at=now - timedelta(minutes=10),
            )
        ],
        runtime_rows=[],
        control_rows=[],
        now=now,
        stale_after=timedelta(minutes=5),
    )

    assert [item.issue_id for item in issues] == ["approved_action_stale:act-1"]
    assert issues[0].severity == "error"
    assert "执行器" in issues[0].suggested_action


def test_coordination_flags_paused_task_without_visible_gate() -> None:
    issues = coordination_issues_from_rows(
        pending_rows=[],
        runtime_rows=[
            SimpleNamespace(
                task_ref="task-2",
                status="paused",
                blob={"pause_reason": "unknown"},
            )
        ],
        control_rows=[],
    )

    assert [item.issue_id for item in issues] == ["paused_without_gate:task-2"]
    assert issues[0].severity == "warn"


def test_coordination_flags_pending_action_against_disabled_handler() -> None:
    issues = coordination_issues_from_rows(
        pending_rows=[
            SimpleNamespace(
                action_id="act-3",
                task_ref="task-3",
                action_type="email.send",
                status="pending_approval",
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
        runtime_rows=[],
        control_rows=[
            SimpleNamespace(
                action_type="email.send",
                status="disabled",
            )
        ],
    )

    assert [item.issue_id for item in issues] == ["handler_control_pending:act-3"]
    assert issues[0].severity == "error"
    assert summarize_coordination_issues(issues)["error"] == 1
