from __future__ import annotations

from types import SimpleNamespace

from kun.ops.state_ledger_repair import (
    build_repaired_state_ledger_entry,
    diff_state_ledger_snapshots,
)


def test_build_repaired_entry_uses_story_and_task_metadata() -> None:
    task = SimpleNamespace(
        user_id="user-1",
        project_id="proj-1",
        task_type="product.ops",
        success_criteria_short="运营产品",
        spec_json={"goal_detail": "把产品持续运营起来"},
        risk_level="medium",
        complexity_score=0.7,
        estimated_cost_usd=1.5,
    )
    story = {
        "task_id": "task-1",
        "status": "paused",
        "current_action": "等待邮件审批",
        "total_cost_usd": 0.42,
        "pending_confirmations": ["act-1"],
        "latest_reason": "真实外发需要确认",
        "risk_flags": ["task.pending_action.blocked"],
        "decision_ticket_ids": ["dt-1"],
        "context_asset_ids": ["mem-1"],
        "skill_refs": ["email_writer"],
        "first_seen_at": "2026-04-30T00:00:00+00:00",
        "last_seen_at": "2026-04-30T00:03:00+00:00",
    }

    entry = build_repaired_state_ledger_entry(
        tenant_id="tenant-1",
        task_id="task-1",
        user_id=None,
        task=task,
        story=story,
    )

    assert entry.status == "paused"
    assert entry.current_goal == "把产品持续运营起来"
    assert entry.pending_confirmations == ["act-1"]
    assert entry.decision_ticket_ids == ["dt-1"]
    assert entry.context_asset_ids == ["mem-1"]
    assert entry.skill_hints == ["email_writer"]
    assert entry.cost_so_far_usd == 0.42


def test_diff_state_ledger_snapshots_only_reports_operational_drift() -> None:
    repaired = build_repaired_state_ledger_entry(
        tenant_id="tenant-1",
        task_id="task-1",
        user_id="user-1",
        task=None,
        story={
            "status": "done",
            "current_action": "任务完成",
            "total_cost_usd": 0.2,
            "pending_confirmations": [],
            "decision_ticket_ids": ["dt-1"],
        },
    )

    diffs = diff_state_ledger_snapshots(
        current={
            "status": "running",
            "current_action": "还在执行",
            "pending_reason": "",
            "cost_so_far_usd": 0.1,
            "pending_confirmations": [],
            "alert_flags": [],
            "decision_ticket_ids": [],
            "context_asset_ids": [],
            "skill_hints": [],
            "title": "标题变化不算运行漂移",
        },
        repaired=repaired,
    )

    assert [item.field for item in diffs] == [
        "status",
        "current_action",
        "cost_so_far_usd",
        "decision_ticket_ids",
    ]
