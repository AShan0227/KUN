"""Minimal V6 supervisor protocol for leases, heartbeats, and recovery.

This module intentionally does not spawn or manage real processes.  It is the
small deterministic protocol layer that runtime integrations can call around
their worker execution boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from kun.control_plane.v6 import (
    FailureCategory,
    FailureRecovery,
    GateEvaluation,
    RunRecord,
    TaskType,
    WorkItem,
    WorkItemType,
    default_recovery_for_failure,
)

SupervisorRunnerType = Literal["model", "tool", "agent", "command", "human", "external_worker"]
LeaseReleaseStatus = Literal[
    "done",
    "partial",
    "failed",
    "cancelled",
    "blocked",
    "waiting_human",
    "waiting_external",
]
SupervisorFindingReason = Literal[
    "timeout_expired",
    "heartbeat_missing",
    "heartbeat_stale",
    "run_without_work_item",
    "work_item_not_running",
    "lease_missing",
]
SupervisorRecoveryAction = Literal["retry", "create_recovery_work_item"]

_RECOVERY_WORK_ITEM_TYPES: dict[FailureCategory, WorkItemType] = {
    "environment_failure": "repair",
    "permission_failure": "collaboration",
    "tool_failure": "repair",
    "model_quality_failure": "plan_change",
    "evidence_failure": "research",
    "plan_failure": "plan_change",
    "external_dependency_failure": "external_worker",
    "user_input_missing": "collaboration",
    "delivery_failure": "repair",
    "cost_overrun": "plan_change",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _supervisor_id(prefix: str) -> str:
    return f"{prefix}-{ULID()}"


def _responsibility_scope(
    failure_category: FailureCategory,
) -> Literal["kun_auto", "human_collaboration", "external_worker", "environment"]:
    if failure_category in {"environment_failure", "tool_failure"}:
        return "environment"
    if failure_category in {"permission_failure", "user_input_missing"}:
        return "human_collaboration"
    if failure_category == "external_dependency_failure":
        return "external_worker"
    return "kun_auto"


class SupervisorLease(BaseModel):
    """Lease issued to one runner for one V6 work item."""

    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(default_factory=lambda: _supervisor_id("lease"))
    work_item_id: str
    run_id: str
    runner_type: SupervisorRunnerType
    runner_identity: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class LeaseAcquisition(BaseModel):
    """Result of acquiring a work-item lease."""

    model_config = ConfigDict(extra="forbid")

    work_item: WorkItem
    run: RunRecord
    lease: SupervisorLease


class LeaseRelease(BaseModel):
    """Result of releasing a work-item lease."""

    model_config = ConfigDict(extra="forbid")

    work_item: WorkItem
    run: RunRecord


class SupervisorFinding(BaseModel):
    """A stale or timed-out supervisor observation."""

    model_config = ConfigDict(extra="forbid")

    reason: SupervisorFindingReason
    work_item_id: str | None = None
    run_id: str | None = None
    observed_at: datetime
    failure_category: FailureCategory = "environment_failure"
    details: dict[str, str] = Field(default_factory=dict)


class SupervisorRecoveryPlan(BaseModel):
    """Failure category mapped to a retry action or recovery work item."""

    model_config = ConfigDict(extra="forbid")

    failure_category: FailureCategory
    failure_recovery: FailureRecovery
    action: SupervisorRecoveryAction
    gate_evaluation: GateEvaluation
    retry_work_item: WorkItem | None = None
    recovery_work_item: WorkItem | None = None


class MinimalSupervisor:
    """Pure V6 supervisor protocol with no process attachment."""

    def __init__(
        self,
        *,
        lease_ttl: timedelta = timedelta(minutes=5),
        stale_heartbeat_after: timedelta = timedelta(minutes=10),
    ) -> None:
        if lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        if stale_heartbeat_after <= timedelta(0):
            raise ValueError("stale_heartbeat_after must be positive")
        self.lease_ttl = lease_ttl
        self.stale_heartbeat_after = stale_heartbeat_after

    def acquire_lease(
        self,
        work_item: WorkItem,
        *,
        runner_type: SupervisorRunnerType,
        runner_identity: str,
        now: datetime | None = None,
        lease_ttl: timedelta | None = None,
    ) -> LeaseAcquisition:
        """Acquire a lease and create the corresponding running RunRecord."""

        observed_at = now or _now()
        ttl = lease_ttl or self.lease_ttl
        if ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        if work_item.status not in {"queued", "retrying"}:
            raise ValueError(f"cannot acquire lease for work item in status {work_item.status!r}")
        if work_item.lease is not None and (
            work_item.timeout is None or work_item.timeout > observed_at
        ):
            raise ValueError(f"work item {work_item.work_item_id} already has an active lease")

        run = RunRecord(
            work_item_id=work_item.work_item_id,
            runner_type=runner_type,
            runner_identity=runner_identity,
            started_at=observed_at,
        )
        lease = SupervisorLease(
            work_item_id=work_item.work_item_id,
            run_id=run.run_id,
            runner_type=runner_type,
            runner_identity=runner_identity,
            acquired_at=observed_at,
            heartbeat_at=observed_at,
            expires_at=observed_at + ttl,
        )
        leased_item = work_item.model_copy(
            update={
                "status": "running",
                "lease": lease.lease_id,
                "heartbeat": observed_at,
                "timeout": lease.expires_at,
            }
        )
        return LeaseAcquisition(work_item=leased_item, run=run, lease=lease)

    def refresh_heartbeat(
        self,
        work_item: WorkItem,
        *,
        lease_id: str,
        now: datetime | None = None,
        lease_ttl: timedelta | None = None,
    ) -> WorkItem:
        """Refresh heartbeat and extend the lease timeout."""

        observed_at = now or _now()
        ttl = lease_ttl or self.lease_ttl
        if ttl <= timedelta(0):
            raise ValueError("lease_ttl must be positive")
        self._assert_lease_matches(work_item, lease_id)
        if work_item.timeout is not None and work_item.timeout <= observed_at:
            raise ValueError(f"lease {lease_id} expired at {work_item.timeout.isoformat()}")
        return work_item.model_copy(
            update={
                "heartbeat": observed_at,
                "timeout": observed_at + ttl,
            }
        )

    def release_lease(
        self,
        work_item: WorkItem,
        run: RunRecord,
        *,
        lease_id: str,
        status: LeaseReleaseStatus,
        now: datetime | None = None,
        failure_category: FailureCategory | None = None,
        artifact_manifest_ref: str | None = None,
        gate_evaluation_ref: str | None = None,
    ) -> LeaseRelease:
        """Release a lease and close the associated RunRecord."""

        observed_at = now or _now()
        self._assert_lease_matches(work_item, lease_id)
        if run.work_item_id != work_item.work_item_id:
            raise ValueError("run work_item_id does not match leased work item")
        if run.exit_status != "running":
            raise ValueError("only running RunRecord can be released")

        exit_status: Literal["succeeded", "failed", "cancelled"]
        if status in {"done", "partial"}:
            exit_status = "succeeded"
        elif status == "failed":
            exit_status = "failed"
        else:
            exit_status = "cancelled"

        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(),
                "ended_at": observed_at,
                "exit_status": exit_status,
                "failure_category": failure_category,
                "artifact_manifest_ref": artifact_manifest_ref,
                "gate_evaluation_ref": gate_evaluation_ref,
            }
        )
        released_item = work_item.model_copy(
            update={
                "status": status,
                "lease": None,
                "heartbeat": observed_at,
                "timeout": None,
                "artifact_manifest_ref": artifact_manifest_ref
                or work_item.artifact_manifest_ref,
            }
        )
        return LeaseRelease(work_item=released_item, run=updated_run)

    def detect_timeouts(
        self,
        work_items: Iterable[WorkItem],
        *,
        now: datetime | None = None,
    ) -> list[SupervisorFinding]:
        """Return running leased work items whose timeout has expired."""

        observed_at = now or _now()
        findings: list[SupervisorFinding] = []
        for item in work_items:
            if (
                item.status == "running"
                and item.lease is not None
                and item.timeout is not None
                and item.timeout <= observed_at
            ):
                findings.append(
                    SupervisorFinding(
                        reason="timeout_expired",
                        work_item_id=item.work_item_id,
                        observed_at=observed_at,
                        details={"lease_id": item.lease, "timeout": item.timeout.isoformat()},
                    )
                )
        return findings

    def detect_stale_runs(
        self,
        *,
        work_items: Mapping[str, WorkItem],
        runs: Iterable[RunRecord],
        now: datetime | None = None,
    ) -> list[SupervisorFinding]:
        """Find running RunRecords whose work-item lease state is stale."""

        observed_at = now or _now()
        findings: list[SupervisorFinding] = []
        for run in runs:
            if run.exit_status != "running":
                continue
            item = work_items.get(run.work_item_id)
            if item is None:
                findings.append(
                    SupervisorFinding(
                        reason="run_without_work_item",
                        work_item_id=run.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                    )
                )
                continue
            if item.status != "running":
                findings.append(
                    SupervisorFinding(
                        reason="work_item_not_running",
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                        details={"work_item_status": item.status},
                    )
                )
                continue
            if item.lease is None:
                findings.append(
                    SupervisorFinding(
                        reason="lease_missing",
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                    )
                )
                continue
            if item.timeout is not None and item.timeout <= observed_at:
                findings.append(
                    SupervisorFinding(
                        reason="timeout_expired",
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                        details={"lease_id": item.lease, "timeout": item.timeout.isoformat()},
                    )
                )
                continue
            if item.heartbeat is None:
                findings.append(
                    SupervisorFinding(
                        reason="heartbeat_missing",
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                    )
                )
                continue
            if item.heartbeat + self.stale_heartbeat_after <= observed_at:
                findings.append(
                    SupervisorFinding(
                        reason="heartbeat_stale",
                        work_item_id=item.work_item_id,
                        run_id=run.run_id,
                        observed_at=observed_at,
                        details={
                            "lease_id": item.lease,
                            "heartbeat": item.heartbeat.isoformat(),
                        },
                    )
                )
        return findings

    def build_recovery_plan(
        self,
        work_item: WorkItem,
        *,
        failure_category: FailureCategory,
        task_type: TaskType,
        created_by: str = "control-plane-supervisor",
        root_cause: str = "",
    ) -> SupervisorRecoveryPlan:
        """Map a failure to retryable work or a V6 recovery work item."""

        recovery = default_recovery_for_failure(failure_category)
        gate = GateEvaluation(
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            subject_ref=work_item.work_item_id,
            stage="workitem",
            task_type=task_type,
            rubric_version="supervisor-v6",
            metric_pack_version="supervisor-v6",
            north_star_verdict="fail",
            result_quality=0.0,
            speed=0.0,
            cost=0.0,
            risk=0.8,
            evidence_quality=0.0,
            collaboration_quality=0.0,
            failure_category=failure_category,
            root_cause=root_cause or f"{failure_category} on {work_item.work_item_id}",
            responsibility_scope=_responsibility_scope(failure_category),
            confidence=0.9,
            next_action=recovery.next_action,
            next_state=recovery.next_state,
            created_by=created_by,
        )
        if work_item.retry_budget > 0:
            retry_item = work_item.model_copy(
                update={
                    "status": "queued",
                    "lease": None,
                    "heartbeat": None,
                    "timeout": None,
                    "retry_budget": work_item.retry_budget - 1,
                }
            )
            return SupervisorRecoveryPlan(
                failure_category=failure_category,
                failure_recovery=recovery,
                action="retry",
                gate_evaluation=gate,
                retry_work_item=retry_item,
            )

        recovery_type = _RECOVERY_WORK_ITEM_TYPES[failure_category]
        recovery_item = WorkItem(
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            type=recovery_type,
            owner="control-plane-supervisor",
            priority=min(100, work_item.priority + 10),
            resource_locks=list(work_item.resource_locks),
            idempotency_key=(
                f"recovery:{work_item.work_item_id}:{failure_category}"
                if recovery_type == "external_worker"
                else None
            ),
            expected_output=(
                f"Recover {work_item.work_item_id} after {failure_category}; "
                f"next action: {recovery.next_action}"
            ),
        )
        return SupervisorRecoveryPlan(
            failure_category=failure_category,
            failure_recovery=recovery,
            action="create_recovery_work_item",
            gate_evaluation=gate,
            recovery_work_item=recovery_item,
        )

    def _assert_lease_matches(self, work_item: WorkItem, lease_id: str) -> None:
        if work_item.status != "running":
            raise ValueError(f"work item {work_item.work_item_id} is not running")
        if work_item.lease != lease_id:
            raise ValueError(f"lease {lease_id} does not own work item {work_item.work_item_id}")
