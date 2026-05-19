from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.control_plane import MinimalSupervisor, RunRecord, WorkItem

NOW = datetime(2026, 5, 17, 10, 0, tzinfo=UTC)


def _work_item(**overrides: object) -> WorkItem:
    payload: dict[str, object] = {
        "work_item_id": "work-supervised",
        "mission_id": "msn-supervisor",
        "task_plan_version": "v1",
        "type": "execution",
        "owner": "kun",
        "priority": 60,
        "retry_budget": 0,
        "expected_output": "Traceable V6 execution result",
    }
    payload.update(overrides)
    return WorkItem.model_validate(payload)


def test_supervisor_acquires_and_releases_lease_with_run_record() -> None:
    supervisor = MinimalSupervisor(lease_ttl=timedelta(minutes=5))

    acquired = supervisor.acquire_lease(
        _work_item(),
        runner_type="agent",
        runner_identity="agent-a",
        now=NOW,
    )

    assert acquired.work_item.status == "running"
    assert acquired.work_item.lease == acquired.lease.lease_id
    assert acquired.work_item.heartbeat == NOW
    assert acquired.work_item.timeout == NOW + timedelta(minutes=5)
    assert acquired.run.exit_status == "running"
    assert acquired.run.work_item_id == acquired.work_item.work_item_id

    released = supervisor.release_lease(
        acquired.work_item,
        acquired.run,
        lease_id=acquired.lease.lease_id,
        status="done",
        now=NOW + timedelta(minutes=2),
        artifact_manifest_ref="manifest-run",
    )

    assert released.work_item.status == "done"
    assert released.work_item.lease is None
    assert released.work_item.timeout is None
    assert released.work_item.artifact_manifest_ref == "manifest-run"
    assert released.run.exit_status == "succeeded"
    assert released.run.ended_at == NOW + timedelta(minutes=2)
    assert released.run.artifact_manifest_ref == "manifest-run"


def test_supervisor_refreshes_heartbeat_and_rejects_expired_lease() -> None:
    supervisor = MinimalSupervisor(lease_ttl=timedelta(minutes=5))
    acquired = supervisor.acquire_lease(
        _work_item(),
        runner_type="tool",
        runner_identity="tool-a",
        now=NOW,
    )

    refreshed = supervisor.refresh_heartbeat(
        acquired.work_item,
        lease_id=acquired.lease.lease_id,
        now=NOW + timedelta(minutes=3),
    )

    assert refreshed.heartbeat == NOW + timedelta(minutes=3)
    assert refreshed.timeout == NOW + timedelta(minutes=8)

    with pytest.raises(ValueError, match="expired"):
        supervisor.refresh_heartbeat(
            acquired.work_item,
            lease_id=acquired.lease.lease_id,
            now=NOW + timedelta(minutes=6),
        )


def test_supervisor_detects_timed_out_work_items() -> None:
    supervisor = MinimalSupervisor()
    timed_out = _work_item(
        status="running",
        lease="lease-old",
        heartbeat=NOW - timedelta(minutes=8),
        timeout=NOW - timedelta(seconds=1),
    )
    still_live = _work_item(
        work_item_id="work-live",
        status="running",
        lease="lease-live",
        heartbeat=NOW,
        timeout=NOW + timedelta(minutes=1),
    )

    findings = supervisor.detect_timeouts([timed_out, still_live], now=NOW)

    assert [finding.work_item_id for finding in findings] == ["work-supervised"]
    assert findings[0].reason == "timeout_expired"
    assert findings[0].failure_category == "environment_failure"


def test_supervisor_maps_failure_to_recovery_work_item_and_gate() -> None:
    supervisor = MinimalSupervisor()
    plan = supervisor.build_recovery_plan(
        _work_item(status="failed"),
        failure_category="environment_failure",
        task_type="ops_tooling",
        root_cause="lease timed out before completion",
    )

    assert plan.action == "create_recovery_work_item"
    assert plan.failure_recovery.next_action == "needs_repair"
    assert plan.failure_recovery.next_state == "repairing"
    assert plan.recovery_work_item is not None
    assert plan.recovery_work_item.type == "repair"
    assert plan.recovery_work_item.mission_id == "msn-supervisor"
    assert plan.gate_evaluation.failure_category == "environment_failure"
    assert plan.gate_evaluation.next_action == "needs_repair"
    assert plan.gate_evaluation.next_state == "repairing"
    assert plan.gate_evaluation.responsibility_scope == "environment"


def test_supervisor_uses_retry_budget_before_recovery_work_item() -> None:
    supervisor = MinimalSupervisor()
    plan = supervisor.build_recovery_plan(
        _work_item(status="failed", retry_budget=2, lease="lease-done"),
        failure_category="tool_failure",
        task_type="ops_tooling",
    )

    assert plan.action == "retry"
    assert plan.retry_work_item is not None
    assert plan.retry_work_item.work_item_id == "work-supervised"
    assert plan.retry_work_item.status == "queued"
    assert plan.retry_work_item.retry_budget == 1
    assert plan.retry_work_item.lease is None
    assert plan.retry_work_item.heartbeat is None
    assert plan.recovery_work_item is None
    assert plan.failure_recovery.failure_category == "tool_failure"


def test_supervisor_detects_stale_run_records() -> None:
    supervisor = MinimalSupervisor(stale_heartbeat_after=timedelta(minutes=5))
    stale_item = _work_item(
        status="running",
        lease="lease-stale",
        heartbeat=NOW - timedelta(minutes=7),
        timeout=NOW + timedelta(minutes=3),
    )
    stale_run = RunRecord(
        run_id="run-stale",
        work_item_id=stale_item.work_item_id,
        runner_type="agent",
        runner_identity="agent-a",
        started_at=NOW - timedelta(minutes=7),
    )
    closed_item = _work_item(work_item_id="work-closed", status="done")
    open_run_for_closed_item = RunRecord(
        run_id="run-closed-item",
        work_item_id=closed_item.work_item_id,
        runner_type="agent",
        runner_identity="agent-b",
        started_at=NOW - timedelta(minutes=1),
    )

    findings = supervisor.detect_stale_runs(
        work_items={
            stale_item.work_item_id: stale_item,
            closed_item.work_item_id: closed_item,
        },
        runs=[stale_run, open_run_for_closed_item],
        now=NOW,
    )

    assert [(finding.run_id, finding.reason) for finding in findings] == [
        ("run-stale", "heartbeat_stale"),
        ("run-closed-item", "work_item_not_running"),
    ]
