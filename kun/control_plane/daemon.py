"""Persistent daemon loop for KUN V6 Control Plane.

The daemon is the productized bridge between the pure runtime and the
supervisor protocol: it wakes up, checks durable state, recovers stale work,
runs ready work items through registered KUN-native runners, and records a
user-facing progress artifact each tick.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.activation import activate_work_item_features
from kun.control_plane.capability_execution import (
    CapabilityExecutionPolicy,
    build_capability_execution_policy,
)
from kun.control_plane.concurrency import (
    FileResourceLockStore,
    InMemoryResourceLockStore,
    ResourceLockConflict,
    SandboxIsolationMode,
    SandboxIsolationSpec,
    WorkerPoolConfig,
    WorkerSlotSnapshot,
    sandbox_spec_for_work_item,
    worker_slots,
)
from kun.control_plane.preflight import WorkItemPreflight, run_work_item_preflight
from kun.control_plane.runtime import ControlPlaneRunner, InMemoryControlPlane, WorkItemResult
from kun.control_plane.runtime_observation import (
    RuntimeObservationReport,
    build_runtime_observation_report,
)
from kun.control_plane.supervisor import MinimalSupervisor, SupervisorFinding
from kun.control_plane.v6 import (
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    GateEvaluation,
    Mission,
    MissionStatus,
    RunRecord,
    TaskPlan,
    WorkItem,
)
from kun.control_plane.watchtower_bridge import evaluate_v6_watchtower_event_sync
from kun.control_plane.workspace_snapshot import restore_workspace_snapshot

if TYPE_CHECKING:
    from kun.watchtower.engine import RuleEngine

ACTIVE_DAEMON_MISSION_STATUSES: frozenset[MissionStatus] = frozenset(
    {
        "queued",
        "running",
        "planning",
        "info_gap",
        "awaiting_approval",
        "waiting_human",
        "waiting_external",
        "blocked",
        "retrying",
        "repairing",
        "rolling_back",
        "changing_plan",
        "delivering",
        "awaiting_acceptance",
        "paused",
        "escalated",
    }
)

DaemonServiceStatus = Literal["starting", "running", "idle", "stopped", "unhealthy"]
DaemonServiceStoppedReason = Literal["idle", "max_ticks", "stop_requested", "error"]


def _now() -> datetime:
    return datetime.now(UTC)


class DaemonTickReport(BaseModel):
    """Auditable result of one daemon wakeup."""

    model_config = ConfigDict(extra="forbid")

    daemon_id: str
    observed_at: datetime
    mission_ids: list[str] = Field(default_factory=list)
    recovered_work_item_ids: list[str] = Field(default_factory=list)
    retired_work_item_ids: list[str] = Field(default_factory=list)
    retired_collaboration_ticket_ids: list[str] = Field(default_factory=list)
    retired_capability_profile_ids: list[str] = Field(default_factory=list)
    recovery_gate_refs: list[str] = Field(default_factory=list)
    created_work_item_ids: list[str] = Field(default_factory=list)
    created_collaboration_ticket_ids: list[str] = Field(default_factory=list)
    ran_work_item_ids: list[str] = Field(default_factory=list)
    resource_lock_skipped_work_item_ids: list[str] = Field(default_factory=list)
    run_refs: list[str] = Field(default_factory=list)
    no_runner_work_item_ids: list[str] = Field(default_factory=list)
    worker_slots: list[WorkerSlotSnapshot] = Field(default_factory=list)
    resource_lock_conflicts: list[ResourceLockConflict] = Field(default_factory=list)
    sandbox_specs: list[SandboxIsolationSpec] = Field(default_factory=list)
    finalized_mission_ids: list[str] = Field(default_factory=list)
    final_gate_refs: list[str] = Field(default_factory=list)
    delivery_manifest_refs: list[str] = Field(default_factory=list)
    progress_artifact_refs: list[str] = Field(default_factory=list)
    observation_artifact_refs: list[str] = Field(default_factory=list)
    runtime_observations: dict[str, RuntimeObservationReport] = Field(default_factory=dict)
    observation_followup_ids: list[str] = Field(default_factory=list)
    activation_artifact_refs: list[str] = Field(default_factory=list)
    preflight_artifact_refs: list[str] = Field(default_factory=list)
    preflight_failed_skill_ids: list[str] = Field(default_factory=list)
    watchtower_fired_rule_ids: list[str] = Field(default_factory=list)
    watchtower_error_count: int = 0
    capability_policy_ref: str | None = None
    capability_profile_refs: list[str] = Field(default_factory=list)
    capability_directive_count: int = 0


class DaemonLoopReport(BaseModel):
    """Auditable result of a daemon loop run."""

    model_config = ConfigDict(extra="forbid")

    daemon_id: str
    started_at: datetime
    ended_at: datetime
    tick_count: int
    stopped_reason: Literal["idle", "max_ticks", "stop_requested"]
    tick_reports: list[DaemonTickReport] = Field(default_factory=list)


class DaemonServiceConfig(BaseModel):
    """Launch settings for a long-lived Control Plane daemon process."""

    model_config = ConfigDict(extra="forbid")

    poll_interval_sec: float = Field(default=30.0, ge=0)
    max_work_items_per_tick: int = Field(default=10, ge=0)
    worker_pool_size: int = Field(default=1, ge=1, le=256)
    resource_lock_ttl_sec: float = Field(default=900.0, gt=0)
    sandbox_mode: SandboxIsolationMode = "workspace_snapshot"
    container_runtime: str | None = None
    max_ticks: int | None = Field(default=None, ge=1)
    stop_when_idle: bool = False
    idle_ticks_to_stop: int = Field(default=1, ge=1)
    stale_heartbeat_after_sec: float = Field(default=900.0, gt=0)


class DaemonServiceState(BaseModel):
    """Durable heartbeat for the background daemon service itself."""

    model_config = ConfigDict(extra="forbid")

    daemon_id: str
    status: DaemonServiceStatus
    started_at: datetime
    updated_at: datetime
    process_id: int
    tick_count: int = 0
    consecutive_idle_ticks: int = 0
    active_mission_ids: list[str] = Field(default_factory=list)
    last_heartbeat_at: datetime | None = None
    next_wakeup_at: datetime | None = None
    last_tick_observed_at: datetime | None = None
    last_tick_ran_work_item_ids: list[str] = Field(default_factory=list)
    last_tick_recovered_work_item_ids: list[str] = Field(default_factory=list)
    last_tick_progress_artifact_refs: list[str] = Field(default_factory=list)
    worker_pool_size: int = 1
    last_tick_worker_slots: list[WorkerSlotSnapshot] = Field(default_factory=list)
    last_tick_resource_lock_skipped_work_item_ids: list[str] = Field(default_factory=list)
    last_tick_resource_lock_conflicts: list[ResourceLockConflict] = Field(default_factory=list)
    last_tick_sandbox_specs: list[SandboxIsolationSpec] = Field(default_factory=list)
    stopped_reason: DaemonServiceStoppedReason | None = None
    stopped_at: datetime | None = None
    last_error: str | None = None

    def is_stale(self, *, now: datetime, stale_after: timedelta) -> bool:
        """Return whether a non-stopped daemon heartbeat is stale."""

        if self.status in {"stopped", "unhealthy"}:
            return False
        heartbeat = self.last_heartbeat_at or self.updated_at
        return heartbeat + stale_after <= now


class DaemonServiceClaim(BaseModel):
    """Result of claiming the durable daemon slot before a managed loop starts."""

    model_config = ConfigDict(extra="forbid")

    daemon_id: str
    accepted: bool
    previous_state: DaemonServiceState | None = None
    state: DaemonServiceState | None = None
    stale_previous: bool = False
    text: str


@dataclass(frozen=True)
class _PreparedWorkItemRun:
    """A claimed, activated work item ready for a worker thread."""

    work_item: WorkItem
    runner: ControlPlaneRunner
    slot: WorkerSlotSnapshot
    holder_id: str
    preserve_mission_status: bool
    preflight_failed_skill_ids: list[str]
    preflight_artifact_refs: list[str]


class DaemonServiceStopRequest(BaseModel):
    """Durable operator stop request consumed by the daemon service loop."""

    model_config = ConfigDict(extra="forbid")

    daemon_id: str
    requested_at: datetime
    requested_by: str = "kun"
    reason: str = "stop_requested"


class FileDaemonServiceStateStore:
    """Atomic JSON state file for daemon service heartbeat and stop reason."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.stop_request_path = self.path.with_name(f"{self.path.name}.stop.json")

    def load(self) -> DaemonServiceState | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return DaemonServiceState.model_validate(payload)

    def save(self, state: DaemonServiceState) -> DaemonServiceState:
        _write_json_atomic(self.path, state.model_dump_json(indent=2))
        return state

    def load_stop_request(self) -> DaemonServiceStopRequest | None:
        """Return a pending stop request, if an operator or watchdog wrote one."""

        if not self.stop_request_path.exists():
            return None
        payload = json.loads(self.stop_request_path.read_text(encoding="utf-8"))
        return DaemonServiceStopRequest.model_validate(payload)

    def request_stop(
        self,
        *,
        daemon_id: str,
        requested_by: str = "kun",
        reason: str = "stop_requested",
        now: datetime | None = None,
    ) -> DaemonServiceStopRequest:
        """Persist a durable stop request that survives process boundaries."""

        request = DaemonServiceStopRequest(
            daemon_id=daemon_id,
            requested_at=now or _now(),
            requested_by=requested_by,
            reason=reason,
        )
        _write_json_atomic(self.stop_request_path, request.model_dump_json(indent=2))
        return request

    def clear_stop_request(self) -> None:
        """Clear a consumed stop request."""

        self.stop_request_path.unlink(missing_ok=True)

    def stop_requested(self, *, daemon_id: str | None = None) -> bool:
        """Return whether a pending stop request applies to this daemon."""

        request = self.load_stop_request()
        if request is None:
            return False
        return daemon_id is None or request.daemon_id == daemon_id

    def claim_start(
        self,
        *,
        daemon_id: str,
        config: DaemonServiceConfig | None = None,
        now: datetime | None = None,
        process_id: int | None = None,
    ) -> DaemonServiceClaim:
        """Claim the service slot, replacing only stale or stopped daemons."""

        active_config = config or DaemonServiceConfig()
        observed_at = now or _now()
        previous = self.load()
        previous_active = previous is not None and previous.status in {
            "starting",
            "running",
            "idle",
        }
        previous_process_missing = (
            previous is not None and previous_active and not _process_is_alive(previous.process_id)
        )
        stale_previous = (
            previous.is_stale(
                now=observed_at,
                stale_after=timedelta(seconds=active_config.stale_heartbeat_after_sec),
            )
            if previous is not None
            else False
        ) or previous_process_missing
        if (
            previous is not None
            and previous.status in {"starting", "running", "idle"}
            and not stale_previous
        ):
            return DaemonServiceClaim(
                daemon_id=daemon_id,
                accepted=False,
                previous_state=previous,
                state=previous,
                stale_previous=False,
                text="已有后台监督服务心跳正常，拒绝重复启动。",
            )

        starting_state = DaemonServiceState(
            daemon_id=daemon_id,
            status="starting",
            started_at=observed_at,
            updated_at=observed_at,
            process_id=process_id or os.getpid(),
            active_mission_ids=list(previous.active_mission_ids) if previous else [],
            last_heartbeat_at=observed_at,
        )
        self.clear_stop_request()
        self.save(starting_state)
        text = (
            "上一次后台监督心跳已过期，已接管服务并准备恢复执行。"
            if stale_previous and not previous_process_missing
            else "上一次后台监督进程已不存在，已接管服务并准备恢复执行。"
            if previous_process_missing
            else "后台监督服务启动声明已写入持久状态。"
        )
        return DaemonServiceClaim(
            daemon_id=daemon_id,
            accepted=True,
            previous_state=previous,
            state=starting_state,
            stale_previous=stale_previous,
            text=text,
        )


class ControlPlaneDaemon:
    """One-shot daemon tick runner around a store-backed Control Plane."""

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        supervisor: MinimalSupervisor | None = None,
        daemon_id: str = "kun-control-plane-daemon",
        runners_by_owner: Mapping[str, ControlPlaneRunner] | None = None,
        runners_by_type: Mapping[str, ControlPlaneRunner] | None = None,
        default_runner: ControlPlaneRunner | None = None,
        rule_engine: RuleEngine | None = None,
        worker_pool: WorkerPoolConfig | None = None,
        resource_lock_store: FileResourceLockStore | InMemoryResourceLockStore | None = None,
        resource_lock_ttl_sec: float = 900.0,
        sandbox_mode: SandboxIsolationMode = "workspace_snapshot",
        container_runtime: str | None = None,
    ) -> None:
        self.control_plane = control_plane
        self.supervisor = supervisor or MinimalSupervisor()
        self.daemon_id = daemon_id
        self.runners_by_owner = dict(runners_by_owner or {})
        self.runners_by_type = dict(runners_by_type or {})
        self.default_runner = default_runner
        self.rule_engine = rule_engine
        self.worker_pool = worker_pool or WorkerPoolConfig()
        self.resource_lock_store = resource_lock_store or _default_resource_lock_store(control_plane)
        self.resource_lock_ttl = timedelta(seconds=resource_lock_ttl_sec)
        self.sandbox_mode = sandbox_mode
        self.container_runtime = container_runtime

    def tick_once(
        self,
        *,
        mission_ids: Sequence[str] | None = None,
        now: datetime | None = None,
        max_work_items: int = 10,
        write_progress: bool = True,
    ) -> DaemonTickReport:
        """Wake once: recover stale work, run ready work, and persist progress."""

        if max_work_items < 0:
            raise ValueError("max_work_items must be non-negative")
        observed_at = now or _now()
        selected_mission_ids = (
            list(mission_ids) if mission_ids is not None else self._active_missions()
        )
        report = DaemonTickReport(
            daemon_id=self.daemon_id,
            observed_at=observed_at,
            mission_ids=selected_mission_ids,
            worker_slots=worker_slots(self.worker_pool),
        )
        self._resolve_capability_duplicates(
            mission_ids=selected_mission_ids,
            observed_at=observed_at,
            report=report,
        )
        capability_policy = build_capability_execution_policy(
            self.control_plane.list_default_runtime_capabilities(),
            policy_id=f"policy-{self.daemon_id}-{_compact_time(observed_at)}",
            built_at=observed_at,
        )
        if capability_policy.capability_profile_refs:
            report.capability_policy_ref = capability_policy.policy_id
            report.capability_profile_refs = list(capability_policy.capability_profile_refs)
            report.capability_directive_count = len(capability_policy.directives)

        for mission_id in selected_mission_ids:
            self._ensure_info_gap_collaboration(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_delivery_acceptance_collaboration(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_plan_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_collaboration_tickets(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )

        for mission_id in selected_mission_ids:
            self._recover_stale_work(mission_id=mission_id, report=report, now=observed_at)

        remaining = max_work_items
        while remaining > 0:
            claimed_resource_locks: set[str] = set()
            prepared_runs: list[_PreparedWorkItemRun] = []
            batch_limit = min(remaining, max(1, self.worker_pool.worker_count))
            while len(prepared_runs) < batch_limit:
                selected_this_pass = False
                for mission_id in selected_mission_ids:
                    if len(prepared_runs) >= batch_limit:
                        break
                    mission = self.control_plane.missions.get(mission_id)
                    if mission is None or mission.status not in ACTIVE_DAEMON_MISSION_STATUSES:
                        continue
                    delivery_state = mission.status in {"delivering", "awaiting_acceptance"}
                    work_item = self._next_non_conflicting_ready_work_item(
                        mission_id=mission_id,
                        claimed_resource_locks=claimed_resource_locks,
                        report=report,
                    )
                    if work_item is None:
                        continue
                    if delivery_state and not _is_delivery_state_followup(work_item):
                        continue
                    slot = _slot_for_run(report.worker_slots, len(prepared_runs))
                    prepared = self._prepare_work_item_run(
                        work_item=work_item,
                        slot=slot,
                        delivery_state=delivery_state,
                        claimed_resource_locks=claimed_resource_locks,
                        capability_policy=capability_policy,
                        observed_at=observed_at,
                        report=report,
                    )
                    if prepared is None:
                        continue
                    prepared_runs.append(prepared)
                    claimed_resource_locks.update(
                        _effective_resource_locks(self.control_plane, prepared.work_item)
                    )
                    selected_this_pass = True
                if not selected_this_pass:
                    break
            if not prepared_runs:
                break
            completed_runs = self._run_prepared_work_items_in_parallel(prepared_runs)
            if not completed_runs:
                break
            for prepared, run in completed_runs:
                report.ran_work_item_ids.append(run.work_item_id)
                report.run_refs.append(run.run_id)
                fired, error_count = self._evaluate_watchtower_for_run(run)
                report.watchtower_fired_rule_ids.extend(fired)
                report.watchtower_error_count += error_count
                self._queue_preflight_followups(
                    work_item=prepared.work_item,
                    failed_skill_ids=prepared.preflight_failed_skill_ids,
                    artifact_refs=prepared.preflight_artifact_refs,
                    report=report,
                )
                remaining -= 1

        for mission_id in selected_mission_ids:
            self._finalize_idle_mission(mission_id=mission_id, report=report)

        if write_progress:
            for mission_id in selected_mission_ids:
                observation = self._observation_artifact(
                    mission_id=mission_id,
                    now=observed_at,
                    report=report,
                    capability_policy=capability_policy,
                )
                self._upsert_artifact(observation)
                report.observation_artifact_refs.append(observation.artifact_id)
                self._queue_observation_followups(
                    mission_id=mission_id,
                    observation_artifact_ref=observation.artifact_id,
                    report=report,
                )
                artifact = self._progress_artifact(
                    mission_id=mission_id,
                    now=observed_at,
                    capability_policy=capability_policy,
                )
                self._upsert_artifact(artifact)
                report.progress_artifact_refs.append(artifact.artifact_id)
        return report

    def run_loop(
        self,
        *,
        mission_ids: Sequence[str] | None = None,
        poll_interval_sec: float = 30.0,
        max_work_items_per_tick: int = 10,
        max_ticks: int | None = None,
        stop_when_idle: bool = False,
        idle_ticks_to_stop: int = 1,
        stop_requested: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now_factory: Callable[[], datetime] = _now,
    ) -> DaemonLoopReport:
        """Run the daemon wakeup loop until an explicit stop condition is met."""

        if poll_interval_sec < 0:
            raise ValueError("poll_interval_sec must be non-negative")
        if max_ticks is not None and max_ticks <= 0:
            raise ValueError("max_ticks must be positive when provided")
        if idle_ticks_to_stop <= 0:
            raise ValueError("idle_ticks_to_stop must be positive")

        started_at = now_factory()
        tick_reports: list[DaemonTickReport] = []
        idle_ticks = 0
        stopped_reason: Literal["idle", "max_ticks", "stop_requested"] = "max_ticks"
        while True:
            if stop_requested is not None and stop_requested():
                stopped_reason = "stop_requested"
                break
            report = self.tick_once(
                mission_ids=mission_ids,
                now=now_factory(),
                max_work_items=max_work_items_per_tick,
            )
            tick_reports.append(report)
            if _tick_is_idle(report):
                idle_ticks += 1
            else:
                idle_ticks = 0
            if stop_when_idle and idle_ticks >= idle_ticks_to_stop:
                stopped_reason = "idle"
                break
            if max_ticks is not None and len(tick_reports) >= max_ticks:
                stopped_reason = "max_ticks"
                break
            sleeper(poll_interval_sec)
        return DaemonLoopReport(
            daemon_id=self.daemon_id,
            started_at=started_at,
            ended_at=now_factory(),
            tick_count=len(tick_reports),
            stopped_reason=stopped_reason,
            tick_reports=tick_reports,
        )

    def run_managed_loop(
        self,
        *,
        config: DaemonServiceConfig | None = None,
        state_store: FileDaemonServiceStateStore | None = None,
        mission_ids: Sequence[str] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now_factory: Callable[[], datetime] = _now,
    ) -> DaemonLoopReport:
        """Run as a service and persist daemon heartbeat/stop state each tick."""

        active_config = config or DaemonServiceConfig()
        self.worker_pool = self.worker_pool.model_copy(
            update={"worker_count": active_config.worker_pool_size}
        )
        self.resource_lock_ttl = timedelta(seconds=active_config.resource_lock_ttl_sec)
        self.sandbox_mode = active_config.sandbox_mode
        self.container_runtime = active_config.container_runtime
        started_at = now_factory()
        tick_reports: list[DaemonTickReport] = []
        idle_ticks = 0
        if state_store is not None:
            claim = state_store.claim_start(
                daemon_id=self.daemon_id,
                config=active_config,
                now=started_at,
                process_id=os.getpid(),
            )
            if not claim.accepted:
                raise RuntimeError(claim.text)
        else:
            self._save_service_state(
                state_store,
                DaemonServiceState(
                    daemon_id=self.daemon_id,
                    status="starting",
                    started_at=started_at,
                    updated_at=started_at,
                    process_id=os.getpid(),
                ),
            )
        stopped_reason: DaemonServiceStoppedReason = "max_ticks"
        try:
            while True:
                if stop_requested is not None and stop_requested():
                    stopped_reason = "stop_requested"
                    break
                report = self.tick_once(
                    mission_ids=mission_ids,
                    now=now_factory(),
                    max_work_items=active_config.max_work_items_per_tick,
                )
                tick_reports.append(report)
                if _tick_is_idle(report):
                    idle_ticks += 1
                    status: DaemonServiceStatus = "idle"
                else:
                    idle_ticks = 0
                    status = "running"
                next_wakeup = report.observed_at + timedelta(
                    seconds=active_config.poll_interval_sec
                )
                self._save_service_state(
                    state_store,
                    _service_state_from_tick(
                        daemon_id=self.daemon_id,
                        started_at=started_at,
                        report=report,
                        status=status,
                        tick_count=len(tick_reports),
                        consecutive_idle_ticks=idle_ticks,
                        next_wakeup_at=next_wakeup,
                    ),
                )
                if active_config.stop_when_idle and idle_ticks >= active_config.idle_ticks_to_stop:
                    stopped_reason = "idle"
                    break
                if (
                    active_config.max_ticks is not None
                    and len(tick_reports) >= active_config.max_ticks
                ):
                    stopped_reason = "max_ticks"
                    break
                sleeper(active_config.poll_interval_sec)
        except Exception as exc:
            stopped_at = now_factory()
            self._save_service_state(
                state_store,
                DaemonServiceState(
                    daemon_id=self.daemon_id,
                    status="unhealthy",
                    started_at=started_at,
                    updated_at=stopped_at,
                    process_id=os.getpid(),
                    tick_count=len(tick_reports),
                    consecutive_idle_ticks=idle_ticks,
                    active_mission_ids=list(mission_ids or self._active_missions()),
                    worker_pool_size=self.worker_pool.worker_count,
                    stopped_reason="error",
                    stopped_at=stopped_at,
                    last_error=f"{type(exc).__name__}: {exc}",
                ),
            )
            raise

        ended_at = now_factory()
        self._save_service_state(
            state_store,
            DaemonServiceState(
                daemon_id=self.daemon_id,
                status="stopped",
                started_at=started_at,
                updated_at=ended_at,
                process_id=os.getpid(),
                tick_count=len(tick_reports),
                consecutive_idle_ticks=idle_ticks,
                active_mission_ids=list(mission_ids or self._active_missions()),
                last_heartbeat_at=tick_reports[-1].observed_at if tick_reports else None,
                last_tick_observed_at=tick_reports[-1].observed_at if tick_reports else None,
                last_tick_ran_work_item_ids=tick_reports[-1].ran_work_item_ids
                if tick_reports
                else [],
                last_tick_recovered_work_item_ids=tick_reports[-1].recovered_work_item_ids
                if tick_reports
                else [],
                last_tick_progress_artifact_refs=tick_reports[-1].progress_artifact_refs
                if tick_reports
                else [],
                worker_pool_size=self.worker_pool.worker_count,
                last_tick_worker_slots=tick_reports[-1].worker_slots if tick_reports else [],
                last_tick_resource_lock_skipped_work_item_ids=tick_reports[
                    -1
                ].resource_lock_skipped_work_item_ids
                if tick_reports
                else [],
                last_tick_resource_lock_conflicts=tick_reports[-1].resource_lock_conflicts
                if tick_reports
                else [],
                last_tick_sandbox_specs=tick_reports[-1].sandbox_specs if tick_reports else [],
                stopped_reason=stopped_reason,
                stopped_at=ended_at,
            ),
        )
        if stopped_reason == "stop_requested" and state_store is not None:
            state_store.clear_stop_request()
        return DaemonLoopReport(
            daemon_id=self.daemon_id,
            started_at=started_at,
            ended_at=ended_at,
            tick_count=len(tick_reports),
            stopped_reason=stopped_reason,
            tick_reports=tick_reports,
        )

    def _active_missions(self) -> list[str]:
        return sorted(
            mission.mission_id
            for mission in self.control_plane.missions.values()
            if mission.status in ACTIVE_DAEMON_MISSION_STATUSES
        )

    def _next_non_conflicting_ready_work_item(
        self,
        *,
        mission_id: str,
        claimed_resource_locks: set[str],
        report: DaemonTickReport,
    ) -> WorkItem | None:
        for candidate in self.control_plane.ready_work_items(
            mission_id,
            now=report.observed_at,
        ):
            locks = _effective_resource_locks(self.control_plane, candidate)
            if locks and claimed_resource_locks.intersection(locks):
                if candidate.work_item_id not in report.resource_lock_skipped_work_item_ids:
                    report.resource_lock_skipped_work_item_ids.append(candidate.work_item_id)
                continue
            return candidate
        return None

    def _prepare_work_item_run(
        self,
        *,
        work_item: WorkItem,
        slot: WorkerSlotSnapshot,
        delivery_state: bool,
        claimed_resource_locks: set[str],
        capability_policy: CapabilityExecutionPolicy,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> _PreparedWorkItemRun | None:
        runner = self._runner_for(work_item)
        if runner is None:
            report.no_runner_work_item_ids.append(work_item.work_item_id)
            return None
        locks = _effective_resource_locks(self.control_plane, work_item)
        if locks and claimed_resource_locks.intersection(locks):
            if work_item.work_item_id not in report.resource_lock_skipped_work_item_ids:
                report.resource_lock_skipped_work_item_ids.append(work_item.work_item_id)
            slot.status = "waiting_lock"
            slot.mission_id = work_item.mission_id
            slot.work_item_id = work_item.work_item_id
            slot.resource_locks = sorted(locks)
            slot.waiting_reason = "同一 tick 内已有 worker 领取了共享资源，等待下一轮。"
            return None
        holder_id = _lease_id(
            daemon_id=self.daemon_id,
            worker_id=slot.worker_id,
            work_item_id=work_item.work_item_id,
            observed_at=observed_at,
        )
        acquisition = self.resource_lock_store.acquire_many(
            resources=sorted(locks),
            holder_id=holder_id,
            daemon_id=self.daemon_id,
            worker_id=slot.worker_id,
            work_item=work_item,
            now=observed_at,
            ttl=self.resource_lock_ttl,
        )
        if not acquisition.acquired:
            if work_item.work_item_id not in report.resource_lock_skipped_work_item_ids:
                report.resource_lock_skipped_work_item_ids.append(work_item.work_item_id)
            report.resource_lock_conflicts.extend(acquisition.conflicts)
            slot.status = "waiting_lock"
            slot.mission_id = work_item.mission_id
            slot.work_item_id = work_item.work_item_id
            slot.resource_locks = sorted(locks)
            slot.waiting_reason = "等待资源锁释放后继续。"
            return None
        claimed_item = self._claim_work_item_lease(
            work_item=work_item,
            lease=holder_id,
            now=observed_at,
        )
        if claimed_item is None:
            self.resource_lock_store.release_holder(holder_id, now=observed_at)
            slot.status = "blocked"
            slot.mission_id = work_item.mission_id
            slot.work_item_id = work_item.work_item_id
            slot.waiting_reason = "工作项已被其他 worker 领取或不再可执行。"
            return None
        activation = activate_work_item_features(
            control_plane=self.control_plane,
            work_item=claimed_item,
            capability_policy=capability_policy,
            actor=self.daemon_id,
            observed_at=observed_at,
        )
        self.control_plane.work_items[activation.work_item.work_item_id] = activation.work_item
        self._persist_work_item(activation.work_item)
        for artifact in activation.artifacts:
            self._upsert_artifact(artifact)
            report.activation_artifact_refs.append(artifact.artifact_id)
        sandbox_spec = sandbox_spec_for_work_item(
            work_item=activation.work_item,
            mode=self.sandbox_mode,
            workspace_ref=_workspace_path_from_contract(
                self.control_plane.contracts.get(
                    self.control_plane.missions[
                        activation.work_item.mission_id
                    ].execution_contract_ref
                    or ""
                )
            ),
            container_runtime=self.container_runtime,
        )
        report.sandbox_specs.append(sandbox_spec)
        active_runner = runner
        if self.sandbox_mode == "container_required" and not getattr(
            runner,
            "supports_container_sandbox",
            False,
        ):
            active_runner = _SandboxRequirementRunner(
                runner_identity=runner.runner_identity,
                sandbox_spec=sandbox_spec,
            )
        slot.status = "running"
        slot.mission_id = activation.work_item.mission_id
        slot.work_item_id = activation.work_item.work_item_id
        slot.runner_identity = active_runner.runner_identity
        slot.resource_locks = sorted(
            _effective_resource_locks(self.control_plane, activation.work_item)
        )
        slot.sandbox_ref = sandbox_spec.sandbox_ref
        if _should_preflight_work_item(activation.work_item):
            preflight = run_work_item_preflight(
                control_plane=self.control_plane,
                work_item=activation.work_item,
                actor=self.daemon_id,
                observed_at=observed_at,
            )
        else:
            preflight = WorkItemPreflight()
        preflight_artifact_refs: list[str] = []
        for artifact in preflight.artifacts:
            self._upsert_artifact(artifact)
            preflight_artifact_refs.append(artifact.artifact_id)
            report.preflight_artifact_refs.append(artifact.artifact_id)
        report.preflight_failed_skill_ids.extend(preflight.failed_skill_ids)
        _bind_capability_policy(active_runner, capability_policy)
        return _PreparedWorkItemRun(
            work_item=activation.work_item,
            runner=active_runner,
            slot=slot,
            holder_id=holder_id,
            preserve_mission_status=delivery_state,
            preflight_failed_skill_ids=list(preflight.failed_skill_ids),
            preflight_artifact_refs=preflight_artifact_refs,
        )

    def _run_prepared_work_items_in_parallel(
        self,
        prepared_runs: list[_PreparedWorkItemRun],
    ) -> list[tuple[_PreparedWorkItemRun, RunRecord]]:
        if len(prepared_runs) == 1:
            run = self._execute_prepared_work_item(prepared_runs[0])
            return [(prepared_runs[0], run)] if run is not None else []
        completed: list[tuple[_PreparedWorkItemRun, RunRecord]] = []
        max_workers = min(len(prepared_runs), max(1, self.worker_pool.worker_count))
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"{_slug(self.daemon_id)}-worker",
        ) as executor:
            futures = {
                executor.submit(self._execute_prepared_work_item, prepared): prepared
                for prepared in prepared_runs
            }
            for future in as_completed(futures):
                prepared = futures[future]
                run = future.result()
                if run is not None:
                    completed.append((prepared, run))
        return sorted(completed, key=lambda pair: pair[1].started_at)

    def _execute_prepared_work_item(
        self,
        prepared: _PreparedWorkItemRun,
    ) -> RunRecord | None:
        try:
            started = self.control_plane.start_work_item_run(
                work_item_id=prepared.work_item.work_item_id,
                runner=prepared.runner,
                preserve_mission_status=prepared.preserve_mission_status,
                lease=prepared.holder_id,
            )
            if started is None:
                return None
            run, running_item = started
            try:
                result = prepared.runner.run(running_item)
            except Exception:  # pragma: no cover - runner failures are normalized.
                result = WorkItemResult(status="failed", failure_category="tool_failure")
            return self.control_plane.finish_work_item_run(
                run_id=run.run_id,
                result=result,
                preserve_mission_status=prepared.preserve_mission_status,
            )
        finally:
            self.resource_lock_store.release_holder(prepared.holder_id, now=_now())
            prepared.slot.status = "idle"

    def _ensure_info_gap_collaboration(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None:
            return
        plan = self._current_task_plan(mission)
        info_gaps = list(plan.info_gaps) if plan is not None else []
        if not info_gaps and mission.status != "info_gap":
            return
        ticket_id = (
            f"collab-info-gap-{_slug(mission.mission_id)}-"
            f"{_slug(plan.version if plan is not None else 'intake')}"
        )
        if ticket_id in self.control_plane.collaboration_tickets:
            return
        if mission.status == "planning" and info_gaps:
            self.control_plane.transition_mission(
                mission_id=mission.mission_id,
                target="info_gap",
                actor=self.daemon_id,
                reason="daemon found unresolved task-plan information gaps before execution",
                subject_ref=plan.plan_id if plan is not None else mission.mission_id,
            )
            mission = self.control_plane.missions[mission.mission_id]
        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission.mission_id,
            type="expert_input",
            role_needed=mission.owner or "mission-owner",
            why_needed=_info_gap_reason(mission=mission, plan=plan, info_gaps=info_gaps),
            context_ref=mission.working_context_ref
            or (plan.plan_id if plan is not None else mission.mission_id),
            risk_if_skipped=(
                "KUN may plan or execute against missing constraints, acceptance criteria, "
                "permissions, or domain facts."
            ),
            deadline=observed_at + timedelta(hours=24),
            sla_policy={"reminder_after_hours": 6, "escalate_after_hours": 24},
            escalation_policy={
                "after_deadline": "pause_or_choose_low_risk_fallback_before_execution"
            },
            fallback_policy={
                "allowed": True,
                "rule": "use lowest-risk assumption only when explicitly safe and reversible",
            },
            resume_after_response=True,
            output_contract="Answer each missing information gap or approve a reversible assumption.",
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        report.created_collaboration_ticket_ids.append(ticket.ticket_id)
        if self.control_plane.missions[mission.mission_id].status == "info_gap":
            self.control_plane.transition_mission(
                mission_id=mission.mission_id,
                target="waiting_human",
                actor=self.daemon_id,
                reason="daemon opened an information-gap ticket and is waiting for input",
                subject_ref=ticket.ticket_id,
            )

    def _ensure_delivery_acceptance_collaboration(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        if mission.status not in {"delivering", "awaiting_acceptance"}:
            return
        if mission.acceptance_ref is not None:
            return
        delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission)
        if delivery_manifest_ref is None:
            return
        ticket_id = (
            f"collab-acceptance-{_slug(mission.mission_id)}-"
            f"{_slug(delivery_manifest_ref)}"
        )
        if ticket_id in self.control_plane.collaboration_tickets:
            return
        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission.mission_id,
            type="review",
            role_needed=mission.owner or "mission-owner",
            why_needed=(
                "KUN has produced a user-facing product delivery. Automated gates can prove "
                "build, evidence, and residual thresholds, but a human or target-user review is "
                "needed to confirm product feel, usefulness, and acceptance."
            ),
            context_ref=delivery_manifest_ref,
            risk_if_skipped=(
                "KUN may close a product task after mechanism-level tests while subjective "
                "experience, visual quality, or real user value remains below the user's bar."
            ),
            deadline=observed_at + timedelta(hours=24),
            sla_policy={"reminder_after_hours": 6, "escalate_after_hours": 24},
            escalation_policy={
                "after_deadline": "continue dogfood iteration only if the risk is reversible"
            },
            fallback_policy={
                "allowed": True,
                "rule": (
                    "if no human response arrives, keep the mission in awaiting_acceptance and "
                    "continue only low-risk polish or evidence improvements"
                ),
            },
            resume_after_response=True,
            recommended_option="review the playable artifact and answer accept / rework / reject",
            output_contract=(
                "Return an acceptance decision, satisfaction score, and concrete requested "
                "changes if the delivery is not good enough."
            ),
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        report.created_collaboration_ticket_ids.append(ticket.ticket_id)
        if mission.status == "delivering":
            self.control_plane.transition_mission(
                mission_id=mission.mission_id,
                target="awaiting_acceptance",
                actor=self.daemon_id,
                reason="daemon opened a human product-acceptance ticket for subjective validation",
                subject_ref=ticket.ticket_id,
            )

    def _resolve_capability_duplicates(
        self,
        *,
        mission_ids: Sequence[str],
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        governance = self.control_plane.govern_default_runtime_capabilities()
        if not governance.duplicate_profile_refs:
            return
        mission_id = mission_ids[0] if mission_ids else "control-plane"
        artifact = ArtifactRecord(
            artifact_id=f"artifact-capability-dedupe-{_compact_time(observed_at)}",
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/capability-dedupe/"
                f"{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(governance.model_dump(mode="json")),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "capability_duplicate_resolution",
                "qi_capability_governance",
                "state_hygiene",
                *[f"kept_capability:{ref}" for ref in governance.profile_refs],
                *[f"retired_capability:{ref}" for ref in governance.duplicate_profile_refs],
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        retired: list[str] = []
        for capability_id in governance.duplicate_profile_refs:
            profile = self.control_plane.capability_profiles.get(capability_id)
            if profile is None or not profile.runtime_enabled:
                continue
            updated = profile.model_copy(
                update={
                    "runtime_enabled": False,
                    "rolled_back_at": observed_at,
                    "rollback_reason": (
                        "superseded by stronger production capability with the same governance key"
                    ),
                    "rollback_refs": _dedupe([*profile.rollback_refs, artifact.artifact_id]),
                    "known_limits": _dedupe(
                        [
                            *profile.known_limits,
                            "disabled by automatic capability duplicate governance",
                        ]
                    ),
                }
            )
            self.control_plane.capability_profiles[capability_id] = updated
            self._persist_capability_profile(updated)
            retired.append(capability_id)
        report.retired_capability_profile_ids.extend(retired)
        if retired and "qi" in self.runners_by_owner:
            mission = self.control_plane.missions.get(mission_id)
            evidence_sig = _hash_payload(
                {
                    "kept": governance.profile_refs,
                    "retired": retired,
                    "artifact": artifact.artifact_id,
                }
            )[:12]
            work_item_id = (
                f"work-qi-capability-dedupe-{_slug(mission_id)}-{evidence_sig}"
            )
            if work_item_id in self.control_plane.work_items:
                return
            work_item = WorkItem(
                work_item_id=work_item_id,
                mission_id=mission_id,
                task_plan_version=mission.current_plan_version
                if mission is not None and mission.current_plan_version
                else "runtime-capability-governance",
                type="governance",
                owner="qi",
                priority=88,
                idempotency_key=f"capability-dedupe:{mission_id}:{evidence_sig}",
                expected_output=(
                    "Audit and govern duplicate production runtime capabilities. Confirm the "
                    "kept profile, merged/retired profiles, source versions, rollback evidence, "
                    "and whether any retired behavior should remain as replay evidence only. "
                    "Do not enable replay/holdout/shadow profiles as runtime defaults."
                ),
                recovery_refs=[
                    artifact.artifact_id,
                    *governance.profile_refs,
                    *retired,
                ],
            )
            self.control_plane.work_items[work_item.work_item_id] = work_item
            self._persist_work_item(work_item)
            report.created_work_item_ids.append(work_item.work_item_id)

    def _retire_superseded_plan_work(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or not mission.current_plan_version:
            return
        if mission.status not in {"delivering", "awaiting_acceptance", "closed", "partial_closed"}:
            return
        active_version = mission.current_plan_version
        retired: list[str] = []
        for work_item in list(self.control_plane.work_items.values()):
            if work_item.mission_id != mission_id:
                continue
            if work_item.task_plan_version == active_version:
                continue
            if work_item.status in {"done", "cancelled"}:
                continue
            updated = work_item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[work_item.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(work_item.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=f"artifact-superseded-work-cleanup-{mission_id}-{_compact_time(observed_at)}",
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"superseded-work-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "active_plan_version": active_version,
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "superseded_work_cleanup",
                "state_hygiene",
                f"active_plan:{active_version}",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _retire_superseded_collaboration_tickets(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or not mission.current_plan_version:
            return
        if mission.status not in {"delivering", "awaiting_acceptance", "closed", "partial_closed"}:
            return

        active_version = mission.current_plan_version
        latest_delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission)
        active_context_refs = {
            ref
            for ref in {
                mission.working_context_ref,
                mission.acceptance_ref,
                latest_delivery_manifest_ref,
            }
            if ref
        }
        for plan in self.control_plane.task_plans.values():
            if plan.mission_id == mission_id and plan.version == active_version:
                active_context_refs.add(plan.plan_id)

        retired: list[str] = []
        for ticket in list(self.control_plane.collaboration_tickets.values()):
            if ticket.mission_id != mission_id:
                continue
            if ticket.status not in {"open", "waiting", "escalated", "fallback_selected"}:
                continue
            if ticket.context_ref in active_context_refs:
                continue
            if (
                latest_delivery_manifest_ref is not None
                and ticket.ticket_id
                == f"collab-acceptance-{_slug(mission_id)}-{_slug(latest_delivery_manifest_ref)}"
            ):
                continue
            updated = ticket.model_copy(update={"status": "cancelled"})
            self.control_plane.collaboration_tickets[ticket.ticket_id] = updated
            self._persist_collaboration_ticket(updated)
            retired.append(ticket.ticket_id)

        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-superseded-collaboration-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"superseded-collaboration-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "active_plan_version": active_version,
                    "active_context_refs": sorted(active_context_refs),
                    "retired_collaboration_ticket_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "superseded_collaboration_cleanup",
                "state_hygiene",
                "human_collaboration_hygiene",
                f"active_plan:{active_version}",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_collaboration_ticket_ids.extend(retired)

    def _current_task_plan(self, mission: Mission) -> TaskPlan | None:
        plans = [
            plan
            for plan in self.control_plane.task_plans.values()
            if plan.mission_id == mission.mission_id
        ]
        if not plans:
            return None
        if mission.current_plan_version:
            for plan in plans:
                if plan.version == mission.current_plan_version:
                    return plan
        return max(plans, key=lambda plan: (plan.version, plan.plan_id))

    def _recover_stale_work(
        self,
        *,
        mission_id: str,
        report: DaemonTickReport,
        now: datetime,
    ) -> None:
        mission_work_items = {
            item.work_item_id: item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
        }
        findings = self.supervisor.detect_stale_runs(
            work_items=mission_work_items,
            runs=[
                run
                for run in self.control_plane.runs.values()
                if run.work_item_id in mission_work_items
            ],
            now=now,
        )
        seen: set[tuple[str | None, str]] = set()
        for finding in [
            *findings,
            *self.supervisor.detect_timeouts(mission_work_items.values(), now=now),
        ]:
            key = (finding.work_item_id, finding.reason)
            if key in seen:
                continue
            seen.add(key)
            self._apply_recovery(finding=finding, report=report, now=now)

    def _apply_recovery(
        self,
        *,
        finding: SupervisorFinding,
        report: DaemonTickReport,
        now: datetime,
    ) -> None:
        if finding.work_item_id is None:
            return
        work_item = self.control_plane.work_items.get(finding.work_item_id)
        if work_item is None:
            return
        plan = self.supervisor.build_recovery_plan(
            work_item,
            failure_category=finding.failure_category,
            task_type=self.control_plane.missions[work_item.mission_id].task_type,
            root_cause=finding.reason,
        )
        failed_item = work_item.model_copy(
            update={
                "status": "failed",
                "lease": None,
                "heartbeat": now,
                "timeout": None,
            }
        )
        self.control_plane.work_items[failed_item.work_item_id] = failed_item
        self._persist_work_item(failed_item)
        self._close_running_run(finding=finding, work_item=failed_item, now=now)
        self.control_plane.apply_gate(plan.gate_evaluation)
        report.recovered_work_item_ids.append(work_item.work_item_id)
        report.recovery_gate_refs.append(plan.gate_evaluation.gate_evaluation_id)

        if plan.retry_work_item is not None:
            self.control_plane.work_items[plan.retry_work_item.work_item_id] = plan.retry_work_item
            self._persist_work_item(plan.retry_work_item)
            self._transition_to_queued(
                work_item.mission_id, subject_ref=plan.retry_work_item.work_item_id
            )
        if plan.recovery_work_item is not None:
            self.control_plane.work_items[plan.recovery_work_item.work_item_id] = (
                plan.recovery_work_item
            )
            self._persist_work_item(plan.recovery_work_item)
            report.created_work_item_ids.append(plan.recovery_work_item.work_item_id)
            self._transition_to_queued(
                work_item.mission_id,
                subject_ref=plan.recovery_work_item.work_item_id,
            )

    def _close_running_run(
        self,
        *,
        finding: SupervisorFinding,
        work_item: WorkItem,
        now: datetime,
    ) -> None:
        run = self._run_for_finding(finding=finding, work_item=work_item)
        if run is None or run.exit_status != "running":
            return
        failed_run = RunRecord.model_validate(
            {
                **run.model_dump(),
                "ended_at": now,
                "exit_status": "failed",
                "failure_category": finding.failure_category,
            }
        )
        self.control_plane.runs[failed_run.run_id] = failed_run
        if self.control_plane.store is not None:
            self.control_plane.store.put_run_record(failed_run)

    def _run_for_finding(
        self,
        *,
        finding: SupervisorFinding,
        work_item: WorkItem,
    ) -> RunRecord | None:
        if finding.run_id is not None:
            return self.control_plane.runs.get(finding.run_id)
        running = [
            run
            for run in self.control_plane.runs.values()
            if run.work_item_id == work_item.work_item_id and run.exit_status == "running"
        ]
        return max(running, key=lambda run: run.started_at, default=None)

    def _runner_for(self, work_item: WorkItem) -> ControlPlaneRunner | None:
        for runner in (
            self.runners_by_owner.get(work_item.owner),
            self.runners_by_type.get(work_item.type),
            self.default_runner,
        ):
            if runner is None:
                continue
            can_run = getattr(runner, "can_run", None)
            if callable(can_run) and not can_run(work_item):
                continue
            return runner
        if work_item.type == "rollback" and work_item.rollback_refs:
            return _WorkspaceRollbackRunner(self.control_plane, self.daemon_id)
        return None

    def _claim_work_item_lease(
        self,
        *,
        work_item: WorkItem,
        lease: str,
        now: datetime,
    ) -> WorkItem | None:
        timeout = now + self.resource_lock_ttl
        claim = getattr(self.control_plane.store, "claim_work_item_lease", None)
        if callable(claim):
            claimed = claim(
                work_item_id=work_item.work_item_id,
                lease=lease,
                now=now,
                timeout=timeout,
            )
            if claimed is None:
                return None
            merged = work_item.model_copy(
                update={
                    "lease": claimed.lease,
                    "heartbeat": claimed.heartbeat,
                    "timeout": claimed.timeout,
                }
            )
            self.control_plane.work_items[merged.work_item_id] = merged
            self._persist_work_item(merged)
            return merged
        active_lease = (
            work_item.lease is not None
            and work_item.timeout is not None
            and work_item.timeout > now
            and work_item.lease != lease
        )
        if active_lease:
            return None
        claimed = work_item.model_copy(
            update={"lease": lease, "heartbeat": now, "timeout": timeout}
        )
        self.control_plane.work_items[claimed.work_item_id] = claimed
        self._persist_work_item(claimed)
        return claimed

    def _evaluate_watchtower_for_run(self, run: RunRecord) -> tuple[list[str], int]:
        if self.rule_engine is None:
            return [], 0
        work_item = self.control_plane.work_items.get(run.work_item_id)
        if work_item is None:
            return [], 0
        mission = self.control_plane.missions.get(work_item.mission_id)
        if mission is None:
            return [], 0
        gate = (
            self.control_plane.gate_evaluations.get(run.gate_evaluation_ref)
            if run.gate_evaluation_ref
            else None
        )
        artifacts = self._artifacts_for_run(work_item=work_item, gate=gate)
        event_types = [
            "control_plane.work_item.completed"
            if run.exit_status == "succeeded"
            else "control_plane.work_item.failed"
        ]
        if gate is not None:
            event_types.append("control_plane.gate_evaluated")
        fired: list[str] = []
        errors = 0
        for event_type in event_types:
            try:
                report = evaluate_v6_watchtower_event_sync(
                    self.rule_engine,
                    event_type=event_type,
                    mission=mission,
                    work_item=work_item,
                    run=run,
                    gate=gate,
                    artifacts=artifacts,
                    tenant_id="control-plane",
                )
            except Exception:
                errors += 1
                continue
            fired.extend(report.fired_rule_ids)
        return _merge_unique(fired), errors

    def _artifacts_for_run(
        self,
        *,
        work_item: WorkItem,
        gate: GateEvaluation | None,
    ) -> list[ArtifactRecord]:
        artifact_ids = {
            artifact.artifact_id
            for artifact in self.control_plane.artifacts.values()
            if artifact.work_item_id == work_item.work_item_id
        }
        if gate is not None:
            artifact_ids.update(gate.artifact_refs)
        return [
            artifact
            for artifact_id in sorted(artifact_ids)
            if (artifact := self.control_plane.artifacts.get(artifact_id)) is not None
        ]

    def _queue_preflight_followups(
        self,
        *,
        work_item: WorkItem,
        failed_skill_ids: Sequence[str],
        artifact_refs: Sequence[str],
        report: DaemonTickReport,
    ) -> None:
        if not failed_skill_ids:
            return
        if work_item.owner in {"qi", "nuo"} or work_item.work_item_id.startswith(
            ("work-qi-preflight-", "work-nuo-preflight-")
        ):
            return
        failed = ", ".join(sorted(set(failed_skill_ids)))
        for owner, item_type, prefix, expected_output in (
            (
                "nuo",
                "repair",
                "work-nuo-preflight",
                (
                    "Classify and repair the failed preflight skills. Treat tool, network, "
                    "auth, timeout, wrapper, and environment problems as system conditions "
                    "first, then decide whether the original work needs rerun."
                ),
            ),
            (
                "qi",
                "governance",
                "work-qi-preflight",
                (
                    "Review this preflight failure as a capability-governance signal. Decide "
                    "whether to keep, merge, modify, delete, or re-promote the trigger/skill."
                ),
            ),
        ):
            if owner not in self.runners_by_owner:
                continue
            followup_id = f"{prefix}-{_slug(work_item.work_item_id)}"
            if followup_id in self.control_plane.work_items:
                continue
            followup = WorkItem(
                work_item_id=followup_id,
                mission_id=work_item.mission_id,
                task_plan_version=work_item.task_plan_version,
                type=item_type,
                owner=owner,
                priority=min(100, work_item.priority + 10),
                idempotency_key=f"{prefix}:{work_item.work_item_id}:{failed}",
                expected_output=f"{expected_output} Failed skills: {failed}.",
                required_capability_refs=list(work_item.required_capability_refs),
                skill_refs=sorted(set(failed_skill_ids)),
                recovery_refs=[work_item.work_item_id, *artifact_refs],
                rollback_refs=list(work_item.rollback_refs),
            )
            self.control_plane.work_items[followup.work_item_id] = followup
            self._persist_work_item(followup)
            report.created_work_item_ids.append(followup.work_item_id)

    def _queue_observation_followups(
        self,
        *,
        mission_id: str,
        observation_artifact_ref: str,
        report: DaemonTickReport,
    ) -> None:
        observation = report.runtime_observations.get(mission_id)
        if observation is None:
            return
        actionable = {"medium", "high", "critical"}
        dedicated_followup_codes = {"preflight_skill_failed"}
        for item in observation.items:
            if item.severity not in actionable:
                continue
            if item.code in dedicated_followup_codes:
                continue
            evidence_refs = _dedupe([observation_artifact_ref, *item.evidence_refs])
            stable_evidence_refs = list(item.evidence_refs) or [item.code]
            evidence_sig = _hash_payload(
                {"code": item.code, "evidence": stable_evidence_refs}
            )[:12]
            qi_expected_output = (
                (
                    "Audit, score, and optimize this failed quality gate. Continue iteration, "
                    "find a better path, turn product gaps into stricter acceptance criteria, "
                    "open a plan-change branch, and require clean retest evidence before final closure. "
                    f"Observation {item.code}: {item.recommended_action}"
                )
                if item.code == "quality_gate_not_passed"
                else (
                    (
                        "Audit this failed work item as an execution-quality incident. Continue "
                        "iteration, find a better path, classify whether the runner/tool path "
                        "failed, and create a strategy change or capability-governance action "
                        f"before the mission can close. Observation {item.code}: {item.recommended_action}"
                    )
                    if item.code == "failed_work_without_recovery"
                    else (
                        (
                        "Audit and govern duplicate runtime capabilities. Merge, dedupe, or "
                        "discard duplicate production defaults, keep the strongest verified "
                        f"profile, and preserve evidence. Observation {item.code}: {item.recommended_action}"
                        )
                        if item.code == "capability_duplicates_collapsed"
                        else (
                            "Audit, score, and optimize this runtime observation. Decide whether "
                            "to merge, dedupe, discard, change plan, or open a better strategy. "
                            f"Observation {item.code}: {item.recommended_action}"
                        )
                    )
                )
            )
            qi_suffix = (
                "strategy_v2-"
                if item.code == "quality_gate_not_passed"
                else "recovery_v1-"
                if item.code == "failed_work_without_recovery"
                else ""
            )
            if "qi" in item.routes:
                self._queue_observation_followup(
                    mission_id=mission_id,
                    owner="qi",
                    item_type="governance",
                    work_item_id=(
                        f"work-qi-observation-{_slug(mission_id)}-"
                        f"{_slug(item.code)}-{qi_suffix}{evidence_sig}"
                    ),
                    expected_output=qi_expected_output,
                    evidence_refs=evidence_refs,
                    report=report,
                )
            if "nuo" in item.routes:
                self._queue_observation_followup(
                    mission_id=mission_id,
                    owner="nuo",
                    item_type="repair",
                    work_item_id=f"work-nuo-observation-{_slug(mission_id)}-{_slug(item.code)}-{evidence_sig}",
                    expected_output=(
                        "Classify this runtime observation before agent scoring. Decide whether "
                        "it is system pollution, environment blockage, state hygiene, or a clean "
                        f"KUN capability failure. Observation {item.code}: {item.recommended_action}"
                    ),
                    evidence_refs=evidence_refs,
                    report=report,
                )

    def _queue_observation_followup(
        self,
        *,
        mission_id: str,
        owner: str,
        item_type: str,
        work_item_id: str,
        expected_output: str,
        evidence_refs: Sequence[str],
        report: DaemonTickReport,
    ) -> None:
        if owner not in self.runners_by_owner:
            return
        if work_item_id in self.control_plane.work_items:
            return
        mission = self.control_plane.missions.get(mission_id)
        work_item = WorkItem(
            work_item_id=work_item_id,
            mission_id=mission_id,
            task_plan_version=mission.current_plan_version if mission else "runtime-observation",
            type=item_type,
            owner=owner,
            priority=90,
            idempotency_key=f"runtime-observation:{owner}:{work_item_id}",
            expected_output=expected_output,
            recovery_refs=list(evidence_refs),
        )
        self.control_plane.work_items[work_item.work_item_id] = work_item
        self._persist_work_item(work_item)
        report.created_work_item_ids.append(work_item.work_item_id)
        report.observation_followup_ids.append(work_item.work_item_id)

    def _finalize_idle_mission(self, *, mission_id: str, report: DaemonTickReport) -> None:
        """Let capable runners close mission-level delivery artifacts when ready."""

        seen_runner_ids: set[int] = set()
        for runner in (
            *self.runners_by_owner.values(),
            *self.runners_by_type.values(),
            self.default_runner,
        ):
            if runner is None:
                continue
            runner_id = id(runner)
            if runner_id in seen_runner_ids:
                continue
            seen_runner_ids.add(runner_id)
            finalize = getattr(runner, "finalize_mission", None)
            if not callable(finalize):
                continue
            payload = finalize(mission_id)
            if not isinstance(payload, Mapping) or not payload.get("finalized"):
                continue
            if mission_id not in report.finalized_mission_ids:
                report.finalized_mission_ids.append(mission_id)
            final_gate_ref = payload.get("final_gate_ref")
            if isinstance(final_gate_ref, str) and final_gate_ref not in report.final_gate_refs:
                report.final_gate_refs.append(final_gate_ref)
            delivery_manifest_ref = payload.get("delivery_manifest_ref")
            if (
                isinstance(delivery_manifest_ref, str)
                and delivery_manifest_ref not in report.delivery_manifest_refs
            ):
                report.delivery_manifest_refs.append(delivery_manifest_ref)
            return

    def _transition_to_queued(self, mission_id: str, *, subject_ref: str) -> None:
        mission = self.control_plane.missions[mission_id]
        if mission.status == "queued":
            return
        if mission.status in {"repairing", "retrying", "blocked", "paused", "changing_plan"}:
            self.control_plane.transition_mission(
                mission_id=mission_id,
                target="queued",
                actor=self.daemon_id,
                reason="daemon prepared recovery work for automatic resume",
                subject_ref=subject_ref,
            )

    def _progress_artifact(
        self,
        *,
        mission_id: str,
        now: datetime,
        capability_policy: CapabilityExecutionPolicy,
    ) -> ArtifactRecord:
        progress = self.control_plane.progress_report(mission_id)
        payload = progress.model_dump(mode="json")
        payload["daemon_id"] = self.daemon_id
        payload["observed_at"] = now.isoformat()
        payload["capability_policy"] = capability_policy.model_dump(mode="json")
        return ArtifactRecord(
            artifact_id=f"artifact-daemon-progress-{mission_id}-{_compact_time(now)}",
            kind="report",
            path_or_uri=f"control-plane://daemon/{self.daemon_id}/{mission_id}/progress/{now.isoformat()}",
            content_hash=_hash_payload(payload),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "daemon_progress",
                "mission_dashboard",
                "persistence_recovery",
                "capability_execution_policy",
                *capability_policy.capability_profile_refs,
            ],
            freshness="fresh",
            source_quality="primary",
        )

    def _observation_artifact(
        self,
        *,
        mission_id: str,
        now: datetime,
        report: DaemonTickReport,
        capability_policy: CapabilityExecutionPolicy,
    ) -> ArtifactRecord:
        observation = build_runtime_observation_report(
            control_plane=self.control_plane,
            mission_id=mission_id,
            tick_report=report,
            capability_policy=capability_policy,
        )
        report.runtime_observations[mission_id] = observation
        payload = observation.model_dump(mode="json")
        supports = [
            "runtime_observation",
            "external_supervision",
            "qi_nuo_iteration",
            f"observation_severity:{observation.max_severity}",
            *[f"observation:{item.code}" for item in observation.items],
            *[f"observation_route:{route}" for route in _observation_routes(observation)],
        ]
        if observation.requires_external_supervision:
            supports.append("requires_external_supervision")
        return ArtifactRecord(
            artifact_id=f"artifact-runtime-observation-{mission_id}-{_compact_time(now)}",
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"observation/{now.isoformat()}"
            ),
            content_hash=_hash_payload(payload),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=supports,
            freshness="fresh",
            source_quality="primary",
        )

    def _upsert_artifact(self, artifact: ArtifactRecord) -> None:
        self.control_plane.artifacts[artifact.artifact_id] = artifact
        if self.control_plane.store is not None:
            self.control_plane.store.put_artifact_record(artifact)

    def _persist_work_item(self, work_item: WorkItem) -> None:
        if self.control_plane.store is not None:
            self.control_plane.store.put_work_item(work_item)

    def _persist_collaboration_ticket(self, ticket: CollaborationTicket) -> None:
        if self.control_plane.store is not None:
            self.control_plane.store.put_collaboration_ticket(ticket)

    def _persist_capability_profile(self, profile: CapabilityProfile) -> None:
        if self.control_plane.store is not None:
            self.control_plane.store.put_capability_profile(profile)

    def _save_service_state(
        self,
        state_store: FileDaemonServiceStateStore | None,
        state: DaemonServiceState,
    ) -> None:
        if state_store is not None:
            state_store.save(state)


def _bind_capability_policy(
    runner: ControlPlaneRunner,
    capability_policy: CapabilityExecutionPolicy,
) -> None:
    bind = getattr(runner, "bind_capability_execution_policy", None)
    if callable(bind):
        bind(capability_policy)


def _should_preflight_work_item(work_item: WorkItem) -> bool:
    """Return whether the daemon should run skill preflight before this item."""

    if work_item.owner in {"qi", "nuo"}:
        return False
    return not work_item.work_item_id.startswith(
        ("work-qi-preflight-", "work-nuo-preflight-", "work-qi-runtime-learning-")
    )


class _WorkspaceRollbackRunner:
    runner_type: Literal["tool"] = "tool"

    def __init__(self, control_plane: InMemoryControlPlane, daemon_id: str) -> None:
        self.control_plane = control_plane
        self.runner_identity = f"{daemon_id}:workspace-rollback"

    def run(self, work_item: WorkItem) -> WorkItemResult:
        snapshot = self._snapshot_artifact(work_item)
        if snapshot is None:
            return WorkItemResult(
                status="failed",
                summary="No restorable workspace snapshot was found for rollback.",
                failure_category="tool_failure",
            )
        try:
            restored = restore_workspace_snapshot(snapshot)
        except Exception as exc:
            return WorkItemResult(
                status="failed",
                summary=f"Workspace rollback failed: {type(exc).__name__}: {exc}",
                failure_category="tool_failure",
            )
        payload = restored.model_dump(mode="json")
        artifact = ArtifactRecord(
            artifact_id=f"artifact-rollback-restore-{_slug(work_item.work_item_id)}-{_compact_time(_now())}",
            kind="report",
            path_or_uri=f"control-plane://rollback/{work_item.mission_id}/{work_item.work_item_id}",
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "workspace_restore",
                "rollback_executed",
                snapshot.artifact_id,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        return WorkItemResult(
            status="done",
            summary=(
                "Workspace rollback restored "
                f"{restored.restored_file_count} files and removed "
                f"{restored.removed_extra_file_count} extra files."
            ),
            artifacts=[artifact],
        )

    def _snapshot_artifact(self, work_item: WorkItem) -> ArtifactRecord | None:
        for artifact_ref in work_item.rollback_refs:
            artifact = self.control_plane.artifacts.get(artifact_ref)
            if artifact is None:
                continue
            if artifact.kind == "snapshot" and (
                "workspace_snapshot" in artifact.supports
                or "restore_mode:file_copy" in artifact.supports
            ):
                return artifact
        return None


class _SandboxRequirementRunner:
    runner_type: Literal["tool"] = "tool"

    def __init__(self, *, runner_identity: str, sandbox_spec: SandboxIsolationSpec) -> None:
        self.runner_identity = f"{runner_identity}:sandbox-requirement"
        self.sandbox_spec = sandbox_spec

    def run(self, work_item: WorkItem) -> WorkItemResult:
        payload = {
            "work_item_id": work_item.work_item_id,
            "mission_id": work_item.mission_id,
            "required_sandbox_mode": self.sandbox_spec.mode,
            "container_runtime": self.sandbox_spec.container_runtime,
            "sandbox_ref": self.sandbox_spec.sandbox_ref,
            "reason": (
                "The work item requires container isolation, but the selected runner "
                "does not declare container sandbox support."
            ),
        }
        artifact = ArtifactRecord(
            artifact_id=(
                "artifact-sandbox-required-"
                f"{_slug(work_item.work_item_id)}-{_compact_time(_now())}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://sandbox-required/{work_item.mission_id}/"
                f"{work_item.work_item_id}"
            ),
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "container_sandbox_required",
                "sandbox_blocked",
                self.sandbox_spec.sandbox_ref,
                f"sandbox_mode:{self.sandbox_spec.mode}",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        return WorkItemResult(
            status="failed",
            summary=(
                "Container sandbox is required before this work item may run; "
                "selected runner did not declare container sandbox support."
            ),
            artifacts=[artifact],
            failure_category="environment_failure",
        )


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _write_json_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    os.replace(temp_name, path)


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _compact_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"


def _info_gap_reason(
    *,
    mission: Mission,
    plan: TaskPlan | None,
    info_gaps: Sequence[str],
) -> str:
    if info_gaps:
        missing = "; ".join(info_gaps[:6])
    elif plan is not None and plan.unknowns:
        missing = "; ".join(plan.unknowns[:6])
    else:
        missing = "the mission is marked info_gap but does not yet have a resolved plan"
    return (
        "KUN must clarify missing information before producing or executing the task plan. "
        f"Mission: {mission.objective}. Missing: {missing}"
    )


def _latest_delivery_manifest_ref(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> str | None:
    for manifest_ref in reversed(mission.artifact_manifest_refs):
        manifest = control_plane.artifact_manifests.get(manifest_ref)
        if manifest is None:
            continue
        if manifest.kind == "delivery" and manifest.supports_delivery:
            return manifest_ref
    return None


def _observation_routes(report: RuntimeObservationReport) -> list[str]:
    seen: set[str] = set()
    routes: list[str] = []
    for item in report.items:
        for route in item.routes:
            if route in seen:
                continue
            seen.add(route)
            routes.append(route)
    return routes


def _tick_is_idle(report: DaemonTickReport) -> bool:
    return not (
        report.recovered_work_item_ids
        or report.retired_work_item_ids
        or report.retired_collaboration_ticket_ids
        or report.retired_capability_profile_ids
        or report.recovery_gate_refs
        or report.created_work_item_ids
        or report.observation_followup_ids
        or report.created_collaboration_ticket_ids
        or report.ran_work_item_ids
        or report.resource_lock_skipped_work_item_ids
        or report.run_refs
        or report.no_runner_work_item_ids
        or report.finalized_mission_ids
        or report.final_gate_refs
        or report.delivery_manifest_refs
        or report.watchtower_fired_rule_ids
    )


def _slot_for_run(slots: list[WorkerSlotSnapshot], run_index: int) -> WorkerSlotSnapshot:
    if not slots:
        raise ValueError("worker pool must contain at least one slot")
    return slots[run_index % len(slots)]


def _lease_id(
    *,
    daemon_id: str,
    worker_id: str,
    work_item_id: str,
    observed_at: datetime,
) -> str:
    return (
        f"lease:{_slug(daemon_id)}:{_slug(worker_id)}:"
        f"{_slug(work_item_id)}:{_compact_time(observed_at)}"
    )


def _default_resource_lock_store(
    control_plane: InMemoryControlPlane,
) -> FileResourceLockStore | InMemoryResourceLockStore:
    store_path = getattr(control_plane.store, "path", None)
    if isinstance(store_path, Path):
        return FileResourceLockStore(store_path.with_name(f"{store_path.stem}.resource-locks.json"))
    if isinstance(store_path, str):
        path = Path(store_path)
        return FileResourceLockStore(path.with_name(f"{path.stem}.resource-locks.json"))
    return InMemoryResourceLockStore()


def _is_delivery_state_followup(work_item: WorkItem) -> bool:
    if work_item.owner not in {"qi", "nuo", "control-plane"}:
        return False
    return work_item.work_item_id.startswith(
        (
            "work-qi-observation-",
            "work-nuo-observation-",
            "work-qi-preflight-",
            "work-nuo-preflight-",
        )
    )


def _effective_resource_locks(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> set[str]:
    locks = set(work_item.resource_locks)
    if (
        work_item.owner in {"qi", "nuo"}
        or work_item.type in {"governance", "repair", "rollback", "merge"}
        or "plan-change" in work_item.work_item_id
    ):
        locks.add(f"mission-state:{work_item.mission_id}")
    workspace_ref = work_item.workspace_ref
    if workspace_ref:
        locks.add(f"workspace:{workspace_ref}")
    mission = control_plane.missions.get(work_item.mission_id)
    contract = control_plane.contracts.get(mission.execution_contract_ref or "") if mission else None
    workspace_path = _workspace_path_from_contract(contract)
    if workspace_path and work_item.type in {
        "execution",
        "test",
        "merge",
        "repair",
        "retest",
        "rollback",
    }:
        locks.add(f"workspace:{workspace_path}")
    return locks


def _workspace_path_from_contract(contract: ExecutionContract | None) -> str | None:
    if contract is None:
        return None
    for payload in (contract.delivery_contract, contract.risk_policy, contract.rollback_policy):
        for key in (
            "workspace_path",
            "project_path",
            "repo_path",
            "target_path",
            "output_dir",
            "delivery_path",
        ):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value.strip():
                return value
    return None


def _merge_unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def _service_state_from_tick(
    *,
    daemon_id: str,
    started_at: datetime,
    report: DaemonTickReport,
    status: DaemonServiceStatus,
    tick_count: int,
    consecutive_idle_ticks: int,
    next_wakeup_at: datetime,
) -> DaemonServiceState:
    return DaemonServiceState(
        daemon_id=daemon_id,
        status=status,
        started_at=started_at,
        updated_at=report.observed_at,
        process_id=os.getpid(),
        tick_count=tick_count,
        consecutive_idle_ticks=consecutive_idle_ticks,
        active_mission_ids=list(report.mission_ids),
        last_heartbeat_at=report.observed_at,
        next_wakeup_at=next_wakeup_at,
        last_tick_observed_at=report.observed_at,
        last_tick_ran_work_item_ids=list(report.ran_work_item_ids),
        last_tick_recovered_work_item_ids=list(report.recovered_work_item_ids),
        last_tick_progress_artifact_refs=list(report.progress_artifact_refs),
        worker_pool_size=len(report.worker_slots) or 1,
        last_tick_worker_slots=list(report.worker_slots),
        last_tick_resource_lock_skipped_work_item_ids=list(
            report.resource_lock_skipped_work_item_ids
        ),
        last_tick_resource_lock_conflicts=list(report.resource_lock_conflicts),
        last_tick_sandbox_specs=list(report.sandbox_specs),
    )


__all__ = [
    "ACTIVE_DAEMON_MISSION_STATUSES",
    "ControlPlaneDaemon",
    "DaemonLoopReport",
    "DaemonServiceClaim",
    "DaemonServiceConfig",
    "DaemonServiceState",
    "DaemonServiceStatus",
    "DaemonServiceStopRequest",
    "DaemonTickReport",
    "FileDaemonServiceStateStore",
]
