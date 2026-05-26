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
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.activation import activate_work_item_features
from kun.control_plane.capability_execution import (
    CapabilityExecutionPolicy,
    build_capability_execution_policy,
    ensure_policy_covers_required_capabilities,
)
from kun.control_plane.concurrency import (
    FileResourceLockStore,
    InMemoryResourceLockStore,
    RedisResourceLockStore,
    ResourceLockBackend,
    ResourceLockConflict,
    SandboxIsolationMode,
    SandboxIsolationSpec,
    SQLiteResourceLockStore,
    WorkerPoolConfig,
    WorkerSlotSnapshot,
    is_pure_governance_work_item,
    is_workspace_resource_lock_ref,
    normalize_resource_lock_ref,
    sandbox_spec_for_work_item,
    work_item_requires_workspace_boundary,
    worker_slots,
)
from kun.control_plane.mission_director import MISSION_DIRECTOR_OWNER
from kun.control_plane.preflight import WorkItemPreflight, run_work_item_preflight
from kun.control_plane.rainflow_ad_mission import RAINFLOW_AD_PRODUCTION_MODE
from kun.control_plane.runtime import ControlPlaneRunner, InMemoryControlPlane, WorkItemResult
from kun.control_plane.runtime_observation import (
    RuntimeObservationItem,
    RuntimeObservationReport,
    build_runtime_observation_report,
)
from kun.control_plane.self_improvement import (
    SELF_IMPROVEMENT_AUDIT_SUPPORT,
    build_self_improvement_audit_work_item,
    self_improvement_audit_signature,
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

_ACCEPTANCE_REWORK_SUFFIX_RE = re.compile(r"(?:-acceptance-rework-[0-9a-f]{8})+$")
_GAME_REWORK_BRANCH_RE = re.compile(r"^work-game-rework-.+-([0-9a-f]{12})-\d{2}-")

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
    resource_lock_backend: ResourceLockBackend = "file"
    resource_lock_redis_url: str | None = None
    resource_lock_ttl_sec: float = Field(default=900.0, gt=0)
    sandbox_mode: SandboxIsolationMode = "workspace_snapshot"
    container_runtime: str | None = None
    max_ticks: int | None = Field(default=None, ge=1)
    stop_when_idle: bool = False
    idle_ticks_to_stop: int = Field(default=1, ge=1)
    stale_heartbeat_after_sec: float = Field(default=900.0, gt=0)
    worker_heartbeat_interval_sec: float = Field(default=30.0, gt=0)


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
    resource_lock_backend: ResourceLockBackend = "file"
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

    def clear_stop_request(self, *, daemon_id: str | None = None) -> bool:
        """Clear a stop request after an operator explicitly allows restart."""

        request = self.load_stop_request()
        if request is None:
            return False
        if daemon_id is not None and request.daemon_id != daemon_id:
            return False
        self.stop_request_path.unlink(missing_ok=True)
        return True

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
            worker_pool_size=active_config.worker_pool_size,
            resource_lock_backend=active_config.resource_lock_backend,
        )
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
        resource_lock_store: (
            FileResourceLockStore
            | SQLiteResourceLockStore
            | RedisResourceLockStore
            | InMemoryResourceLockStore
            | None
        ) = None,
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
        self.resource_lock_store = resource_lock_store or _default_resource_lock_store(
            control_plane
        )
        self.resource_lock_backend: ResourceLockBackend = _resource_lock_backend(
            self.resource_lock_store
        )
        self.resource_lock_ttl = timedelta(seconds=resource_lock_ttl_sec)
        self.sandbox_mode = sandbox_mode
        self.container_runtime = container_runtime
        # Track the store state seen at construction.  Subsequent ticks refresh
        # only when another process changes the shared store, which preserves
        # in-memory mission/contract edits made by the current process before
        # the first wakeup.
        self._store_refresh_signature: tuple[str, int, int] | None = (
            getattr(self.control_plane.store, "loaded_signature", None)
            or self._current_store_signature()
        )
        self._service_state_store: FileDaemonServiceStateStore | None = None
        self._service_started_at: datetime | None = None
        self._service_tick_count = 0
        self._service_consecutive_idle_ticks = 0
        self._service_active_mission_ids: list[str] = []
        self._service_worker_slots: dict[str, WorkerSlotSnapshot] = {}
        self._service_worker_heartbeat_interval_sec = 30.0
        self._service_state_lock = threading.RLock()

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
        if self._should_refresh_shared_work_queue(
            mission_ids=mission_ids,
            observed_at=observed_at,
        ):
            self.control_plane.refresh_from_store()
            self._store_refresh_signature = self._current_store_signature()
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
        self._release_orphaned_resource_locks(now=observed_at)

        for mission_id in selected_mission_ids:
            self._ensure_info_gap_collaboration(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._resume_stale_info_gap_mission(
                mission_id=mission_id,
                observed_at=observed_at,
            )
            self._resume_waiting_product_mission_with_ready_current_work(
                mission_id=mission_id,
                observed_at=observed_at,
            )
            self._ensure_delivery_acceptance_collaboration(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_player_review_collaboration_from_gate(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_open_acceptance_keeps_product_pressure(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_acceptance_rework_from_feedback(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_mission_director_review(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_self_improvement_audit(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_mission_director_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_terminal_followup_only_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._restore_cancelled_rainflow_phase1_acceptance_reviews(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_plan_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_product_gate_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_failed_gate(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_cancelled_dependency(
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

        for mission_id in selected_mission_ids:
            self._retire_stale_quality_followups_before_running(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )

        for mission_id in selected_mission_ids:
            observation = build_runtime_observation_report(
                control_plane=self.control_plane,
                mission_id=mission_id,
                tick_report=report,
                capability_policy=capability_policy,
            )
            report.runtime_observations[mission_id] = observation

        remaining = max_work_items
        while remaining > 0:
            claimed_resource_locks: set[str] = set()
            deferred_work_item_ids: set[str] = set()
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
                    mission_state_requires_followup = _mission_state_requires_followup_only_work(
                        mission.status
                    )
                    if (
                        mission.status == "waiting_human"
                        and mission_state_requires_followup
                        and not _has_ready_state_preserving_followup(
                            self.control_plane,
                            mission.mission_id,
                            now=report.observed_at,
                        )
                        and _has_ready_current_plan_product_work(
                            self.control_plane,
                            mission,
                            now=report.observed_at,
                        )
                    ):
                        mission_state_requires_followup = False
                    work_item = self._next_non_conflicting_ready_work_item(
                        mission_id=mission_id,
                        claimed_resource_locks=claimed_resource_locks,
                        excluded_work_item_ids=deferred_work_item_ids,
                        followup_only_state=mission_state_requires_followup,
                        report=report,
                    )
                    if work_item is None:
                        continue
                    slot = _slot_for_run(report.worker_slots, len(prepared_runs))
                    created_ticket_count = len(report.created_collaboration_ticket_ids)
                    prepared = self._prepare_work_item_run(
                        work_item=work_item,
                        slot=slot,
                        preserve_mission_status=mission_state_requires_followup,
                        claimed_resource_locks=claimed_resource_locks,
                        capability_policy=capability_policy,
                        observed_at=observed_at,
                        report=report,
                    )
                    if prepared is None:
                        deferred_work_item_ids.add(work_item.work_item_id)
                        if (
                            work_item.work_item_id in report.no_runner_work_item_ids
                            or len(report.created_collaboration_ticket_ids) > created_ticket_count
                            or _is_human_collaboration_work_item(work_item)
                        ):
                            selected_this_pass = True
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
            self._finalize_idle_mission(
                mission_id=mission_id,
                report=report,
                capability_policy=capability_policy,
            )

        for mission_id in selected_mission_ids:
            mission = self.control_plane.missions.get(mission_id)
            contract = (
                self.control_plane.contracts.get(mission.execution_contract_ref or "")
                if mission is not None
                else None
            )
            self._ensure_delivery_acceptance_collaboration(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_player_review_collaboration_from_gate(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_plan_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_failed_gate(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_cancelled_dependency(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            if not _should_continue_product_pressure_while_awaiting_acceptance(contract):
                continue
            self._ensure_open_acceptance_keeps_product_pressure(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._ensure_acceptance_rework_from_feedback(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_plan_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_superseded_product_gate_work(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_failed_gate(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )
            self._retire_downstream_product_work_after_cancelled_dependency(
                mission_id=mission_id,
                observed_at=observed_at,
                report=report,
            )

        if write_progress:
            store = self.control_plane.store
            if store is None:
                self._write_progress_artifacts(
                    mission_ids=selected_mission_ids,
                    observed_at=observed_at,
                    report=report,
                    capability_policy=capability_policy,
                )
            else:
                with store.transaction():
                    self._write_progress_artifacts(
                        mission_ids=selected_mission_ids,
                        observed_at=observed_at,
                        report=report,
                        capability_policy=capability_policy,
                    )
        self._store_refresh_signature = self._current_store_signature()
        return report

    def _write_progress_artifacts(
        self,
        *,
        mission_ids: Sequence[str],
        observed_at: datetime,
        report: DaemonTickReport,
        capability_policy: CapabilityExecutionPolicy,
    ) -> None:
        """Persist per-tick progress under one store transaction when available."""

        for mission_id in mission_ids:
            observation = self._observation_artifact(
                mission_id=mission_id,
                now=observed_at,
                report=report,
                capability_policy=capability_policy,
            )
            self._upsert_artifact(observation)
            report.observation_artifact_refs.append(observation.artifact_id)
            self._retire_resolved_observation_followups(
                mission_id=mission_id,
                observation=report.runtime_observations[mission_id],
                observed_at=observed_at,
                report=report,
            )
            self._queue_observation_followups(
                mission_id=mission_id,
                observation_artifact_ref=observation.artifact_id,
                report=report,
            )
            self._recover_stale_waiting_human_nuo_clean_retests(
                mission_id=mission_id,
                report=report,
            )
            self._queue_nuo_clean_retests_for_partial_recoveries(
                mission_id=mission_id,
                observation_artifact_ref=observation.artifact_id,
                report=report,
            )
            self._queue_nuo_clean_retests_for_blocked_current_plan_work(
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
        if active_config.resource_lock_backend == "redis":
            if active_config.resource_lock_redis_url:
                self.resource_lock_store = RedisResourceLockStore(
                    active_config.resource_lock_redis_url
                )
            elif not isinstance(self.resource_lock_store, RedisResourceLockStore):
                raise ValueError("resource_lock_redis_url is required for redis lock backend")
        elif active_config.resource_lock_backend != _resource_lock_backend(
            self.resource_lock_store
        ):
            self.resource_lock_store = _default_resource_lock_store(
                self.control_plane,
                backend=active_config.resource_lock_backend,
            )
        self.resource_lock_backend = _resource_lock_backend(self.resource_lock_store)
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
                return DaemonLoopReport(
                    daemon_id=self.daemon_id,
                    started_at=started_at,
                    ended_at=started_at,
                    tick_count=0,
                    stopped_reason="idle",
                    tick_reports=[],
                )
            else:
                self._save_service_state(
                    state_store,
                    DaemonServiceState(
                        daemon_id=self.daemon_id,
                        status="starting",
                        started_at=started_at,
                        updated_at=started_at,
                        process_id=os.getpid(),
                        worker_pool_size=self.worker_pool.worker_count,
                        resource_lock_backend=self.resource_lock_backend,
                    ),
                )
            self._bind_managed_service_context(
                state_store=state_store,
                started_at=started_at,
                mission_ids=list(mission_ids or self._active_missions()),
                worker_heartbeat_interval_sec=active_config.worker_heartbeat_interval_sec,
            )
        stopped_reason: DaemonServiceStoppedReason = "max_ticks"
        try:
            while True:
                if stop_requested is not None and stop_requested():
                    stopped_reason = "stop_requested"
                    break
                observed_at = now_factory()
                self._service_tick_count = len(tick_reports) + 1
                self._save_managed_service_heartbeat(
                    status="running",
                    observed_at=observed_at,
                    tick_count=self._service_tick_count,
                    consecutive_idle_ticks=idle_ticks,
                )
                report = self.tick_once(
                    mission_ids=mission_ids,
                    now=observed_at,
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
                        resource_lock_backend=self.resource_lock_backend,
                    ),
                )
                self._service_tick_count = len(tick_reports)
                self._service_consecutive_idle_ticks = idle_ticks
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
                    resource_lock_backend=self.resource_lock_backend,
                    stopped_reason="error",
                    stopped_at=stopped_at,
                    last_error=f"{type(exc).__name__}: {exc}",
                ),
            )
            self._clear_managed_service_context()
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
                last_heartbeat_at=ended_at,
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
                resource_lock_backend=self.resource_lock_backend,
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
        self._clear_managed_service_context()
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

    def _should_refresh_shared_work_queue(
        self,
        *,
        mission_ids: Sequence[str] | None,
        observed_at: datetime,
    ) -> bool:
        if self.control_plane.store is None:
            return False
        _ = (mission_ids, observed_at)
        signature = self._current_store_signature()
        if signature is None:
            return True
        return signature != self._store_refresh_signature

    def _current_store_signature(self) -> tuple[str, int, int] | None:
        store = self.control_plane.store
        if store is None:
            return None
        path = getattr(store, "path", None)
        if path is None:
            return None
        store_path = Path(path)
        try:
            stat = store_path.stat()
        except OSError:
            return (str(store_path), -1, -1)
        return (str(store_path), stat.st_mtime_ns, stat.st_size)

    def _next_non_conflicting_ready_work_item(
        self,
        *,
        mission_id: str,
        claimed_resource_locks: set[str],
        excluded_work_item_ids: set[str] | None = None,
        followup_only_state: bool = False,
        report: DaemonTickReport,
    ) -> WorkItem | None:
        excluded = excluded_work_item_ids or set()
        ready_items = _prioritize_ready_work_items_for_daemon(
            self.control_plane,
            mission_id=mission_id,
            ready_items=self.control_plane.ready_work_items(
                mission_id,
                now=report.observed_at,
            ),
        )
        for candidate in ready_items:
            if candidate.work_item_id in excluded:
                continue
            if candidate.work_item_id in report.no_runner_work_item_ids:
                continue
            if followup_only_state and not _is_state_preserving_followup(candidate):
                continue
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
        preserve_mission_status: bool,
        claimed_resource_locks: set[str],
        capability_policy: CapabilityExecutionPolicy,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> _PreparedWorkItemRun | None:
        runner = self._runner_for(work_item)
        if runner is None:
            if _is_human_collaboration_work_item(work_item):
                self._ensure_collaboration_ticket_for_work_item(
                    work_item=work_item,
                    observed_at=observed_at,
                    report=report,
                )
                return None
            if work_item.work_item_id not in report.no_runner_work_item_ids:
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
        work_item_capability_policy = ensure_policy_covers_required_capabilities(
            capability_policy,
            claimed_item.required_capability_refs,
            work_item_id=claimed_item.work_item_id,
        )
        activation = activate_work_item_features(
            control_plane=self.control_plane,
            work_item=claimed_item,
            capability_policy=work_item_capability_policy,
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
        if preflight.failed_skill_ids:
            active_runner = _PreflightBlockedRunner(
                runner_identity=active_runner.runner_identity,
                failed_skill_ids=preflight.failed_skill_ids,
                artifact_refs=preflight_artifact_refs,
            )
        _bind_capability_policy(active_runner, work_item_capability_policy)
        return _PreparedWorkItemRun(
            work_item=activation.work_item,
            runner=active_runner,
            slot=slot,
            holder_id=holder_id,
            preserve_mission_status=preserve_mission_status
            and _is_state_preserving_followup(activation.work_item),
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
                try:
                    run = future.result()
                except Exception:  # pragma: no cover - defensive worker isolation.
                    self._release_claimed_work_item_lease(
                        work_item_id=prepared.work_item.work_item_id,
                        lease=prepared.holder_id,
                    )
                    self.resource_lock_store.release_holder(prepared.holder_id, now=_now())
                    prepared.slot.status = "idle"
                    continue
                if run is not None:
                    completed.append((prepared, run))
        return sorted(completed, key=lambda pair: pair[1].started_at)

    def _execute_prepared_work_item(
        self,
        prepared: _PreparedWorkItemRun,
    ) -> RunRecord | None:
        stop_worker_heartbeat: Callable[[], None] | None = None
        try:
            try:
                started = self.control_plane.start_work_item_run(
                    work_item_id=prepared.work_item.work_item_id,
                    runner=prepared.runner,
                    preserve_mission_status=prepared.preserve_mission_status,
                    lease=prepared.holder_id,
                )
            except Exception:
                self._release_claimed_work_item_lease(
                    work_item_id=prepared.work_item.work_item_id,
                    lease=prepared.holder_id,
                )
                return None
            if started is None:
                self._release_claimed_work_item_lease(
                    work_item_id=prepared.work_item.work_item_id,
                    lease=prepared.holder_id,
                )
                return None
            run, running_item = started
            stop_worker_heartbeat = self._start_managed_worker_heartbeat(prepared.slot)
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
            if stop_worker_heartbeat is not None:
                stop_worker_heartbeat()
            self.resource_lock_store.release_holder(prepared.holder_id, now=_now())
            prepared.slot.status = "idle"

    def _release_claimed_work_item_lease(self, *, work_item_id: str, lease: str) -> None:
        release = getattr(self.control_plane.store, "release_work_item_lease", None)
        if callable(release):
            released = release(work_item_id=work_item_id, lease=lease)
            if released is not None:
                self.control_plane.work_items[released.work_item_id] = released
            return
        work_item = self.control_plane.work_items.get(work_item_id)
        if work_item is None or work_item.lease != lease or work_item.status != "queued":
            return
        released = work_item.model_copy(update={"lease": None, "heartbeat": None, "timeout": None})
        self.control_plane.work_items[released.work_item_id] = released
        self._persist_work_item(released)

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
        info_gaps = list(plan.info_gaps or plan.unknowns) if plan is not None else []
        if not info_gaps:
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

    def _resume_stale_info_gap_mission(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
    ) -> None:
        """Resume execution when an old info-gap state no longer has a real blocker."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.status != "info_gap":
            return
        plan = self._current_task_plan(mission)
        info_gaps = list(plan.info_gaps or plan.unknowns) if plan is not None else []
        if info_gaps:
            return
        has_open_ticket = any(
            ticket.mission_id == mission_id and ticket.status in {"open", "waiting", "escalated"}
            for ticket in self.control_plane.collaboration_tickets.values()
        )
        if has_open_ticket:
            return
        ready_items = self.control_plane.ready_work_items(mission_id, now=observed_at)
        has_state_preserving_followup = any(
            _is_state_preserving_followup(item) for item in ready_items
        )
        if has_state_preserving_followup:
            return
        has_product_work = any(not _is_state_preserving_followup(item) for item in ready_items)
        if not has_product_work:
            return
        self.control_plane.transition_mission(
            mission_id=mission_id,
            target="changing_plan",
            actor=self.daemon_id,
            reason="daemon resumed stale info_gap state after tickets and plan gaps were cleared",
            subject_ref=plan.plan_id if plan is not None else mission_id,
        )
        self.control_plane.transition_mission(
            mission_id=mission_id,
            target="queued",
            actor=self.daemon_id,
            reason="daemon queued cleared info-gap mission so ready work can continue",
            subject_ref=plan.plan_id if plan is not None else mission_id,
        )

    def _resume_waiting_product_mission_with_ready_current_work(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
    ) -> None:
        """Do not let a human-wait state hide unfinished current-plan product work."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        if mission.status not in {"awaiting_acceptance", "waiting_human"}:
            return
        if mission.acceptance_ref is not None:
            return
        ready_product_work = _ready_current_plan_product_work(
            self.control_plane,
            mission,
            now=observed_at,
        )
        if not ready_product_work:
            return
        ready_items = self.control_plane.ready_work_items(mission_id, now=observed_at)
        if any(_is_state_preserving_followup(item) for item in ready_items):
            return
        self._transition_to_queued(
            mission_id,
            subject_ref=ready_product_work[0].work_item_id,
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
        if mission.acceptance_ref is not None:
            return
        delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission)
        if delivery_manifest_ref is None:
            return
        if mission.status not in {"delivering", "awaiting_acceptance"} and not (
            mission.status == "running"
            and not _has_ready_or_active_current_plan_work(self.control_plane, mission)
        ):
            return
        existing_open_ticket = _latest_open_acceptance_ticket(self.control_plane, mission)
        if existing_open_ticket is not None:
            if mission.status in {"running", "delivering"}:
                _transition_product_delivery_to_awaiting_acceptance(
                    self.control_plane,
                    mission_id=mission.mission_id,
                    actor=self.daemon_id,
                    reason="daemon found completed product delivery waiting for existing acceptance ticket",
                    subject_ref=existing_open_ticket.ticket_id,
                )
            return
        delivery_iteration_ref = _delivery_acceptance_iteration_ref(
            self.control_plane,
            mission=mission,
            delivery_manifest_ref=delivery_manifest_ref,
        )
        ticket_id = _acceptance_ticket_id(
            mission.mission_id,
            delivery_manifest_ref,
            delivery_iteration_ref=delivery_iteration_ref,
        )
        if ticket_id in self.control_plane.collaboration_tickets:
            if mission.status in {"running", "delivering"}:
                _transition_product_delivery_to_awaiting_acceptance(
                    self.control_plane,
                    mission_id=mission.mission_id,
                    actor=self.daemon_id,
                    reason=(
                        "daemon found completed product delivery with an answered acceptance "
                        "ticket for the current delivery iteration"
                    ),
                    subject_ref=ticket_id,
                )
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
        if mission.status in {"delivering", "running"}:
            _transition_product_delivery_to_awaiting_acceptance(
                self.control_plane,
                mission_id=mission.mission_id,
                actor=self.daemon_id,
                reason="daemon opened a human product-acceptance ticket for subjective validation",
                subject_ref=ticket.ticket_id,
            )

    def _ensure_player_review_collaboration_from_gate(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        if mission.acceptance_ref is not None:
            return
        gate = _latest_human_player_review_only_gate(self.control_plane, mission)
        if gate is None:
            return
        ticket_id = _player_review_ticket_id(
            mission_id=mission.mission_id,
            gate_id=gate.gate_evaluation_id,
        )
        existing = self.control_plane.collaboration_tickets.get(ticket_id)
        if existing is not None:
            if existing.status not in {"open", "waiting", "escalated"}:
                existing = existing.model_copy(
                    update={
                        "status": "open",
                        "resolution_refs": _dedupe(
                            [*existing.resolution_refs, gate.gate_evaluation_id]
                        ),
                    }
                )
                self.control_plane.collaboration_tickets[existing.ticket_id] = existing
                self._persist_collaboration_ticket(existing)
            self._move_mission_to_waiting_human_for_player_review(
                mission_id=mission.mission_id,
                subject_ref=existing.ticket_id,
            )
            return
        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission.mission_id,
            type="review",
            role_needed=f"{mission.owner or 'mission-owner'}-or-target-player-reviewer",
            why_needed=(
                "KUN has reached the point where automated game/product gates need a fresh "
                "human or target-player playtest. Missing player-feel approval is not a "
                "code repair signal by itself."
            ),
            context_ref=gate.gate_evaluation_id,
            risk_if_skipped=(
                "The daemon may keep opening mechanical acceptance-rework branches instead "
                "of resolving whether the current game actually feels good enough to a real player."
            ),
            deadline=observed_at + timedelta(hours=24),
            sla_policy={"reminder_after_hours": 6, "escalate_after_hours": 24},
            escalation_policy={
                "after_deadline": (
                    "keep waiting_human or explicitly run low-risk polish; do not mark final"
                )
            },
            fallback_policy={
                "allowed": False,
                "rule": "fresh human or target-player review is required for final completion",
            },
            resume_after_response=True,
            recommended_option="play the current build and answer accept / rework / reject",
            output_contract=(
                "Return accept/rework/reject, a player-feel score, and concrete notes on "
                "visual objects, drag feel, object causality, NPC goals, rewards, UI, and fun."
            ),
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        report.created_collaboration_ticket_ids.append(ticket.ticket_id)
        self._move_mission_to_waiting_human_for_player_review(
            mission_id=mission.mission_id,
            subject_ref=ticket.ticket_id,
        )

    def _move_mission_to_waiting_human_for_player_review(
        self,
        *,
        mission_id: str,
        subject_ref: str,
    ) -> None:
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.status == "waiting_human":
            return
        transition_path: list[MissionStatus]
        if mission.status in {"running", "blocked"}:
            transition_path = ["waiting_human"]
        elif mission.status == "queued":
            transition_path = ["running", "waiting_human"]
        elif mission.status in {"changing_plan", "repairing"}:
            transition_path = ["queued", "running", "waiting_human"]
        else:
            return
        for target in transition_path:
            current = self.control_plane.missions.get(mission_id)
            if current is None or current.status == target:
                continue
            self.control_plane.transition_mission(
                mission_id=mission_id,
                target=target,
                actor=self.daemon_id,
                reason=(
                    "daemon converted missing fresh player-review evidence into a human "
                    "playtest collaboration wait"
                ),
                subject_ref=subject_ref,
            )

    def _ensure_open_acceptance_keeps_product_pressure(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Do not let product dogfood stop just because automated gates passed.

        A product mission may need explicit human acceptance, but an open
        acceptance ticket is not permission to idle forever.  For high-polish
        dogfood contracts, the daemon converts "awaiting acceptance without a
        human accept" into the next low-risk product-pressure iteration.  This
        makes the behavior KUN-native instead of relying on an external
        supervisor to keep nudging the mission.
        """

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        if mission.status != "awaiting_acceptance":
            return
        if mission.acceptance_ref is not None:
            return
        contract = self.control_plane.contracts.get(mission.execution_contract_ref or "")
        if not _should_continue_product_pressure_while_awaiting_acceptance(contract):
            return
        ticket = _latest_open_acceptance_ticket(self.control_plane, mission)
        if ticket is None:
            return
        if _report_has_mechanical_acceptance_loop(report, mission_id):
            return
        if _has_pending_mechanical_acceptance_loop_followup(self.control_plane, mission):
            return
        if _has_ready_or_active_current_plan_work(self.control_plane, mission):
            return
        if _latest_product_pressure_evidence_allows_waiting(contract):
            return
        if _is_acceptance_rework_plan_version(
            mission.current_plan_version
        ) and _latest_product_pressure_evidence_allows_waiting(
            contract,
            allow_continuous_pressure_wait=True,
        ):
            return
        gate_id = _open_acceptance_pressure_gate_id(
            mission_id=mission.mission_id,
            task_plan_version=mission.current_plan_version or "product",
            ticket_id=ticket.ticket_id,
            context_ref=ticket.context_ref,
        )
        if gate_id in self.control_plane.gate_evaluations:
            return
        gate = GateEvaluation(
            gate_evaluation_id=gate_id,
            mission_id=mission.mission_id,
            task_plan_version=mission.current_plan_version or "product",
            subject_ref=ticket.ticket_id,
            stage="acceptance",
            task_type="product_development",
            rubric_version="kun-open-acceptance-product-pressure-v1",
            metric_pack_version="kun-v6-north-star-v1",
            north_star_verdict="partial",
            result_quality=0.72,
            speed=0.7,
            cost=0.76,
            risk=0.42,
            evidence_quality=0.72,
            collaboration_quality=0.8,
            score_breakdown={
                "human_acceptance_missing": 1.0,
                "automated_gates_not_final_acceptance": 1.0,
                "commercial_product_polish_pressure": 1.0,
            },
            thresholds={"result_quality": 0.95},
            hard_gate_failures=[
                "human_acceptance_missing",
                "automated_gate_pass_is_not_final_acceptance",
                "commercial_game_polish_required",
                "character_animation_ui_reference_iteration_required",
            ],
            failure_category="delivery_failure",
            root_cause=(
                "The latest product delivery has automated evidence but no human acceptance. "
                "KUN must keep improving product feel, commercial UI polish, character animation, "
                "touch manipulation, and causal interaction depth instead of idling in awaiting_acceptance."
            ),
            responsibility_scope="kun_auto",
            confidence=0.88,
            next_action="needs_repair",
            next_state="repairing",
            governance_signal="open_acceptance_requires_continued_product_pressure",
            created_by=self.daemon_id,
        )
        self.control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
        self.control_plane._persist_gate(gate)
        report.recovery_gate_refs.append(gate.gate_evaluation_id)

    def _ensure_acceptance_rework_from_feedback(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Turn rejected product acceptance feedback into new executable work.

        Product tasks must not stop just because internal gates passed.  If a
        human/user acceptance gate says the product feel is not good enough,
        the daemon opens the next iteration itself and leaves
        ``awaiting_acceptance`` so KUN-owned runners can keep working.
        """

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        if mission.status not in {
            "delivering",
            "awaiting_acceptance",
            "repairing",
            "changing_plan",
            "running",
        }:
            return
        gate = _latest_acceptance_rework_gate(self.control_plane, mission)
        if gate is None:
            return
        contract = self.control_plane.contracts.get(mission.execution_contract_ref or "")
        if _report_has_mechanical_acceptance_loop(report, mission_id):
            return
        if _has_pending_mechanical_acceptance_loop_followup(self.control_plane, mission):
            return
        if (
            gate.governance_signal == "open_acceptance_requires_continued_product_pressure"
            and _latest_product_pressure_evidence_allows_waiting(
                contract,
                allow_continuous_pressure_wait=True,
            )
            and _is_acceptance_rework_plan_version(gate.task_plan_version)
        ):
            return
        plan_version = _acceptance_rework_plan_version(mission=mission, gate=gate)
        if any(
            item.mission_id == mission_id
            and item.task_plan_version == plan_version
            and item.status not in {"cancelled"}
            for item in self.control_plane.work_items.values()
        ):
            if mission.current_plan_version != plan_version:
                self._set_mission_plan_version(
                    mission_id=mission_id,
                    plan_version=plan_version,
                    subject_ref=gate.gate_evaluation_id,
                )
            self._move_feedback_rework_to_queue(
                mission_id=mission_id,
                subject_ref=gate.gate_evaluation_id,
            )
            return

        base_plan = _task_plan_for_version(
            self.control_plane,
            mission_id=mission_id,
            plan_version=gate.task_plan_version,
        )
        plan = _acceptance_rework_task_plan(
            mission=mission,
            gate=gate,
            base_plan=base_plan,
            plan_version=plan_version,
            contract=contract,
        )
        self.control_plane.task_plans[plan.plan_id] = plan
        if self.control_plane.store is not None:
            self.control_plane.store.put_task_plan(plan)

        work_items = _acceptance_rework_work_items(
            mission=mission,
            gate=gate,
            plan_version=plan_version,
            contract=contract,
        )
        for work_item in work_items:
            self.control_plane.work_items[work_item.work_item_id] = work_item
            self._persist_work_item(work_item)
            report.created_work_item_ids.append(work_item.work_item_id)

        self._set_mission_plan_version(
            mission_id=mission_id,
            plan_version=plan_version,
            subject_ref=gate.gate_evaluation_id,
        )
        self._move_feedback_rework_to_queue(
            mission_id=mission_id,
            subject_ref=gate.gate_evaluation_id,
        )
        report.recovery_gate_refs.append(gate.gate_evaluation_id)

    def _set_mission_plan_version(
        self,
        *,
        mission_id: str,
        plan_version: str,
        subject_ref: str,
    ) -> None:
        mission = self.control_plane.missions[mission_id]
        if mission.current_plan_version == plan_version:
            return
        updated = mission.model_copy(update={"current_plan_version": plan_version})
        self.control_plane.missions[mission_id] = updated
        if self.control_plane.store is not None:
            self.control_plane.store.put_mission(updated)
        self.control_plane._record_ledger_event(
            mission_id=mission_id,
            event_type="plan_change",
            actor=self.daemon_id,
            subject_ref=subject_ref,
            before={"current_plan_version": mission.current_plan_version},
            after={"current_plan_version": plan_version},
            payload={"reason": "human/product acceptance feedback required another iteration"},
        )

    def _move_feedback_rework_to_queue(self, *, mission_id: str, subject_ref: str) -> None:
        mission = self.control_plane.missions[mission_id]
        if mission.status in {"delivering", "awaiting_acceptance"}:
            target = "repairing" if mission.status == "awaiting_acceptance" else "changing_plan"
            self.control_plane.transition_mission(
                mission_id=mission_id,
                target=target,
                actor=self.daemon_id,
                reason="product acceptance feedback rejected final delivery; reopen automatic rework",
                subject_ref=subject_ref,
            )
        self._transition_to_queued(mission_id, subject_ref=subject_ref)

    def _ensure_mission_director_review(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Queue a KUN-native Mission Director review when supervision is configured."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.status not in ACTIVE_DAEMON_MISSION_STATUSES:
            return
        if mission.status == "paused":
            return
        plan_version = mission.current_plan_version or "no-plan"
        delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission)
        open_ticket_ids = sorted(
            ticket.ticket_id
            for ticket in self.control_plane.collaboration_tickets.values()
            if ticket.mission_id == mission_id
            and ticket.status in {"open", "waiting", "escalated", "fallback_selected"}
        )
        queued_or_active = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id and item.status in {"queued", "running", "retrying"}
        ]
        if any(_is_pending_mission_director_or_plan_change(item) for item in queued_or_active):
            return
        if not self._mission_director_should_review(
            mission=mission,
            queued_or_active=queued_or_active,
        ):
            return
        signature = _hash_payload(
            {
                "mission_id": mission_id,
                "status": mission.status,
                "task_plan_version": plan_version,
                "delivery_manifest_ref": delivery_manifest_ref,
                "acceptance_ref": mission.acceptance_ref,
                "open_ticket_ids": open_ticket_ids,
                "queued_or_active_count": len(queued_or_active),
            }
        )[:12]
        idempotency_key = f"mission-director:{mission_id}:{signature}"
        if any(
            item.idempotency_key == idempotency_key and item.status != "cancelled"
            for item in self.control_plane.work_items.values()
        ):
            return
        work_item = WorkItem(
            work_item_id=f"work-mission-director-{_slug(mission_id)}-{signature}",
            mission_id=mission_id,
            task_plan_version=plan_version,
            type="review",
            owner=MISSION_DIRECTOR_OWNER,
            priority=100,
            idempotency_key=idempotency_key,
            expected_output=(
                "Act as KUN Mission Director. Review goal alignment, information gaps, "
                "task decomposition, worker distribution, evidence, acceptance state, and "
                "whether gate/test/self-score pass is being mistaken for final product quality."
            ),
            recovery_refs=[ref for ref in [delivery_manifest_ref] if ref is not None],
        )
        if self._runner_for(work_item) is None:
            return
        self.control_plane.work_items[work_item.work_item_id] = work_item
        self._persist_work_item(work_item)
        report.created_work_item_ids.append(work_item.work_item_id)

    def _retire_superseded_mission_director_work(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Cancel queued duplicate reviews once a plan-change handoff exists."""

        pending_plan_change_ids = {
            item.work_item_id
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.status in {"queued", "running", "retrying"}
            and _is_kun_plan_change_work_item(item)
        }
        pending_directors = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.owner == MISSION_DIRECTOR_OWNER
            and item.status in {"queued", "retrying"}
            and not _is_rainflow_phase1_acceptance_review(item)
        ]
        mission = self.control_plane.missions.get(mission_id)
        accepted_or_terminal = (
            mission is not None
            and mission.status in {"learning_writeback", "closed", "partial_closed"}
            and mission.acceptance_ref is not None
        )
        if accepted_or_terminal or pending_plan_change_ids:
            retiring = pending_directors
        elif len(pending_directors) > 1:
            retiring = pending_directors[:-1]
        else:
            return
        retired: list[str] = []
        for item in retiring:
            updated = item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[item.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(item.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-superseded-mission-director-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"superseded-mission-director-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "pending_plan_change_ids": sorted(pending_plan_change_ids),
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "superseded_mission_director_cleanup",
                "mission_director_dedupe",
                "plan_change_handoff",
                "state_hygiene",
                *sorted(pending_plan_change_ids),
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _retire_terminal_followup_only_work(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Collapse stale governance residue after accepted product delivery."""

        mission = self.control_plane.missions.get(mission_id)
        if (
            mission is None
            or mission.acceptance_ref is None
            or mission.status not in {"learning_writeback", "closed", "partial_closed"}
        ):
            return
        retired: list[str] = []
        for work_item in list(self.control_plane.work_items.values()):
            if work_item.mission_id != mission_id:
                continue
            if work_item.status in {"done", "cancelled"}:
                continue
            if not (
                _is_state_preserving_followup(work_item) or _is_kun_plan_change_work_item(work_item)
            ):
                continue
            updated = work_item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[work_item.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(work_item.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-terminal-followup-cleanup-{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"terminal-followup-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "mission_status": mission.status,
                    "acceptance_ref": mission.acceptance_ref,
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "terminal_followup_cleanup",
                "state_hygiene",
                "followup_closure",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _restore_cancelled_rainflow_phase1_acceptance_reviews(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Repair over-eager governance cleanup of RainFlow Phase 1 reviews."""

        if not _is_rainflow_ad_mission(self.control_plane, mission_id):
            return
        mission = self.control_plane.missions.get(mission_id)
        if mission is None or not mission.current_plan_version:
            return
        current_items = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.task_plan_version == mission.current_plan_version
        ]
        downstream_dependency_refs = {
            dep
            for item in current_items
            if item.status in {"queued", "running", "retrying", "blocked"}
            for dep in item.dependencies
        }
        restored: list[str] = []
        for item in current_items:
            if item.status != "cancelled":
                continue
            if not _is_rainflow_phase1_acceptance_review(item):
                continue
            if item.work_item_id not in downstream_dependency_refs:
                continue
            dependency_statuses = [
                self.control_plane.work_items[dep].status
                for dep in item.dependencies
                if dep in self.control_plane.work_items
            ]
            if any(status == "cancelled" for status in dependency_statuses):
                continue
            updated = item.model_copy(update={"status": "queued"})
            self.control_plane.work_items[item.work_item_id] = updated
            self._persist_work_item(updated)
            restored.append(item.work_item_id)
        if not restored:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-rainflow-phase1-review-restore-{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"rainflow-phase1-review-restore/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "restored_work_item_ids": restored,
                    "reason": "RainFlow Phase 1 acceptance review is core product work.",
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "rainflow_phase1_acceptance_review_restored",
                "state_hygiene",
                "core_product_gate_preserved",
                *restored,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.recovered_work_item_ids.extend(restored)

    def _mission_director_should_review(
        self,
        *,
        mission: Mission,
        queued_or_active: Sequence[WorkItem],
    ) -> bool:
        if mission.status in {"delivering", "awaiting_acceptance"}:
            return True
        current_plan_reviewed = self._mission_director_reviewed_current_plan(mission)
        if (
            mission.artifact_manifest_refs
            and mission.current_plan_version
            and not current_plan_reviewed
        ):
            return True
        plan = self._current_task_plan(mission)
        if plan is None:
            return mission.status in {"planning", "info_gap", "contracted", "queued", "running"}
        if plan.info_gaps:
            return mission.status in {"planning", "info_gap", "contracted", "queued", "running"}
        if mission.status in {"blocked", "repairing"}:
            return not queued_or_active
        if queued_or_active:
            return False
        return mission.status in {"planning", "info_gap", "contracted", "queued", "running"}

    def _mission_director_reviewed_current_plan(self, mission: Mission) -> bool:
        if not mission.current_plan_version:
            return False
        return any(
            gate.mission_id == mission.mission_id
            and gate.task_plan_version == mission.current_plan_version
            and gate.created_by == MISSION_DIRECTOR_OWNER
            for gate in self.control_plane.gate_evaluations.values()
        )

    def _ensure_self_improvement_audit(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Queue a governed Nuo self-audit when task evidence exposes KUN gaps."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None:
            return
        nuo_runner = self.runners_by_owner.get("nuo")
        if nuo_runner is None:
            return
        if not self._mission_has_self_improvement_audit_signal(mission):
            return
        signature = self_improvement_audit_signature(self.control_plane, mission_id=mission_id)
        work_item = build_self_improvement_audit_work_item(
            mission=mission,
            signature=signature,
        )
        can_run = getattr(nuo_runner, "can_run", None)
        if callable(can_run) and not can_run(work_item):
            return
        if self._self_improvement_audit_already_seen(work_item):
            return
        self.control_plane.work_items[work_item.work_item_id] = work_item
        self._persist_work_item(work_item)
        report.created_work_item_ids.append(work_item.work_item_id)
        self._transition_to_queued(mission_id, subject_ref=work_item.work_item_id)
        artifact = ArtifactRecord(
            artifact_id=f"artifact-self-improvement-audit-queued-{_slug(mission_id)}-{signature}",
            kind="decision",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/self-improvement-audit/"
                f"{mission_id}/{signature}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "signature": signature,
                    "work_item_id": work_item.work_item_id,
                    "observed_at": observed_at.isoformat(),
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "self_improvement_audit_queued",
                SELF_IMPROVEMENT_AUDIT_SUPPORT,
                f"self_improvement_audit_signature:{signature}",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)

    def _mission_has_self_improvement_audit_signal(self, mission: Mission) -> bool:
        mission_items = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission.mission_id
        ]
        if any(
            item.required_capability_refs and item.status in {"done", "partial"}
            for item in mission_items
        ):
            return True
        if any(
            item.owner in {"qi", "nuo", MISSION_DIRECTOR_OWNER}
            and item.status in {"blocked", "failed", "partial"}
            for item in mission_items
        ):
            return True
        if mission.status in {"delivering", "awaiting_acceptance", "blocked", "repairing"}:
            return True
        if mission.task_type == "self_improvement" and any(
            item.status in {"failed", "blocked", "partial"} for item in mission_items
        ):
            return True
        return any(
            profile.runtime_enabled
            and (
                profile.promotion_stage != "production"
                or not profile.evidence_refs
                or not profile.rollback_plan
            )
            for profile in self.control_plane.capability_profiles.values()
        )

    def _self_improvement_audit_already_seen(self, work_item: WorkItem) -> bool:
        if any(
            item.idempotency_key == work_item.idempotency_key
            and item.status in {"queued", "running", "retrying", "done", "partial"}
            for item in self.control_plane.work_items.values()
        ):
            return True
        return any(
            artifact.work_item_id == work_item.work_item_id
            and SELF_IMPROVEMENT_AUDIT_SUPPORT in artifact.supports
            for artifact in self.control_plane.artifacts.values()
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
            work_item_id = f"work-qi-capability-dedupe-{_slug(mission_id)}-{evidence_sig}"
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
        if mission.status not in {
            "queued",
            "running",
            "waiting_human",
            "waiting_external",
            "blocked",
            "repairing",
            "changing_plan",
            "delivering",
            "awaiting_acceptance",
            "learning_writeback",
            "closed",
            "partial_closed",
        }:
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

    def _retire_superseded_product_gate_work(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Cancel stale product gates while a newer rework branch is active."""

        mission = self.control_plane.missions.get(mission_id)
        if (
            mission is None
            or mission.task_type != "product_development"
            or not mission.current_plan_version
        ):
            return
        current_items = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.task_plan_version == mission.current_plan_version
        ]
        protected_branch_id = _active_game_rework_branch_id(current_items)
        active_iteration_ids = {
            item.work_item_id
            for item in current_items
            if item.owner == "kun-game-production-runner"
            and item.type == "execution"
            and item.status not in {"done", "cancelled"}
            and "iteration" in (item.phase or item.work_item_id)
            and (
                protected_branch_id is None
                or _game_rework_branch_id(item) in {None, protected_branch_id}
            )
        }
        if not active_iteration_ids and protected_branch_id is None:
            return

        protected_ids = set(active_iteration_ids)
        if protected_branch_id is not None:
            protected_ids.update(
                item.work_item_id
                for item in current_items
                if _game_rework_branch_id(item) == protected_branch_id
                and item.status not in {"done", "cancelled"}
            )
        changed = True
        while changed:
            changed = False
            for item in current_items:
                if item.work_item_id in protected_ids:
                    continue
                if any(dep in protected_ids for dep in item.dependencies):
                    protected_ids.add(item.work_item_id)
                    changed = True

        retired: list[str] = []
        for item in current_items:
            if item.work_item_id in protected_ids:
                continue
            if item.status in {"done", "cancelled"}:
                continue
            branch_id = _game_rework_branch_id(item)
            if (
                protected_branch_id is not None
                and branch_id is not None
                and branch_id != protected_branch_id
                and item.status != "running"
            ):
                updated = item.model_copy(update={"status": "cancelled"})
                self.control_plane.work_items[updated.work_item_id] = updated
                self._persist_work_item(updated)
                retired.append(updated.work_item_id)
                continue
            if not _is_product_review_or_delivery_gate(item):
                continue
            updated = item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[updated.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(updated.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-superseded-product-gate-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"superseded-product-gate-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "active_plan_version": mission.current_plan_version,
                    "active_iteration_ids": sorted(active_iteration_ids),
                    "protected_work_item_ids": sorted(protected_ids),
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "superseded_product_gate_cleanup",
                "product_rework_branch_isolation",
                *retired,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _retire_downstream_product_work_after_failed_gate(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Cancel queued product delivery steps blocked by a failed product gate."""

        mission = self.control_plane.missions.get(mission_id)
        if (
            mission is None
            or mission.task_type != "product_development"
            or not mission.current_plan_version
        ):
            return
        current_items = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.task_plan_version == mission.current_plan_version
        ]
        failed_gate_ids = {
            item.work_item_id
            for item in current_items
            if item.status == "failed" and _is_blocking_product_gate(item)
        }
        if not failed_gate_ids:
            return

        retired: list[str] = []
        blocked_ids = set(failed_gate_ids)
        changed = True
        while changed:
            changed = False
            for item in current_items:
                if item.work_item_id in blocked_ids:
                    continue
                if item.status in {"done", "cancelled", "running"}:
                    continue
                if not any(dep in blocked_ids for dep in item.dependencies):
                    continue
                blocked_ids.add(item.work_item_id)
                if _is_product_review_or_delivery_gate(item):
                    updated = item.model_copy(update={"status": "cancelled"})
                    self.control_plane.work_items[updated.work_item_id] = updated
                    self._persist_work_item(updated)
                    retired.append(updated.work_item_id)
                changed = True

        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-failed-product-gate-downstream-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"failed-product-gate-downstream-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "active_plan_version": mission.current_plan_version,
                    "failed_gate_work_item_ids": sorted(failed_gate_ids),
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "failed_product_gate_downstream_cleanup",
                "delivery_blocked_by_failed_player_gate",
                *retired,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _retire_downstream_product_work_after_cancelled_dependency(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Cancel queued product gates that can never run after a dependency is cancelled."""

        mission = self.control_plane.missions.get(mission_id)
        if (
            mission is None
            or mission.task_type != "product_development"
            or not mission.current_plan_version
        ):
            return
        current_items = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == mission_id
            and item.task_plan_version == mission.current_plan_version
        ]
        blocked_ids = {item.work_item_id for item in current_items if item.status == "cancelled"}
        if not blocked_ids:
            return

        retired: list[str] = []
        changed = True
        while changed:
            changed = False
            for item in current_items:
                if item.work_item_id in blocked_ids:
                    continue
                if item.status in {"done", "cancelled", "running"}:
                    continue
                if not any(dep in blocked_ids for dep in item.dependencies):
                    continue
                blocked_ids.add(item.work_item_id)
                if _is_product_review_or_delivery_gate(item):
                    updated = item.model_copy(update={"status": "cancelled"})
                    self.control_plane.work_items[updated.work_item_id] = updated
                    self._persist_work_item(updated)
                    retired.append(updated.work_item_id)
                changed = True

        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-cancelled-dependency-downstream-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"cancelled-dependency-downstream-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "active_plan_version": mission.current_plan_version,
                    "cancelled_dependency_work_item_ids": sorted(blocked_ids),
                    "retired_work_item_ids": retired,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "cancelled_dependency_downstream_cleanup",
                "unrunnable_delivery_queue_cleanup",
                *retired,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _retire_stale_quality_followups_before_running(
        self,
        *,
        mission_id: str,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Retire stale quality follow-ups when fresher execution lanes supersede them."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or mission.task_type != "product_development":
            return
        contract = self.control_plane.contracts.get(mission.execution_contract_ref or "")
        ready_items = self.control_plane.ready_work_items(mission_id, now=observed_at)
        has_ready_core_work = any(
            _is_ready_product_core_work(
                self.control_plane,
                mission=mission,
                contract=contract,
                work_item=item,
            )
            for item in ready_items
        )
        human_only_gate = _latest_human_player_review_only_gate(self.control_plane, mission)
        if not has_ready_core_work and human_only_gate is None:
            return

        retired: list[str] = []
        for item in list(self.control_plane.work_items.values()):
            if item.mission_id != mission_id or item.status not in {"queued", "partial"}:
                continue
            if not _is_stale_quality_followup(item):
                continue
            if human_only_gate is not None and item.recovery_refs:
                referenced_gate_ids = {
                    ref for ref in item.recovery_refs if ref in self.control_plane.gate_evaluations
                }
                if (
                    referenced_gate_ids
                    and human_only_gate.gate_evaluation_id not in referenced_gate_ids
                ):
                    continue
            updated = item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[updated.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(updated.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-stale-quality-followup-cleanup-{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"stale-quality-followup-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "retired_work_item_ids": retired,
                    "has_ready_core_work": has_ready_core_work,
                    "human_only_gate": human_only_gate.gate_evaluation_id
                    if human_only_gate is not None
                    else None,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "stale_quality_followup_cleanup",
                "core_product_lane_priority",
                *retired,
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
        if mission.status not in {
            "queued",
            "running",
            "waiting_human",
            "waiting_external",
            "blocked",
            "repairing",
            "changing_plan",
            "delivering",
            "awaiting_acceptance",
            "closed",
            "partial_closed",
        }:
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
            if self._ticket_is_bound_to_active_waiting_work(ticket):
                continue
            if ticket.context_ref in active_context_refs:
                continue
            if (
                latest_delivery_manifest_ref is not None
                and ticket.ticket_id
                == _acceptance_ticket_id(mission_id, latest_delivery_manifest_ref)
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

    def _ticket_is_bound_to_active_waiting_work(self, ticket: CollaborationTicket) -> bool:
        """Keep human tickets visible while their referenced work item is still blocked."""

        bound_refs = [ticket.context_ref, *ticket.auto_resolvable_by]
        for ref in bound_refs:
            if not ref:
                continue
            work_item = self.control_plane.work_items.get(ref)
            if work_item is None:
                continue
            if work_item.mission_id != ticket.mission_id:
                continue
            if work_item.status in {"waiting_human", "waiting_external", "blocked"}:
                return True
        return False

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
        protected_game_rework_branch_id = _active_game_rework_branch_id(
            list(mission_work_items.values())
        )
        running_run_item_ids = {
            run.work_item_id
            for run in self.control_plane.runs.values()
            if run.work_item_id in mission_work_items and run.exit_status == "running"
        }
        for item in mission_work_items.values():
            if item.status != "queued" or item.lease is None:
                continue
            lease_expired = item.timeout is not None and item.timeout <= now
            lease_owned_by_this_daemon = _lease_owned_by_daemon(item.lease, self.daemon_id)
            if not lease_expired and not lease_owned_by_this_daemon:
                continue
            self.resource_lock_store.release_holder(item.lease, now=now)
            recovered = item.model_copy(
                update={
                    "lease": None,
                    "heartbeat": None,
                    "timeout": None,
                }
            )
            self.control_plane.work_items[recovered.work_item_id] = recovered
            self._persist_work_item(recovered)
            if recovered.work_item_id not in report.recovered_work_item_ids:
                report.recovered_work_item_ids.append(recovered.work_item_id)
        for item in mission_work_items.values():
            if item.status != "running" or item.work_item_id in running_run_item_ids:
                continue
            if item.lease is not None or item.heartbeat is not None:
                continue
            if (
                protected_game_rework_branch_id is not None
                and _game_rework_branch_id(item) == protected_game_rework_branch_id
                and item.type in {"execution", "test"}
            ):
                continue
            recovered = item.model_copy(
                update={
                    "status": "queued",
                    "lease": None,
                    "heartbeat": None,
                    "timeout": None,
                }
            )
            self.control_plane.work_items[recovered.work_item_id] = recovered
            self._persist_work_item(recovered)
            if recovered.work_item_id not in report.recovered_work_item_ids:
                report.recovered_work_item_ids.append(recovered.work_item_id)
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

    def _release_orphaned_resource_locks(self, *, now: datetime) -> None:
        """Clear locks whose owning work item is no longer actively running.

        Recovery can happen after an external kill or interrupted daemon loop.
        In that case the work item lease may already be cleared while the
        separate resource-lock file still contains the old holder.  Keeping that
        holder blocks clean retests and creates an artificial deadlock.
        """

        released_holders: set[str] = set()
        for lease in self.resource_lock_store.list_active(now=now):
            work_item = self.control_plane.work_items.get(lease.work_item_id)
            still_owns_lock = (
                work_item is not None
                and work_item.status == "running"
                and work_item.lease == lease.holder_id
            )
            if still_owns_lock or lease.holder_id in released_holders:
                continue
            self.resource_lock_store.release_holder(lease.holder_id, now=now)
            released_holders.add(lease.holder_id)

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
        if work_item.lease:
            self.resource_lock_store.release_holder(work_item.lease, now=now)
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
        mission = self.control_plane.missions.get(work_item.mission_id)
        if (
            mission is not None
            and mission.status == "queued"
            and plan.gate_evaluation.next_state not in {"running", "blocked", "cancelled", "paused"}
        ):
            self.control_plane.transition_mission(
                mission_id=mission.mission_id,
                target="running",
                actor=self.daemon_id,
                reason=(
                    "stale work recovery needs a running-state recovery transition; "
                    "move queued mission into running before applying recovery gate"
                ),
                subject_ref=finding.work_item_id,
            )
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
            self.control_plane.work_items[claimed.work_item_id] = claimed
            self._persist_work_item(claimed)
            return claimed
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
            if self._runner_for(followup) is None:
                self._queue_unrunnable_followup_ticket(
                    mission_id=work_item.mission_id,
                    work_item_id=followup.work_item_id,
                    owner=owner,
                    item_type=item_type,
                    report=report,
                )
                continue
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
            mission = self.control_plane.missions.get(mission_id)
            if (
                item.code == "quality_gate_not_passed"
                and _is_rainflow_ad_mission(self.control_plane, mission_id)
                and any(
                    _is_rainflow_core_work_item(ready)
                    for ready in self.control_plane.ready_work_items(
                        mission_id,
                        now=report.observed_at,
                    )
                )
            ):
                continue
            current_plan_version = mission.current_plan_version if mission else None
            evidence_refs = _dedupe([observation_artifact_ref, *item.evidence_refs])
            # Observation follow-ups are scoped to the active plan and code.
            # Their evidence can grow on every tick in long-running dogfood
            # missions; including all refs in the id creates endless duplicate
            # Qi/Nuo work. Keep ids stable and let recovery_refs accumulate.
            evidence_sig = _hash_payload(
                _observation_followup_signature_payload(
                    item,
                    task_plan_version=current_plan_version,
                )
            )[:12]
            if item.code == "mechanical_acceptance_rework_loop":
                qi_expected_output = (
                    "Run a Qi-owned strategy replay / shadow rerun / process audit for this "
                    "mechanical acceptance-rework loop before more product rework runs. Compare "
                    "the repeated delivery path against a stricter alternative strategy, record "
                    "observable product deltas, evidence gaps, and acceptance deltas, then emit "
                    "replay-only capability or known-limit evidence. Do not mutate production "
                    f"runtime defaults. Observation {item.code}: {item.recommended_action}"
                )
            elif item.code == "quality_gate_not_passed":
                qi_expected_output = (
                    "Audit, score, and optimize this failed quality gate. Continue iteration, "
                    "find a better path, turn product gaps into stricter acceptance criteria, "
                    "open a plan-change branch, and require clean retest evidence before final closure. "
                    f"Observation {item.code}: {item.recommended_action}"
                )
            elif item.code in {
                "failed_work_without_recovery",
                "failed_work_recovery_incomplete",
            }:
                qi_expected_output = (
                    "Audit this failed work item as an execution-quality incident. Continue "
                    "iteration, find a better path, classify whether the runner/tool path "
                    "failed, and create a strategy change or capability-governance action "
                    f"before the mission can close. Observation {item.code}: {item.recommended_action}"
                )
            elif item.code == "capability_duplicates_collapsed":
                qi_expected_output = (
                    "Audit and govern duplicate runtime capabilities. Merge, dedupe, or "
                    "discard duplicate production defaults, keep the strongest verified "
                    f"profile, and preserve evidence. Observation {item.code}: {item.recommended_action}"
                )
            else:
                qi_expected_output = (
                    "Audit, score, and optimize this runtime observation. Decide whether "
                    "to merge, dedupe, discard, change plan, or open a better strategy. "
                    f"Observation {item.code}: {item.recommended_action}"
                )
            qi_suffix = (
                "strategy-replay-"
                if item.code == "mechanical_acceptance_rework_loop"
                else "strategy_v2-"
                if item.code == "quality_gate_not_passed"
                else "recovery_v1-"
                if item.code in {"failed_work_without_recovery", "failed_work_recovery_incomplete"}
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
                    required=item.severity in {"high", "critical"},
                    priority=98 if item.code == "mechanical_acceptance_rework_loop" else 90,
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
                    required=item.severity in {"high", "critical"},
                    priority=98 if item.code == "mechanical_acceptance_rework_loop" else 90,
                    report=report,
                )

    def _retire_resolved_observation_followups(
        self,
        *,
        mission_id: str,
        observation: RuntimeObservationReport,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        active_codes = {item.code for item in observation.items}
        retired: list[str] = []
        for work_item in list(self.control_plane.work_items.values()):
            if work_item.mission_id != mission_id or work_item.status in {"done", "cancelled"}:
                continue
            if not _is_resolved_observation_followup(self.control_plane, work_item):
                continue
            if active_codes and _observation_followup_matches_active_code(
                work_item,
                active_codes,
            ):
                continue
            updated = work_item.model_copy(update={"status": "cancelled"})
            self.control_plane.work_items[updated.work_item_id] = updated
            self._persist_work_item(updated)
            retired.append(updated.work_item_id)
        if not retired:
            return
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-resolved-observation-followup-cleanup-"
                f"{mission_id}-{_compact_time(observed_at)}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://daemon/{self.daemon_id}/{mission_id}/"
                f"resolved-observation-followup-cleanup/{observed_at.isoformat()}"
            ),
            content_hash=_hash_payload(
                {
                    "mission_id": mission_id,
                    "retired_work_item_ids": retired,
                    "observation_ref": observation.mission_id,
                }
            ),
            created_by=self.daemon_id,
            mission_id=mission_id,
            supports=[
                "resolved_observation_followup_cleanup",
                "state_hygiene",
                "clean_runtime_observation"
                if not active_codes
                else "partially_resolved_runtime_observation",
            ],
            freshness="fresh",
            source_quality="primary",
        )
        self._upsert_artifact(artifact)
        report.retired_work_item_ids.extend(retired)

    def _queue_observation_followup(
        self,
        *,
        mission_id: str,
        owner: str,
        item_type: str,
        work_item_id: str,
        expected_output: str,
        evidence_refs: Sequence[str],
        required: bool,
        priority: int = 90,
        report: DaemonTickReport,
    ) -> None:
        if work_item_id in self.control_plane.work_items:
            existing = self.control_plane.work_items[work_item_id]
            if existing.status == "partial" and owner == "nuo" and item_type == "repair":
                self._queue_nuo_clean_retest_for_partial_repair(
                    mission_id=mission_id,
                    partial_repair=existing,
                    evidence_refs=evidence_refs,
                    priority=priority,
                    report=report,
                )
                return
            merged_recovery_refs = _dedupe([*existing.recovery_refs, *evidence_refs])
            fresh_evidence = any(
                ref not in existing.recovery_refs and _is_substantive_observation_evidence(ref)
                for ref in evidence_refs
            )
            if existing.status == "done" and required and fresh_evidence:
                if _is_mechanical_acceptance_loop_observation_followup(existing):
                    updated = existing.model_copy(update={"recovery_refs": merged_recovery_refs})
                    self.control_plane.work_items[work_item_id] = updated
                    self._persist_work_item(updated)
                    return
                updated = existing.model_copy(
                    update={
                        "status": "queued",
                        "priority": max(existing.priority, priority),
                        "expected_output": expected_output,
                        "recovery_refs": merged_recovery_refs,
                        "lease": None,
                        "heartbeat": None,
                        "timeout": None,
                    }
                )
                self.control_plane.work_items[work_item_id] = updated
                self._persist_work_item(updated)
                report.observation_followup_ids.append(work_item_id)
                return
            if existing.status == "queued" and (
                existing.priority < priority
                or existing.expected_output != expected_output
                or merged_recovery_refs != existing.recovery_refs
            ):
                updated = existing.model_copy(
                    update={
                        "priority": max(existing.priority, priority),
                        "expected_output": expected_output,
                        "recovery_refs": merged_recovery_refs,
                    }
                )
                self.control_plane.work_items[work_item_id] = updated
                self._persist_work_item(updated)
            return
        mission = self.control_plane.missions.get(mission_id)
        work_item = WorkItem(
            work_item_id=work_item_id,
            mission_id=mission_id,
            task_plan_version=mission.current_plan_version if mission else "runtime-observation",
            type=item_type,
            owner=owner,
            priority=priority,
            idempotency_key=f"runtime-observation:{owner}:{work_item_id}",
            expected_output=expected_output,
            recovery_refs=list(evidence_refs),
        )
        if self._runner_for(work_item) is None:
            if not required:
                return
            self._queue_unrunnable_followup_ticket(
                mission_id=mission_id,
                work_item_id=work_item.work_item_id,
                owner=owner,
                item_type=item_type,
                report=report,
            )
            return
        self.control_plane.work_items[work_item.work_item_id] = work_item
        self._persist_work_item(work_item)
        report.created_work_item_ids.append(work_item.work_item_id)
        report.observation_followup_ids.append(work_item.work_item_id)

    def _queue_nuo_clean_retests_for_partial_recoveries(
        self,
        *,
        mission_id: str,
        observation_artifact_ref: str,
        report: DaemonTickReport,
    ) -> None:
        """Ensure Nuo-origin partial repair work is closed by clean retest evidence."""

        for item in list(self.control_plane.work_items.values()):
            if item.mission_id != mission_id or item.status != "partial" or item.type != "repair":
                continue
            is_nuo_repair = item.owner == "nuo"
            is_nuo_recovery = item.owner == "control-plane" and (
                item.idempotency_key or ""
            ).startswith("nuo-recovery:")
            if not (is_nuo_repair or is_nuo_recovery):
                continue
            if _has_done_nuo_clean_retest(self.control_plane, item):
                self._close_partial_repair_with_completed_clean_retest(
                    partial_repair=item,
                    report=report,
                )
                self._requeue_failed_subjects_for_completed_clean_retest(
                    partial_repair=item,
                    report=report,
                )
                continue
            if self._has_active_nuo_clean_retest(item):
                continue
            self._queue_nuo_clean_retest_for_partial_repair(
                mission_id=mission_id,
                partial_repair=item,
                evidence_refs=[observation_artifact_ref],
                priority=item.priority,
                report=report,
            )

    def _recover_stale_waiting_human_nuo_clean_retests(
        self,
        *,
        mission_id: str,
        report: DaemonTickReport,
    ) -> None:
        """Re-probe orphaned clean retests before keeping a mission human-blocked."""

        for item in list(self.control_plane.work_items.values()):
            if item.mission_id != mission_id:
                continue
            if item.status != "waiting_human":
                continue
            if item.owner != "nuo" or item.type != "retest":
                continue
            if not (item.idempotency_key or "").startswith("nuo-clean-retest:"):
                continue
            if not _workspace_clean_retest_can_requeue(item):
                self._ensure_waiting_clean_retest_ticket(
                    mission_id=mission_id,
                    work_item=item,
                    report=report,
                )
                continue
            updated = item.model_copy(
                update={
                    "status": "queued",
                    "lease": None,
                    "heartbeat": None,
                    "timeout": None,
                }
            )
            self.control_plane.work_items[updated.work_item_id] = updated
            self._persist_work_item(updated)
            if updated.work_item_id not in report.recovered_work_item_ids:
                report.recovered_work_item_ids.append(updated.work_item_id)
            if updated.work_item_id not in report.observation_followup_ids:
                report.observation_followup_ids.append(updated.work_item_id)
            self._retire_collaboration_tickets_bound_to_work_item(
                mission_id=mission_id,
                work_item_id=updated.work_item_id,
                report=report,
            )

    def _ensure_waiting_clean_retest_ticket(
        self,
        *,
        mission_id: str,
        work_item: WorkItem,
        report: DaemonTickReport,
    ) -> None:
        """Keep an operator ticket open while a clean retest remains human-blocked."""

        matching = [
            ticket
            for ticket in self.control_plane.collaboration_tickets.values()
            if ticket.mission_id == mission_id
            and (
                ticket.context_ref == work_item.work_item_id
                or work_item.work_item_id in ticket.auto_resolvable_by
            )
        ]
        if any(
            ticket.status in {"open", "waiting", "escalated", "fallback_selected"}
            for ticket in matching
        ):
            return
        if matching:
            ticket = matching[-1]
            updated = ticket.model_copy(
                update={
                    "status": "open",
                    "resolution_refs": _dedupe([*ticket.resolution_refs, work_item.work_item_id]),
                }
            )
            self.control_plane.collaboration_tickets[updated.ticket_id] = updated
            self._persist_collaboration_ticket(updated)
            if updated.ticket_id not in report.created_collaboration_ticket_ids:
                report.created_collaboration_ticket_ids.append(updated.ticket_id)
            return
        ticket_id = f"ticket-nuo-clean-retest-{_slug(work_item.work_item_id)}-operator-action"
        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission_id,
            type="operator_action",
            role_needed="operator_with_workspace_write_access",
            why_needed=(
                "Nuo clean retest remains blocked; KUN cannot safely resume implementation "
                "until the operator restores write access or approves a writable project path."
            ),
            context_ref=work_item.work_item_id,
            risk_if_skipped=(
                "The mission will keep cycling through failed repair work without fresh "
                "product changes or clean retest evidence."
            ),
            deadline=report.observed_at + timedelta(hours=24),
            output_contract=(
                "Restore write access or provide an authorized project workspace, then rerun "
                f"{work_item.work_item_id} and attach clean retest evidence."
            ),
            fallback_policy={
                "if_unavailable": "continue only governance/replay work; do not deliver final product"
            },
            escalation_policy={
                "on_timeout": "keep mission repairing and do not report final acceptance"
            },
            resume_after_response=True,
            auto_resolvable_by=[work_item.work_item_id],
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        if ticket.ticket_id not in report.created_collaboration_ticket_ids:
            report.created_collaboration_ticket_ids.append(ticket.ticket_id)

    def _queue_nuo_clean_retests_for_blocked_current_plan_work(
        self,
        *,
        mission_id: str,
        observation_artifact_ref: str,
        report: DaemonTickReport,
    ) -> None:
        """Recover stale blocked implementation/test work with a bounded clean retest."""

        mission = self.control_plane.missions.get(mission_id)
        if mission is None or not mission.current_plan_version:
            return
        for item in list(self.control_plane.work_items.values()):
            if (
                item.mission_id != mission_id
                or item.task_plan_version != mission.current_plan_version
                or item.status != "blocked"
                or item.owner == "nuo"
            ):
                continue
            if not self._blocked_work_item_allows_clean_retest(item):
                continue
            if _has_done_nuo_clean_retest(self.control_plane, item):
                self._requeue_failed_subjects_for_completed_clean_retest(
                    partial_repair=item,
                    report=report,
                )
                continue
            if self._has_active_nuo_clean_retest(item):
                continue
            self._queue_nuo_clean_retest_for_partial_repair(
                mission_id=mission_id,
                partial_repair=item,
                evidence_refs=[observation_artifact_ref],
                priority=max(item.priority, 80),
                report=report,
            )

    def _close_partial_repair_with_completed_clean_retest(
        self,
        *,
        partial_repair: WorkItem,
        report: DaemonTickReport,
    ) -> None:
        """Mark Nuo diagnosis complete once the required clean retest has passed."""

        clean_retest_refs = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == partial_repair.mission_id
            and item.owner == "nuo"
            and item.type == "retest"
            and item.status == "done"
            and partial_repair.work_item_id in item.recovery_refs
        ]
        if not clean_retest_refs or partial_repair.status != "partial":
            return
        evidence_refs = [
            ref
            for clean_retest in clean_retest_refs
            for ref in [clean_retest.work_item_id, *clean_retest.recovery_refs]
        ]
        closed = partial_repair.model_copy(
            update={
                "status": "done",
                "lease": None,
                "heartbeat": None,
                "timeout": None,
                "recovery_refs": _dedupe([*partial_repair.recovery_refs, *evidence_refs]),
            }
        )
        self.control_plane.work_items[closed.work_item_id] = closed
        self._persist_work_item(closed)
        if closed.work_item_id not in report.recovered_work_item_ids:
            report.recovered_work_item_ids.append(closed.work_item_id)

    def _blocked_work_item_allows_clean_retest(self, item: WorkItem) -> bool:
        text = (
            f"{item.work_item_id}\n{item.expected_output}\n"
            f"{' '.join(item.recovery_refs)}\n"
            f"{' '.join(item.checkpoint_refs)}\n"
            f"{' '.join(item.external_source_refs)}"
        ).lower()
        human_decision_tokens = (
            "human acceptance",
            "human/player acceptance",
            "explicit human",
            "user acceptance",
            "user approval",
            "awaiting_acceptance",
            "人工验收",
            "用户确认",
            "用户审批",
        )
        if any(token in text for token in human_decision_tokens):
            return False
        clean_retest_tokens = (
            "workspace",
            "write",
            "permission",
            "sandbox",
            "project",
            "build",
            "test",
            "wrapper",
            "timeout",
            "tool",
            "path",
            "npm",
            "typescript",
            "ts2304",
            "写入",
            "权限",
            "构建",
            "测试",
            "目录",
        )
        if (item.workspace_ref or item.resource_locks) and any(
            token in text for token in clean_retest_tokens
        ):
            return True
        latest_run = _latest_run_for_work_item(self.control_plane, item.work_item_id)
        if latest_run is None:
            return False
        return latest_run.failure_category in {
            "tool_failure",
            "environment_failure",
            "timeout",
            "network_failure",
            "auth_failure",
            "wrapper_failure",
        }

    def _has_active_nuo_clean_retest(self, partial_repair: WorkItem) -> bool:
        return any(
            item.mission_id == partial_repair.mission_id
            and item.owner == "nuo"
            and item.type == "retest"
            and item.status in {"queued", "running", "retrying", "partial"}
            and partial_repair.work_item_id in item.recovery_refs
            for item in self.control_plane.work_items.values()
        )

    def _requeue_failed_subjects_for_completed_clean_retest(
        self,
        *,
        partial_repair: WorkItem,
        report: DaemonTickReport,
    ) -> None:
        clean_retest_refs = [
            item
            for item in self.control_plane.work_items.values()
            if item.mission_id == partial_repair.mission_id
            and item.owner == "nuo"
            and item.type == "retest"
            and item.status == "done"
            and partial_repair.work_item_id in item.recovery_refs
        ]
        if not clean_retest_refs:
            return
        subject_ids: list[str] = []
        if partial_repair.status in {"failed", "blocked"}:
            subject_ids.append(partial_repair.work_item_id)
        key = partial_repair.idempotency_key or ""
        if key.startswith("nuo-recovery:"):
            parts = key.split(":")
            if len(parts) >= 2:
                subject_ids.append(parts[1])
        for ref in partial_repair.recovery_refs:
            item = self.control_plane.work_items.get(ref)
            if item is not None and item.status in {"failed", "blocked"}:
                subject_ids.append(item.work_item_id)

        evidence_refs = [
            artifact_ref
            for clean_retest in clean_retest_refs
            for artifact_ref in [clean_retest.work_item_id, *clean_retest.recovery_refs]
        ]
        for subject_id in dict.fromkeys(subject_ids):
            subject = self.control_plane.work_items.get(subject_id)
            if subject is None or subject.status not in {"failed", "queued", "blocked"}:
                continue
            if _subject_failed_after_clean_retest(
                self.control_plane,
                subject=subject,
                clean_retests=clean_retest_refs,
            ):
                continue
            if _daemon_subject_requires_product_iteration_instead_of_clean_retest_rerun(
                self.control_plane,
                subject,
            ):
                continue
            evidence_already_attached = any(ref in subject.recovery_refs for ref in evidence_refs)
            if subject.status == "queued" and evidence_already_attached:
                continue
            update = {
                "recovery_refs": _dedupe([*subject.recovery_refs, *evidence_refs]),
            }
            if subject.status in {"failed", "blocked"}:
                update.update(
                    {
                        "status": "queued",
                        "lease": None,
                        "heartbeat": None,
                        "timeout": None,
                    }
                )
            requeued = subject.model_copy(update=update)
            self.control_plane.work_items[requeued.work_item_id] = requeued
            self._persist_work_item(requeued)
            report.recovered_work_item_ids.append(requeued.work_item_id)

    def _queue_nuo_clean_retest_for_partial_repair(
        self,
        *,
        mission_id: str,
        partial_repair: WorkItem,
        evidence_refs: Sequence[str],
        priority: int,
        report: DaemonTickReport,
    ) -> None:
        """Close Nuo diagnosis loops with an explicit clean retest work item."""

        repair_sig = _hash_payload({"partial_repair": partial_repair.work_item_id})[:12]
        work_item_id = f"work-nuo-clean-retest-{_slug(partial_repair.work_item_id)}-{repair_sig}"
        if work_item_id in self.control_plane.work_items:
            existing = self.control_plane.work_items[work_item_id]
            if existing.status == "waiting_human" and _workspace_clean_retest_can_requeue(existing):
                updated = existing.model_copy(
                    update={
                        "status": "queued",
                        "priority": max(existing.priority, priority),
                        "recovery_refs": _dedupe([*existing.recovery_refs, *evidence_refs]),
                        "lease": None,
                        "heartbeat": None,
                        "timeout": None,
                    }
                )
                self.control_plane.work_items[updated.work_item_id] = updated
                self._persist_work_item(updated)
                report.recovered_work_item_ids.append(updated.work_item_id)
                report.observation_followup_ids.append(updated.work_item_id)
                self._retire_collaboration_tickets_bound_to_work_item(
                    mission_id=mission_id,
                    work_item_id=updated.work_item_id,
                    report=report,
                )
                return
            if existing.status == "queued" and existing.priority < priority:
                updated = existing.model_copy(
                    update={
                        "priority": priority,
                        "recovery_refs": _dedupe([*existing.recovery_refs, *evidence_refs]),
                    }
                )
                self.control_plane.work_items[work_item_id] = updated
                self._persist_work_item(updated)
            return

        mission = self.control_plane.missions.get(mission_id)
        contract = (
            self.control_plane.contracts.get(mission.execution_contract_ref or "")
            if mission
            else None
        )
        workspace_ref = partial_repair.workspace_ref
        contract_workspace = _workspace_path_from_contract(contract)
        if contract_workspace:
            workspace_ref = contract_workspace
        resource_locks = list(partial_repair.resource_locks)
        if workspace_ref and not any(
            lock == f"workspace:{workspace_ref}" for lock in resource_locks
        ):
            resource_locks.append(f"workspace:{workspace_ref}")
        repair_text = (
            f"{partial_repair.work_item_id}\n{partial_repair.expected_output}\n"
            f"{' '.join(partial_repair.recovery_refs)}"
        ).lower()
        is_acceptance_loop_repair = (
            "mechanical_acceptance_rework_loop" in repair_text
            or "mechanical acceptance rework loop" in repair_text
            or ("acceptance-rework" in repair_text and "loop" in repair_text)
        )
        expected_output = (
            "Run a Nuo clean retest for the mechanical_acceptance_rework_loop diagnosis. "
            "Verify that the blocker is a product/process loop, attach governance evidence, "
            "and require Qi strategy replay or a plan-change branch before another delivery "
            "attempt. Do not open an operator ticket unless a separate environment probe "
            "fails."
            if is_acceptance_loop_repair
            else (
                "Run a Nuo clean retest for the prior partial repair diagnosis. Verify whether "
                "the workspace/write/environment blocker is cleared, attach clean retest "
                "evidence, and only then allow the product loop to continue. Do not count the "
                "original incident as a KUN capability failure without this retest."
            )
        )

        work_item = WorkItem(
            work_item_id=work_item_id,
            mission_id=mission_id,
            task_plan_version=partial_repair.task_plan_version,
            type="retest",
            owner="nuo",
            priority=priority,
            dependencies=[],
            idempotency_key=f"nuo-clean-retest:{partial_repair.work_item_id}",
            expected_output=expected_output,
            workspace_ref=workspace_ref,
            resource_locks=resource_locks,
            recovery_refs=_dedupe(
                [partial_repair.work_item_id, *partial_repair.recovery_refs, *evidence_refs]
            ),
        )
        if self._runner_for(work_item) is None:
            self._queue_unrunnable_followup_ticket(
                mission_id=mission_id,
                work_item_id=work_item.work_item_id,
                owner="nuo",
                item_type="retest",
                report=report,
            )
            return
        self.control_plane.work_items[work_item.work_item_id] = work_item
        self._persist_work_item(work_item)
        report.created_work_item_ids.append(work_item.work_item_id)
        report.observation_followup_ids.append(work_item.work_item_id)

    def _retire_collaboration_tickets_bound_to_work_item(
        self,
        *,
        mission_id: str,
        work_item_id: str,
        report: DaemonTickReport,
    ) -> None:
        """Cancel only tickets explicitly bound to a requeued machine-retest item."""

        for ticket in list(self.control_plane.collaboration_tickets.values()):
            if ticket.mission_id != mission_id:
                continue
            if ticket.status not in {"open", "waiting", "escalated", "fallback_selected"}:
                continue
            if ticket.context_ref != work_item_id and work_item_id not in ticket.auto_resolvable_by:
                continue
            updated = ticket.model_copy(
                update={
                    "status": "cancelled",
                    "resolution_refs": _dedupe([*ticket.resolution_refs, work_item_id]),
                }
            )
            self.control_plane.collaboration_tickets[updated.ticket_id] = updated
            self._persist_collaboration_ticket(updated)
            if updated.ticket_id not in report.retired_collaboration_ticket_ids:
                report.retired_collaboration_ticket_ids.append(updated.ticket_id)

    def _queue_unrunnable_followup_ticket(
        self,
        *,
        mission_id: str,
        work_item_id: str,
        owner: str,
        item_type: str,
        report: DaemonTickReport,
    ) -> None:
        ticket_id = f"collab-runner-needed-{_slug(work_item_id)}"
        if ticket_id in self.control_plane.collaboration_tickets:
            return
        mission = self.control_plane.missions.get(mission_id)
        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission_id,
            type="operator_action",
            role_needed="operator",
            why_needed=(
                "KUN identified a necessary automatic follow-up, but no runner is registered "
                f"for owner={owner!r}, type={item_type!r}. It will not enqueue work that cannot run."
            ),
            context_ref=work_item_id,
            risk_if_skipped=(
                "The mission may appear to have a recovery path while the follow-up is actually "
                "unexecutable."
            ),
            deadline=report.observed_at + timedelta(hours=4),
            sla_policy={"reminder_after_hours": 1, "escalate_after_hours": 4},
            escalation_policy={
                "after_deadline": "block_mission_until_runner_or_plan_change_exists"
            },
            fallback_policy={"allowed": False, "rule": "register a runner or change the plan"},
            resume_after_response=True,
            output_contract="Register a runner, add an executable fallback, or approve plan change.",
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        report.created_collaboration_ticket_ids.append(ticket.ticket_id)
        if mission is not None and mission.status not in {"blocked", "completed", "cancelled"}:
            try:
                self.control_plane.transition_mission(
                    mission_id=mission_id,
                    target="blocked",
                    actor=self.daemon_id,
                    reason="automatic follow-up has no executable runner",
                    subject_ref=ticket.ticket_id,
                )
            except ValueError:
                return

    def _ensure_collaboration_ticket_for_work_item(
        self,
        *,
        work_item: WorkItem,
        observed_at: datetime,
        report: DaemonTickReport,
    ) -> None:
        """Turn human/operator work into a resumable ticket instead of no-runner churn."""

        ticket_id = f"collab-work-item-{_slug(work_item.work_item_id)}"
        existing = self.control_plane.collaboration_tickets.get(ticket_id)
        if existing is not None:
            if existing.status in {"answered", "fallback_selected", "closed"}:
                target_status = "done"
            elif existing.status == "cancelled":
                target_status = "cancelled"
            else:
                target_status = "waiting_human"
            if work_item.status != target_status:
                updated = work_item.model_copy(update={"status": target_status})
                self.control_plane.work_items[updated.work_item_id] = updated
                self._persist_work_item(updated)
            return

        ticket = CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=work_item.mission_id,
            type="operator_action",
            role_needed=work_item.owner or "operator",
            why_needed=(
                "This work item represents a human/operator collaboration step. It is not "
                "daemon-runnable, so KUN must wait for an explicit response instead of "
                "putting it back on the worker queue."
            ),
            context_ref=work_item.work_item_id,
            risk_if_skipped=(
                "The mission may keep reporting runner_missing or keep replaying governance "
                "work while the actual product test queue is delayed."
            ),
            deadline=observed_at + timedelta(hours=24),
            sla_policy={"reminder_after_hours": 4, "escalate_after_hours": 24},
            escalation_policy={"after_deadline": "keep_ticket_visible_without_spawning_work"},
            fallback_policy={
                "allowed": True,
                "rule": "record a bounded human-simulator decision for non-final playtest gates",
            },
            resume_after_response=True,
            output_contract=work_item.expected_output or "Provide the requested human response.",
            auto_resolvable_by=[work_item.work_item_id],
        )
        self.control_plane.record_collaboration_ticket(ticket, actor=self.daemon_id)
        report.created_collaboration_ticket_ids.append(ticket.ticket_id)
        updated = work_item.model_copy(update={"status": "waiting_human"})
        self.control_plane.work_items[updated.work_item_id] = updated
        self._persist_work_item(updated)

    def _finalize_idle_mission(
        self,
        *,
        mission_id: str,
        report: DaemonTickReport,
        capability_policy: CapabilityExecutionPolicy,
    ) -> None:
        """Let capable runners close mission-level delivery artifacts when ready."""

        observation = build_runtime_observation_report(
            control_plane=self.control_plane,
            mission_id=mission_id,
            tick_report=report,
            capability_policy=capability_policy,
        )
        report.runtime_observations[mission_id] = observation
        blocking_items = [
            item
            for item in observation.items
            if item.severity in {"high", "critical"}
            and item.code not in {"delivery_manifest_missing"}
            and not (
                item.code == "mechanical_acceptance_rework_loop"
                and _current_plan_delivery_candidate_completed(self.control_plane, mission_id)
            )
        ]
        if blocking_items:
            return

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
        if mission.status == "awaiting_acceptance":
            self.control_plane.transition_mission(
                mission_id=mission_id,
                target="repairing",
                actor=self.daemon_id,
                reason=(
                    "daemon reopened awaiting-acceptance mission because executable "
                    "current-plan work is still ready"
                ),
                subject_ref=subject_ref,
            )
            mission = self.control_plane.missions[mission_id]
        elif mission.status == "delivering":
            self.control_plane.transition_mission(
                mission_id=mission_id,
                target="changing_plan",
                actor=self.daemon_id,
                reason="daemon reopened delivery mission because executable work is ready",
                subject_ref=subject_ref,
            )
            mission = self.control_plane.missions[mission_id]
        if mission.status in {
            "repairing",
            "retrying",
            "blocked",
            "paused",
            "changing_plan",
            "waiting_human",
            "waiting_external",
        }:
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

    def _bind_managed_service_context(
        self,
        *,
        state_store: FileDaemonServiceStateStore | None,
        started_at: datetime,
        mission_ids: list[str],
        worker_heartbeat_interval_sec: float,
    ) -> None:
        with self._service_state_lock:
            self._service_state_store = state_store
            self._service_started_at = started_at
            self._service_tick_count = 0
            self._service_consecutive_idle_ticks = 0
            self._service_active_mission_ids = list(mission_ids)
            self._service_worker_slots = {}
            self._service_worker_heartbeat_interval_sec = worker_heartbeat_interval_sec

    def _clear_managed_service_context(self) -> None:
        with self._service_state_lock:
            self._service_state_store = None
            self._service_started_at = None
            self._service_tick_count = 0
            self._service_consecutive_idle_ticks = 0
            self._service_active_mission_ids = []
            self._service_worker_slots = {}

    def _clear_managed_worker_slot(self, slot_id: str) -> None:
        with self._service_state_lock:
            if self._service_state_store is None or self._service_started_at is None:
                return
            self._service_worker_slots.pop(slot_id, None)
            tick_count = self._service_tick_count
            consecutive_idle_ticks = self._service_consecutive_idle_ticks
        self._save_managed_service_heartbeat(
            status="running",
            observed_at=_now(),
            tick_count=tick_count,
            consecutive_idle_ticks=consecutive_idle_ticks,
        )

    def _save_managed_service_heartbeat(
        self,
        *,
        status: DaemonServiceStatus,
        observed_at: datetime,
        tick_count: int,
        consecutive_idle_ticks: int,
    ) -> None:
        with self._service_state_lock:
            state_store = self._service_state_store
            started_at = self._service_started_at
            if state_store is None or started_at is None:
                return
            worker_slots = list(self._service_worker_slots.values())
            state = DaemonServiceState(
                daemon_id=self.daemon_id,
                status=status,
                started_at=started_at,
                updated_at=observed_at,
                process_id=os.getpid(),
                tick_count=tick_count,
                consecutive_idle_ticks=consecutive_idle_ticks,
                active_mission_ids=list(self._service_active_mission_ids),
                last_heartbeat_at=observed_at,
                last_tick_observed_at=observed_at,
                last_tick_ran_work_item_ids=[
                    slot.work_item_id for slot in worker_slots if slot.work_item_id
                ],
                worker_pool_size=self.worker_pool.worker_count,
                resource_lock_backend=self.resource_lock_backend,
                last_tick_worker_slots=worker_slots,
            )
            state_store.save(state)

    def _start_managed_worker_heartbeat(
        self,
        slot: WorkerSlotSnapshot,
    ) -> Callable[[], None] | None:
        with self._service_state_lock:
            if self._service_state_store is None:
                return None
            running_slot = slot.model_copy(update={"status": "running"})
            self._service_worker_slots[slot.slot_id] = running_slot
            interval = self._service_worker_heartbeat_interval_sec
            tick_count = self._service_tick_count
            consecutive_idle_ticks = self._service_consecutive_idle_ticks
        self._save_managed_service_heartbeat(
            status="running",
            observed_at=_now(),
            tick_count=tick_count,
            consecutive_idle_ticks=consecutive_idle_ticks,
        )
        stop_event = threading.Event()

        def _heartbeat_loop() -> None:
            while not stop_event.wait(interval):
                self._save_managed_service_heartbeat(
                    status="running",
                    observed_at=_now(),
                    tick_count=self._service_tick_count,
                    consecutive_idle_ticks=self._service_consecutive_idle_ticks,
                )

        thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"{_slug(self.daemon_id)}-{_slug(slot.slot_id)}-heartbeat",
            daemon=True,
        )
        thread.start()

        def _stop() -> None:
            stop_event.set()
            thread.join(timeout=min(interval, 1.0))
            self._clear_managed_worker_slot(slot.slot_id)

        return _stop


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
                f"artifact-sandbox-required-{_slug(work_item.work_item_id)}-{_compact_time(_now())}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://sandbox-required/{work_item.mission_id}/{work_item.work_item_id}"
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


class _PreflightBlockedRunner:
    runner_type: Literal["tool"] = "tool"

    def __init__(
        self,
        *,
        runner_identity: str,
        failed_skill_ids: Sequence[str],
        artifact_refs: Sequence[str],
    ) -> None:
        self.runner_identity = f"{runner_identity}:preflight-blocked"
        self.failed_skill_ids = sorted(set(failed_skill_ids))
        self.artifact_refs = list(artifact_refs)

    def run(self, work_item: WorkItem) -> WorkItemResult:
        payload = {
            "work_item_id": work_item.work_item_id,
            "mission_id": work_item.mission_id,
            "failed_skill_ids": self.failed_skill_ids,
            "preflight_artifact_refs": self.artifact_refs,
            "reason": (
                "Required preflight skill or external information check failed; "
                "KUN must repair the environment, tool, network, or skill route before "
                "scoring this as agent execution."
            ),
        }
        artifact = ArtifactRecord(
            artifact_id=(
                f"artifact-preflight-blocked-{_slug(work_item.work_item_id)}-"
                f"{_compact_time(_now())}"
            ),
            kind="report",
            path_or_uri=(
                f"control-plane://preflight-blocked/{work_item.mission_id}/{work_item.work_item_id}"
            ),
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "preflight_blocked",
                "required_skill_failed",
                "external_info_or_skill_not_silent",
                *[f"failed_skill:{skill_id}" for skill_id in self.failed_skill_ids],
            ],
            freshness="fresh",
            source_quality="primary",
        )
        return WorkItemResult(
            status="failed",
            summary=(
                "Preflight blocked execution because required skill/external-info checks failed: "
                + ", ".join(self.failed_skill_ids)
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


def daemon_service_process_is_alive(process_id: int) -> bool:
    """Return whether a recorded daemon service PID still appears alive."""

    return _process_is_alive(process_id)


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
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-")
    if not safe:
        return "item"
    if len(safe) <= 80:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:67].rstrip('-_')}-{digest}"


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


def _current_plan_delivery_candidate_completed(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    """Return true when the active plan has a passing delivery candidate."""

    mission = control_plane.missions.get(mission_id)
    if mission is None or not mission.current_plan_version:
        return False
    current_items = [
        item
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
        and item.task_plan_version == mission.current_plan_version
        and item.status != "cancelled"
        and not _is_nonblocking_governance_followup(item)
    ]
    if not current_items or any(item.status not in {"done", "partial"} for item in current_items):
        return False
    delivery_items = [
        item
        for item in current_items
        if (
            item.type == "merge"
            or "final-delivery" in item.work_item_id
            or "final_delivery" in item.work_item_id
            or "final delivery" in item.expected_output.lower()
        )
    ]
    if not delivery_items:
        return False
    delivery_item_ids = {item.work_item_id for item in delivery_items}
    return any(
        gate.subject_ref in delivery_item_ids
        and gate.north_star_verdict == "pass"
        and not gate.hard_gate_failures
        for gate in control_plane.gate_evaluations.values()
    )


def _is_nonblocking_governance_followup(work_item: WorkItem) -> bool:
    if work_item.owner == MISSION_DIRECTOR_OWNER and work_item.work_item_id.startswith(
        "work-mission-director-"
    ):
        return True
    if work_item.owner != "qi":
        return False
    if work_item.type not in {"governance", "research"}:
        return False
    identifier = f"{work_item.work_item_id} {work_item.idempotency_key or ''}"
    return (
        "runtime-learning" in identifier
        or "runtime-observation:qi:" in identifier
        or "mechanical_acceptance_rework_loop" in identifier
    )


def _acceptance_ticket_id(
    mission_id: str,
    delivery_manifest_ref: str,
    *,
    delivery_iteration_ref: str | None = None,
) -> str:
    """Build a stable, collision-resistant ticket ID for a delivery manifest.

    Manifest IDs can be very long and often share a long common prefix across
    iterations.  A hash suffix prevents a fresh delivery from being hidden by
    an older open acceptance ticket with the same truncated slug.
    """

    digest = _hash_payload(
        {
            "mission_id": mission_id,
            "delivery_manifest_ref": delivery_manifest_ref,
            "delivery_iteration_ref": delivery_iteration_ref or "",
        }
    )[:12]
    return f"collab-acceptance-{_slug(mission_id)}-{_slug(delivery_manifest_ref)}-{digest}"


def _delivery_acceptance_iteration_ref(
    control_plane: InMemoryControlPlane,
    *,
    mission: Mission,
    delivery_manifest_ref: str,
) -> str:
    manifest = control_plane.artifact_manifests.get(delivery_manifest_ref)
    return _hash_payload(
        {
            "delivery_manifest_ref": delivery_manifest_ref,
            "manifest_content_hash": manifest.content_hash if manifest is not None else "",
            "artifact_refs": manifest.artifact_refs if manifest is not None else [],
            "evidence_refs": manifest.evidence_refs if manifest is not None else [],
            "review_refs": manifest.review_refs if manifest is not None else [],
            "rollback_refs": manifest.rollback_refs if manifest is not None else [],
            "current_plan_version": mission.current_plan_version or "",
        }
    )[:12]


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


def _observation_followup_signature_payload(
    item: RuntimeObservationItem,
    *,
    task_plan_version: str | None,
) -> dict[str, object]:
    """Keep follow-ups stable per blocker, not just per plan."""

    payload: dict[str, object] = {
        "code": item.code,
        "task_plan_version": task_plan_version,
    }
    if item.code in {
        "failed_work_recovery_incomplete",
        "failed_work_without_recovery",
        "mechanical_acceptance_rework_loop",
    }:
        payload["evidence_refs"] = sorted(set(item.evidence_refs))[:8]
    return payload


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


def _lease_owned_by_daemon(lease: str, daemon_id: str) -> bool:
    return lease.startswith(f"lease:{_slug(daemon_id)}:")


def _default_resource_lock_store(
    control_plane: InMemoryControlPlane,
    *,
    backend: ResourceLockBackend = "file",
) -> FileResourceLockStore | SQLiteResourceLockStore | InMemoryResourceLockStore:
    if backend == "redis":
        raise ValueError("redis resource lock backend requires an explicit RedisResourceLockStore")
    store_path = getattr(control_plane.store, "path", None)
    if isinstance(store_path, Path):
        if backend == "sqlite":
            return SQLiteResourceLockStore(
                store_path.with_name(f"{store_path.stem}.resource-locks.sqlite3")
            )
        return FileResourceLockStore(store_path.with_name(f"{store_path.stem}.resource-locks.json"))
    if isinstance(store_path, str):
        path = Path(store_path)
        if backend == "sqlite":
            return SQLiteResourceLockStore(path.with_name(f"{path.stem}.resource-locks.sqlite3"))
        return FileResourceLockStore(path.with_name(f"{path.stem}.resource-locks.json"))
    if backend == "sqlite":
        return SQLiteResourceLockStore(
            Path(tempfile.gettempdir())
            / f"kun-control-plane-{id(control_plane)}.resource-locks.sqlite3"
        )
    return InMemoryResourceLockStore()


def _resource_lock_backend(
    store: (
        FileResourceLockStore
        | SQLiteResourceLockStore
        | RedisResourceLockStore
        | InMemoryResourceLockStore
    ),
) -> ResourceLockBackend:
    if isinstance(store, RedisResourceLockStore):
        return "redis"
    if isinstance(store, SQLiteResourceLockStore):
        return "sqlite"
    if isinstance(store, FileResourceLockStore):
        return "file"
    return "memory"


def _mission_state_requires_followup_only_work(status: MissionStatus) -> bool:
    return status in {
        "info_gap",
        "awaiting_approval",
        "waiting_human",
        "waiting_external",
        "blocked",
        "delivering",
        "awaiting_acceptance",
        "paused",
        "escalated",
    }


def _is_state_preserving_followup(work_item: WorkItem) -> bool:
    if work_item.owner not in {"qi", "nuo", "control-plane", MISSION_DIRECTOR_OWNER}:
        return False
    idempotency_key = work_item.idempotency_key or ""
    if work_item.owner in {"qi", "nuo"} and idempotency_key.startswith("nuo-recovery:"):
        return True
    return work_item.work_item_id.startswith(
        (
            "work-mission-director-",
            "work-qi-observation-",
            "work-qi-strategy-replay-",
            "work-qi-runtime-learning-",
            "work-nuo-observation-",
            "work-nuo-clean-retest-",
            "work-nuo-",
            "work-qi-preflight-",
            "work-nuo-preflight-",
        )
    )


def _is_kun_plan_change_work_item(work_item: WorkItem) -> bool:
    return work_item.owner == "kun" and (
        work_item.work_item_id.startswith("work-kun-plan-change-")
        or (work_item.idempotency_key or "").startswith("mission-director-plan-change:")
    )


def _is_pending_mission_director_or_plan_change(work_item: WorkItem) -> bool:
    return work_item.owner == MISSION_DIRECTOR_OWNER or _is_kun_plan_change_work_item(work_item)


def _latest_run_for_work_item(
    control_plane: InMemoryControlPlane,
    work_item_id: str,
) -> RunRecord | None:
    runs = [run for run in control_plane.runs.values() if run.work_item_id == work_item_id]
    if not runs:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(runs, key=lambda run: run.ended_at or run.started_at or floor)


def _run_finished_at(run: RunRecord | None) -> datetime | None:
    if run is None:
        return None
    return run.ended_at or run.started_at


def _subject_failed_after_clean_retest(
    control_plane: InMemoryControlPlane,
    *,
    subject: WorkItem,
    clean_retests: Sequence[WorkItem],
) -> bool:
    """Avoid infinite reruns once a clean retest has already unblocked the subject."""

    latest_subject_run = _latest_run_for_work_item(control_plane, subject.work_item_id)
    if latest_subject_run is None or latest_subject_run.exit_status != "failed":
        return False
    subject_finished_at = _run_finished_at(latest_subject_run)
    if subject_finished_at is None:
        return False

    clean_retest_finished_at = [
        finished_at
        for clean_retest in clean_retests
        if (
            finished_at := _run_finished_at(
                _latest_run_for_work_item(control_plane, clean_retest.work_item_id)
            )
        )
        is not None
    ]
    return bool(clean_retest_finished_at) and subject_finished_at > max(clean_retest_finished_at)


def _is_product_review_or_delivery_gate(work_item: WorkItem) -> bool:
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.owner,
            work_item.type,
            work_item.phase or "",
            work_item.expected_output,
        ]
    ).lower()
    if work_item.owner not in {"external-supervisor-gpt5.5", "kun-game-production-runner"}:
        return False
    if work_item.type not in {"review", "merge", "test"} and not (
        work_item.type == "execution" and ("final-delivery" in text or "final_delivery" in text)
    ):
        return False
    return any(
        marker in text
        for marker in (
            "supervisor-gate",
            "final-player",
            "final_player",
            "benchmark-residual",
            "benchmark_residual",
            "final-delivery",
            "final_delivery",
            "player experience",
            "product feel",
        )
    )


def _is_blocking_product_gate(work_item: WorkItem) -> bool:
    if not _is_product_review_or_delivery_gate(work_item):
        return False
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.phase or "",
            work_item.expected_output,
        ]
    ).lower()
    return any(
        marker in text
        for marker in (
            "supervisor-gate",
            "final-player",
            "final_player",
            "player experience",
            "product feel",
        )
    )


def _game_rework_branch_id(work_item: WorkItem) -> str | None:
    match = _GAME_REWORK_BRANCH_RE.match(work_item.work_item_id)
    if match is None:
        return None
    return match.group(1)


def _active_game_rework_branch_id(work_items: Sequence[WorkItem]) -> str | None:
    branches: dict[str, list[WorkItem]] = {}
    for work_item in work_items:
        branch_id = _game_rework_branch_id(work_item)
        if branch_id is None:
            continue
        branches.setdefault(branch_id, []).append(work_item)
    if not branches:
        return None

    candidates: list[tuple[tuple[int, int, int, int, str], str]] = []
    for branch_id, branch_items in branches.items():
        has_running = 1 if any(item.status == "running" for item in branch_items) else 0
        pending_exec_or_test = any(
            item.status in {"queued", "retrying", "blocked", "partial"}
            and item.type in {"execution", "test"}
            for item in branch_items
        )
        pending_any = any(
            item.status in {"queued", "retrying", "blocked", "partial"} for item in branch_items
        )
        failed_count = sum(1 for item in branch_items if item.status == "failed")
        if not (has_running or pending_exec_or_test or pending_any):
            continue
        candidates.append(
            (
                (
                    has_running,
                    1 if pending_exec_or_test else 0,
                    -failed_count,
                    sum(1 for item in branch_items if item.status == "done"),
                    branch_id,
                ),
                branch_id,
            )
        )
    if not candidates:
        return None
    return max(candidates)[1]


def _daemon_subject_requires_product_iteration_instead_of_clean_retest_rerun(
    control_plane: InMemoryControlPlane,
    subject: WorkItem,
) -> bool:
    """Do not let environment clean retests rerun product-quality gates."""

    latest_run = _latest_run_for_work_item(control_plane, subject.work_item_id)
    if latest_run is not None:
        product_failure = latest_run.failure_category in {
            "delivery_failure",
            "model_quality_failure",
            "evidence_failure",
        }
        if product_failure and (
            _subject_latest_gate_is_browser_environment_blocker(control_plane, subject)
            or _subject_latest_gate_is_local_runtime_evidence_linkage_blocker(
                control_plane, subject
            )
        ):
            return False
        if product_failure and (
            _is_product_review_or_delivery_gate(subject) or _is_rainflow_core_work_item(subject)
        ):
            return True
    if not _is_product_review_or_delivery_gate(subject):
        return False
    for gate in control_plane.gate_evaluations.values():
        if gate.mission_id != subject.mission_id or gate.subject_ref != subject.work_item_id:
            continue
        if _daemon_gate_requires_product_iteration(gate):
            return True
    return False


def _subject_latest_gate_is_browser_environment_blocker(
    control_plane: InMemoryControlPlane,
    subject: WorkItem,
) -> bool:
    gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == subject.mission_id and gate.subject_ref == subject.work_item_id
    ]
    if not gates:
        return False
    return set(gates[-1].hard_gate_failures) == {"local_browser_player_gate_blocked"}


def _subject_latest_gate_is_local_runtime_evidence_linkage_blocker(
    control_plane: InMemoryControlPlane,
    subject: WorkItem,
) -> bool:
    gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == subject.mission_id and gate.subject_ref == subject.work_item_id
    ]
    if not gates:
        return False
    return set(gates[-1].hard_gate_failures) == {"local_runtime_evidence_missing"}


def _daemon_gate_requires_product_iteration(gate: GateEvaluation) -> bool:
    if gate.next_action not in {"needs_plan_change", "needs_repair", "rejected"}:
        return False
    if _gate_only_requires_fresh_human_player_review(gate):
        return False
    text = " ".join(
        [
            gate.failure_category or "",
            gate.root_cause,
            gate.governance_signal,
            *gate.hard_gate_failures,
        ]
    ).lower()
    return any(
        marker in text
        for marker in (
            "fresh_real_player_review_pass",
            "final_player",
            "player_feel",
            "player experience",
            "product feel",
            "commercial_ui",
            "visual_object",
            "character_animation",
            "causal_interaction",
            "touch_drag",
            "delivery_failure",
            "not final",
            "rework_required",
        )
    )


def _should_continue_product_pressure_while_awaiting_acceptance(
    contract: ExecutionContract | None,
) -> bool:
    if contract is None:
        return False
    delivery_policy = contract.delivery_contract
    if delivery_policy.get("auto_continue_until_human_acceptance") is True:
        return True
    production_mode = delivery_policy.get("production_mode")
    return production_mode == "scribble_adventure_functional_parity_v1"


def _is_acceptance_rework_plan_version(plan_version: str | None) -> bool:
    return bool(plan_version and "-acceptance-rework-" in plan_version)


def _latest_product_pressure_evidence_allows_waiting(
    contract: ExecutionContract | None,
    *,
    allow_continuous_pressure_wait: bool = False,
) -> bool:
    """Return true when fresh external product-feel evidence is strong enough to wait.

    Human acceptance is still required to close a product mission.  Contracts
    that explicitly ask KUN to continue until final game completion or human
    acceptance must keep using passed product evidence as an input to the next
    iteration, not as permission to idle in ``awaiting_acceptance``.
    """

    if contract is None or not isinstance(contract.delivery_contract, dict):
        return False
    delivery_policy = contract.delivery_contract
    if (
        _contract_requires_continuous_product_pressure(delivery_policy)
        and not allow_continuous_pressure_wait
    ):
        return False
    project_path = _workspace_path_from_contract(contract)
    if not project_path:
        return False
    docs_path = Path(project_path).expanduser() / "docs"
    final_payload = _read_json_file(docs_path / "final-player-experience-gate.json")
    production_mode = delivery_policy.get("production_mode")
    final_required = bool(
        delivery_policy.get("final_player_experience_required")
        or production_mode == "scribble_adventure_functional_parity_v1"
    )
    if final_required:
        if not final_payload:
            return False
        final_threshold = float(
            final_payload.get(
                "threshold",
                delivery_policy.get("final_player_experience_threshold", 0.95),
            )
        )
        if final_payload.get("pass") is not True:
            return False
        if float(final_payload.get("score", 0.0)) < final_threshold:
            return False
        dimensions = final_payload.get("dimensions")
        dimension_floor = final_payload.get("dimension_floor")
        if isinstance(dimensions, dict) and dimension_floor is not None:
            floor = float(dimension_floor)
            for value in dimensions.values():
                if isinstance(value, dict) and float(value.get("score", 0.0)) < floor:
                    return False

    residual_payload = _read_json_file(docs_path / "benchmark-residual-audit.json")
    residual_required = bool(delivery_policy.get("benchmark_residual_required") or residual_payload)
    if residual_required:
        if not residual_payload:
            return False
        residual_threshold = float(
            residual_payload.get(
                "threshold",
                delivery_policy.get("benchmark_residual_threshold", 0.003),
            )
        )
        if residual_payload.get("pass") is not True:
            return False
        if float(residual_payload.get("overall_residual", 1.0)) > residual_threshold:
            return False

    return final_required or residual_required


def _has_pending_mechanical_acceptance_loop_followup(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> bool:
    """Avoid product rework churn while Qi/Nuo are auditing a detected loop."""

    contract = control_plane.contracts.get(mission.execution_contract_ref or "")
    if _is_rainflow_ad_context(mission, contract):
        return False

    loop_marker = "mechanical_acceptance_rework_loop"
    active_statuses = {"queued", "running", "retrying", "partial"}
    governance_owners = {"qi", "nuo"}
    for item in control_plane.work_items.values():
        if (
            item.mission_id != mission.mission_id
            or item.owner not in governance_owners
            or item.status not in active_statuses
        ):
            continue
        if (
            item.owner == "nuo"
            and item.status == "partial"
            and _has_done_nuo_clean_retest(control_plane, item)
        ):
            continue
        haystack = " ".join(
            [
                item.work_item_id,
                item.expected_output,
                *item.recovery_refs,
            ]
        )
        if loop_marker in haystack:
            return True
        if "acceptance-rework" in (mission.current_plan_version or "") and (
            "quality_gate_not_passed" in haystack or "strategy replay" in haystack
        ):
            return True
    return False


def _report_has_mechanical_acceptance_loop(
    report: DaemonTickReport,
    mission_id: str,
) -> bool:
    observation = report.runtime_observations.get(mission_id)
    if observation is None:
        return False
    return any(
        item.code == "mechanical_acceptance_rework_loop" and item.severity in {"high", "critical"}
        for item in observation.items
    )


def _has_done_nuo_clean_retest(
    control_plane: InMemoryControlPlane,
    partial_repair: WorkItem,
) -> bool:
    return any(
        item.mission_id == partial_repair.mission_id
        and item.owner == "nuo"
        and item.type == "retest"
        and item.status == "done"
        and partial_repair.work_item_id in item.recovery_refs
        for item in control_plane.work_items.values()
    )


def _contract_requires_continuous_product_pressure(delivery_policy: dict[str, object]) -> bool:
    if delivery_policy.get("auto_continue_until_human_acceptance") is True:
        return True
    hard_constraint = str(delivery_policy.get("hard_user_constraint", ""))
    if hard_constraint in {
        "continue_until_final_game_complete",
        "auto_continue_until_human_acceptance",
    }:
        return True
    return delivery_policy.get("production_mode") == "scribble_adventure_functional_parity_v1"


def _transition_product_delivery_to_awaiting_acceptance(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    actor: str,
    reason: str,
    subject_ref: str,
) -> None:
    mission = control_plane.missions[mission_id]
    if mission.status == "awaiting_acceptance":
        return
    if mission.status == "running":
        control_plane.transition_mission(
            mission_id=mission_id,
            target="delivering",
            actor=actor,
            reason=f"{reason}; mark completed product delivery as delivering first",
            subject_ref=subject_ref,
        )
    control_plane.transition_mission(
        mission_id=mission_id,
        target="awaiting_acceptance",
        actor=actor,
        reason=reason,
        subject_ref=subject_ref,
    )


def _latest_open_acceptance_ticket(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> CollaborationTicket | None:
    latest_manifest_ref = _latest_delivery_manifest_ref(control_plane, mission)
    candidates = [
        ticket
        for ticket in control_plane.collaboration_tickets.values()
        if ticket.mission_id == mission.mission_id
        and ticket.type == "review"
        and ticket.status == "open"
        and (
            latest_manifest_ref is None
            or ticket.context_ref == latest_manifest_ref
            or ticket.ticket_id == _acceptance_ticket_id(mission.mission_id, latest_manifest_ref)
        )
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda ticket: ticket.ticket_id)[-1]


def _has_ready_or_active_current_plan_work(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> bool:
    return any(
        work_item.mission_id == mission.mission_id
        and work_item.task_plan_version == mission.current_plan_version
        and work_item.status
        in {
            "queued",
            "running",
            "waiting_human",
            "waiting_external",
            "blocked",
            "retrying",
            "repairing",
            "rolling_back",
            "changing_plan",
            "merging",
            "partial",
        }
        for work_item in control_plane.work_items.values()
    )


def _ready_current_plan_product_work(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    *,
    now: datetime,
) -> list[WorkItem]:
    return [
        item
        for item in control_plane.ready_work_items(mission.mission_id, now=now)
        if item.task_plan_version == mission.current_plan_version
        and not _is_state_preserving_followup(item)
    ]


def _has_ready_current_plan_product_work(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    *,
    now: datetime,
) -> bool:
    return bool(_ready_current_plan_product_work(control_plane, mission, now=now))


def _open_acceptance_pressure_gate_id(
    *,
    mission_id: str,
    task_plan_version: str,
    ticket_id: str,
    context_ref: str,
) -> str:
    digest = _hash_payload(
        {
            "mission_id": mission_id,
            "task_plan_version": task_plan_version,
            "ticket_id": ticket_id,
            "context_ref": context_ref,
        }
    )[:12]
    return f"gate-{_slug(mission_id)}-{_slug(task_plan_version)}-open-acceptance-pressure-{digest}"


def _latest_acceptance_rework_gate(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> GateEvaluation | None:
    rework_actions = {"rejected", "needs_plan_change", "needs_repair"}
    candidates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission.mission_id
        and gate.stage in {"acceptance", "delivery"}
        and (
            mission.current_plan_version is None
            or gate.task_plan_version == mission.current_plan_version
        )
        and (
            gate.next_action in rework_actions
            or gate.north_star_verdict != "pass"
            or gate.hard_gate_failures
            or "human_product_feedback" in gate.governance_signal
            or "user_feedback" in gate.governance_signal
        )
        and not _gate_only_requires_fresh_human_player_review(gate)
        and not _acceptance_gate_recovered_by_later_clean_gate(control_plane, gate)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda gate: gate.gate_evaluation_id)[-1]


def _latest_human_player_review_only_gate(
    control_plane: InMemoryControlPlane,
    mission: Mission,
) -> GateEvaluation | None:
    candidates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission.mission_id
        and gate.stage in {"acceptance", "delivery"}
        and (
            mission.current_plan_version is None
            or gate.task_plan_version == mission.current_plan_version
        )
        and _gate_only_requires_fresh_human_player_review(gate)
        and not _acceptance_gate_recovered_by_later_clean_gate(control_plane, gate)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda gate: gate.gate_evaluation_id)[-1]


def _player_review_ticket_id(*, mission_id: str, gate_id: str) -> str:
    return f"collab-player-review-{_slug(mission_id)}-{_slug(gate_id)}"


def _gate_failure_name(value: str) -> str:
    return value.split(":", 1)[-1].strip().lower()


_HUMAN_PLAYER_ACCEPTANCE_FAILURES = {
    "fresh_real_player_review_pass",
    "human_acceptance_missing",
    "human_or_target_user_acceptance_missing",
    "human_or_target_user_review_missing",
    "human_player_review_missing",
    "human_review_missing",
    "player_acceptance_missing",
    "target_player_acceptance_missing",
    "target_user_acceptance_missing",
}


def _is_human_player_acceptance_failure(value: str) -> bool:
    name = _gate_failure_name(value)
    return name in _HUMAN_PLAYER_ACCEPTANCE_FAILURES or name.endswith(
        (
            "_human_acceptance_missing",
            "_target_user_acceptance_missing",
            "_target_player_acceptance_missing",
            "_player_acceptance_missing",
        )
    )


def _gate_only_requires_fresh_human_player_review(gate: GateEvaluation) -> bool:
    failures = [failure for failure in gate.hard_gate_failures if failure]
    if failures:
        return all(_is_human_player_acceptance_failure(failure) for failure in failures)
    return gate.next_action == "needs_human" and gate.next_state in {
        "awaiting_acceptance",
        "waiting_human",
    }


def _task_plan_for_version(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    plan_version: str,
) -> TaskPlan | None:
    for plan in control_plane.task_plans.values():
        if plan.mission_id == mission_id and plan.version == plan_version:
            return plan
    return None


def _acceptance_rework_plan_version(*, mission: Mission, gate: GateEvaluation) -> str:
    base = gate.task_plan_version or mission.current_plan_version or "product"
    base = _base_acceptance_rework_plan_version(base)
    sig = _hash_payload(
        {
            "gate": gate.gate_evaluation_id,
            "subject": gate.subject_ref,
            "hard_gate_failures": gate.hard_gate_failures,
        }
    )[:8]
    return f"{base}-acceptance-rework-{sig}"


def _base_acceptance_rework_plan_version(plan_version: str) -> str:
    collapsed = _ACCEPTANCE_REWORK_SUFFIX_RE.sub("", plan_version)
    return collapsed.removesuffix("-acceptance-rework")


def _acceptance_rework_task_plan(
    *,
    mission: Mission,
    gate: GateEvaluation,
    base_plan: TaskPlan | None,
    plan_version: str,
    contract: ExecutionContract | None = None,
) -> TaskPlan:
    base_acceptance = list(base_plan.acceptance_criteria) if base_plan else []
    base_constraints = list(base_plan.constraints) if base_plan else []
    rainflow_mode = _is_rainflow_ad_context(mission, contract)
    feedback_constraints = [
        "KUN self-score, residual pass, and one internal gate pass are not final acceptance.",
        "If the user says the product is not close enough, treat it as a hard product gate failure.",
        "Rework must produce fresh browser/playtest evidence before returning to acceptance.",
    ]
    if any("image" in failure or "interaction" in failure for failure in gate.hard_gate_failures):
        feedback_constraints.append(
            "Generated objects must be pictorial, movable stage entities, not text-label cards."
        )
        feedback_constraints.append(
            "Dragging an existing object must move it and trigger object-to-object interactions without duplicating it."
        )
    if _is_full_product_parity_feedback(gate):
        feedback_constraints.append(
            "Do not treat mechanism parity as product parity; UI, character, world art, animation, and player feel are hard gates."
        )
        feedback_constraints.append(
            "KUN must keep iterating until the delivery is a polished playable game, not a dashboard or prototype shell."
        )
    if rainflow_mode:
        feedback_constraints.append(
            "Phase 1 mixed-edit quality must pass before any AI video generation stage can continue."
        )
        feedback_constraints.append(
            "Opening hook, conversion flow, CTA guidance, creator/spoken-sales handling, and smooth pacing are hard ad-product gates."
        )
    return TaskPlan(
        plan_id=f"plan-{_slug(mission.mission_id)}-{_slug(plan_version)}",
        mission_id=mission.mission_id,
        version=plan_version,
        objective=mission.objective,
        known_facts=[
            *(base_plan.known_facts if base_plan else []),
            "Human/product acceptance feedback rejected the previous delivery.",
            gate.root_cause or "The delivered artifact did not meet final player/product feel.",
        ],
        acceptance_criteria=_dedupe(
            [
                *base_acceptance,
                "User feedback has been converted into executable product gates.",
                "The next delivery demonstrates the rejected behavior has been repaired.",
                "Fresh build, browser/playtest, product residual, and final-player-experience gates pass.",
                "The mission returns to awaiting acceptance only after real product evidence is refreshed.",
                *(
                    [
                        "Phase 1 mixed-edit demo comparison proves stronger hook, structure, pacing, and CTA than the previous baseline.",
                        "Stage 1 AI transition clips remain blocked until Phase 1 acceptance evidence is complete.",
                        "Stage 2 regenerated visuals preserve the same meaning while improving ad quality.",
                        "Stage 3 advanced creative blends into the information-flow ad without breaking conversion clarity or pacing.",
                    ]
                    if rainflow_mode
                    else []
                ),
            ]
        ),
        constraints=_dedupe([*base_constraints, *feedback_constraints]),
        risk_register=[
            *(base_plan.risk_register if base_plan else []),
            "Premature closure risk: internal gates can miss subjective game feel and UI parity gaps.",
        ],
        evidence_plan=[
            *(base_plan.evidence_plan if base_plan else []),
            "Capture the acceptance-rework gate, fresh tests, browser evidence, and final player experience gate.",
            *(
                [
                    "Capture baseline-vs-current ad-video comparison artifacts for the rejected RainFlow delivery.",
                ]
                if rainflow_mode
                else []
            ),
        ],
        decomposition=[
            "Reopen the product plan from acceptance feedback.",
            "Repair the concrete rejected interaction/experience gap.",
            "Run fresh internal, browser, residual, and final-player-experience gates.",
            "Deliver again only after evidence proves the new version is closer to the user's target.",
            *(
                [
                    "Keep AI transition/regeneration/advanced-creative stages disabled until Phase 1 mixed-edit quality is accepted.",
                    "Advance RainFlow AI video work in order: Stage 1 transitions, Stage 2 semantic-preserving visual regeneration, then Stage 3 advanced creative blending.",
                ]
                if rainflow_mode
                else []
            ),
        ],
        worker_plan=[
            "KUN-owned product runner performs the implementation.",
            "External supervisor gate reviews final player experience; it does not replace human acceptance.",
        ],
        merge_plan=[
            "Merge only after tests and gates pass; keep previous delivery evidence as superseded history.",
        ],
        test_plan=[
            "Run build and internal tests.",
            "Run visual/sandbox/fun/browser/static/long gates when available.",
            "Run benchmark residual and final-player-experience gates before acceptance.",
            *(
                [
                    "Run hook/structure/CTA demo comparison and creator-spoken-sales handling review before acceptance.",
                    "Run stage-specific semantic-fidelity and creative-blending gates before advancing from Stage 1 to Stage 2 or Stage 3.",
                ]
                if rainflow_mode
                else []
            ),
        ],
        rollback_plan=[
            "If the rework worsens playability, roll back to the last passing product snapshot and reopen Qi/Nuo analysis.",
        ],
        human_confirmation_points=[
            "Only request final acceptance when KUN has fresh evidence and no known rejected product gap remains.",
        ],
        change_log=[
            f"Reopened from acceptance feedback gate {gate.gate_evaluation_id}.",
        ],
        approval_status="approved_with_limits",
    )


def _acceptance_rework_work_items(
    *,
    mission: Mission,
    gate: GateEvaluation,
    plan_version: str,
    contract: ExecutionContract | None,
) -> list[WorkItem]:
    workspace_path = _workspace_path_from_contract(contract)
    workspace_ref = f"workspace://{workspace_path}" if workspace_path else None
    sandbox_ref = _sandbox_ref_from_contract(contract)
    resource_locks = _acceptance_rework_resource_locks(
        mission=mission,
        contract=contract,
        workspace_path=workspace_path,
    )
    plan_signature = _hash_payload(
        {"mission_id": mission.mission_id, "plan_version": plan_version}
    )[:8]
    prefix = f"work-{_slug(mission.mission_id)}-{_slug(plan_version)}-{plan_signature}"
    delivery_policy = contract.delivery_contract if contract is not None else {}
    production_mode = (
        delivery_policy.get("production_mode") if isinstance(delivery_policy, dict) else None
    )
    game_mode = production_mode == "scribble_adventure_functional_parity_v1"
    rainflow_mode = _is_rainflow_ad_context(mission, contract)
    full_parity_feedback = _is_full_product_parity_feedback(gate)
    if game_mode and full_parity_feedback:
        specs = [
            (
                "01-visual-product-iteration",
                "execution",
                "kun-game-production-runner",
                [],
                (
                    "Repair rejected final-game parity gap: world visuals, character presentation, "
                    "object imagery, animation feedback, and UI game feel must read as a playable "
                    "Scribblenauts-style game rather than a tagged prototype."
                ),
            ),
            (
                "02-experience-product-iteration",
                "execution",
                "kun-game-production-runner",
                [f"{prefix}-01-visual-product-iteration"],
                (
                    "Deepen the player loop, level requests, feedback, reward cadence, and tablet "
                    "interaction so the game feels like a finished creative puzzle sandbox."
                ),
            ),
            (
                "03-sandbox-dynamics-iteration",
                "execution",
                "kun-game-production-runner",
                [f"{prefix}-02-experience-product-iteration"],
                (
                    "Strengthen object physics, cause/effect, object-to-object reactions, and "
                    "multi-solution sandbox behavior."
                ),
            ),
            (
                "04-image-object-interaction-iteration",
                "execution",
                "kun-game-production-runner",
                [f"{prefix}-03-sandbox-dynamics-iteration"],
                (
                    "Ensure generated words become pictorial movable game entities, not text labels; "
                    "dragging an existing object moves it and can trigger reactions such as food eaten by NPCs."
                ),
            ),
            (
                "05-commercial-game-polish-iteration",
                "execution",
                "kun-game-production-runner",
                [f"{prefix}-04-image-object-interaction-iteration"],
                (
                    "Continue product-pressure after automated gates: upgrade commercial game UI, "
                    "character reference integration, animation, touch feel, object sprite quality, "
                    "and richer causal reactions before asking for acceptance again."
                ),
            ),
            (
                "06-fun-and-browser-retest",
                "test",
                "kun-game-production-runner",
                [f"{prefix}-05-commercial-game-polish-iteration"],
                "Run fresh build, fun, visual, sandbox, browser, and long-play evidence.",
            ),
            (
                "07-final-player-experience-gate",
                "review",
                "external-supervisor-gpt5.5",
                [f"{prefix}-06-fun-and-browser-retest"],
                "Review product feel against the final game standard; KUN self-score is insufficient.",
            ),
            (
                "08-benchmark-residual-audit",
                "review",
                "kun-game-production-runner",
                [f"{prefix}-07-final-player-experience-gate"],
                "Recompute benchmark residual with visual, UI, interaction, and player-feel gaps included.",
            ),
            (
                "09-final-delivery",
                "merge",
                "kun-game-production-runner",
                [f"{prefix}-08-benchmark-residual-audit"],
                "Deliver again only if the fresh final-player-experience and residual gates pass.",
            ),
        ]
    elif game_mode:
        specs = [
            (
                "01-image-object-interaction-iteration",
                "execution",
                "kun-game-production-runner",
                [],
                (
                    "Repair rejected Scribblenauts parity gap: generated objects must render as "
                    "pictorial game entities, stage drag must move existing objects, and object-to-object "
                    "interactions such as burger feeding wolf must work without duplicate copies."
                ),
            ),
            (
                "02-fun-and-browser-retest",
                "test",
                "kun-game-production-runner",
                [f"{prefix}-01-image-object-interaction-iteration"],
                "Run fresh build, fun, visual, sandbox, browser, and long-play evidence.",
            ),
            (
                "03-final-player-experience-gate",
                "review",
                "external-supervisor-gpt5.5",
                [f"{prefix}-02-fun-and-browser-retest"],
                "Review product feel against the final game standard; KUN self-score is insufficient.",
            ),
            (
                "04-benchmark-residual-audit",
                "review",
                "kun-game-production-runner",
                [f"{prefix}-03-final-player-experience-gate"],
                "Recompute benchmark residual with visual, UI, interaction, and player-feel gaps included.",
            ),
            (
                "05-final-delivery",
                "merge",
                "kun-game-production-runner",
                [f"{prefix}-04-benchmark-residual-audit"],
                "Deliver again only if the fresh final-player-experience and residual gates pass.",
            ),
        ]
    elif rainflow_mode:
        specs = [
            (
                "01-phase1-mixed-edit-repair",
                "execution",
                "kun",
                [],
                (
                    "Repair RainFlow mixed-edit quality first: strengthen opening hook, ad structure, "
                    "conversion logic, CTA guidance, creator/spoken-sales handling, and pacing before "
                    "any AI generation stage continues."
                ),
            ),
            (
                "02-material-screening-and-selection",
                "execution",
                "kun",
                [f"{prefix}-01-phase1-mixed-edit-repair"],
                (
                    "Improve material acquisition, review/screening, and selection so clips support a "
                    "coherent information-flow ad narrative instead of filler assembly."
                ),
            ),
            (
                "03-assembly-and-creator-integration",
                "execution",
                "kun",
                [f"{prefix}-02-material-screening-and-selection"],
                (
                    "Refine mixed-edit assembly with usable creator/spoken-sales segments, smoother "
                    "transitions, and better timing between proof, guidance, and CTA."
                ),
            ),
            (
                "04-phase1-demo-comparison-and-retest",
                "test",
                "kun",
                [f"{prefix}-03-assembly-and-creator-integration"],
                (
                    "Run fresh Phase 1 regression tests and baseline-vs-current demo comparison for "
                    "hook, structure, pacing, CTA, creator handling, and ad polish."
                ),
            ),
            (
                "05-phase1-acceptance-review",
                "review",
                MISSION_DIRECTOR_OWNER,
                [f"{prefix}-04-phase1-demo-comparison-and-retest"],
                (
                    "Review whether Phase 1 mixed-edit quality now clearly beats the previous baseline "
                    "and whether AI generation stages may be reopened."
                ),
            ),
            (
                "06-stage1-transition-generation",
                "execution",
                "kun",
                [f"{prefix}-05-phase1-acceptance-review"],
                (
                    "Only after Phase 1 passes, generate transition clips that improve mixed-edit flow "
                    "without changing ad meaning."
                ),
            ),
            (
                "07-stage1-retest-and-gate",
                "test",
                "kun",
                [f"{prefix}-06-stage1-transition-generation"],
                "Retest transition-clip quality and confirm the mixed-edit meaning remains unchanged.",
            ),
            (
                "08-stage2-visual-regeneration",
                "execution",
                "kun",
                [f"{prefix}-07-stage1-retest-and-gate"],
                (
                    "After Stage 1 passes, regenerate or replace original visuals while preserving the "
                    "same meaning, offer logic, and creator-spoken-sales intent."
                ),
            ),
            (
                "09-stage2-retest-and-gate",
                "test",
                "kun",
                [f"{prefix}-08-stage2-visual-regeneration"],
                (
                    "Retest Stage 2 meaning preservation, visual quality lift, and rollback evidence "
                    "before any advanced creative blending."
                ),
            ),
            (
                "10-stage3-advanced-creative-generation",
                "execution",
                "kun",
                [f"{prefix}-09-stage2-retest-and-gate"],
                (
                    "Create stronger advanced ad creative and blend it into the information-flow ad "
                    "while keeping pacing, guidance, and conversion flow coherent."
                ),
            ),
            (
                "11-stage3-retest-and-gate",
                "test",
                "kun",
                [f"{prefix}-10-stage3-advanced-creative-generation"],
                (
                    "Retest Stage 3 creative blending for ad polish, conversion logic, and safe mixed-edit "
                    "integration."
                ),
            ),
            (
                "12-final-delivery",
                "merge",
                "kun",
                [f"{prefix}-11-stage3-retest-and-gate"],
                (
                    "Deliver again only if refreshed evidence proves Phase 1 quality, Stage 1 safety, "
                    "Stage 2 semantic preservation, and Stage 3 blended ad polish."
                ),
            ),
        ]
    else:
        specs = [
            (
                "01-acceptance-feedback-repair",
                "repair",
                "kun",
                [],
                "Repair the rejected product delivery according to human/product feedback.",
            ),
            (
                "02-acceptance-feedback-retest",
                "retest",
                "kun",
                [f"{prefix}-01-acceptance-feedback-repair"],
                "Retest the repaired product against the user-facing acceptance criteria.",
            ),
        ]
    items: list[WorkItem] = []
    for suffix, item_type, owner, dependencies, expected_output in specs:
        work_item_id = f"{prefix}-{suffix}"
        items.append(
            WorkItem(
                work_item_id=work_item_id,
                mission_id=mission.mission_id,
                task_plan_version=plan_version,
                type=item_type,
                owner=owner,
                phase=_acceptance_rework_phase(owner=owner, suffix=suffix),
                dependencies=dependencies,
                priority=96 if suffix.startswith("01") else 92,
                resource_locks=list(resource_locks),
                idempotency_key=f"acceptance-rework:{gate.gate_evaluation_id}:{work_item_id}",
                expected_output=expected_output,
                workspace_ref=workspace_ref,
                sandbox_ref=sandbox_ref,
                recovery_refs=[gate.gate_evaluation_id],
            )
        )
    return items


def _acceptance_rework_phase(*, owner: str, suffix: str) -> str | None:
    if owner not in {"kun-game-production-runner", "external-supervisor-gpt5.5"}:
        return None
    phase = suffix.split("-", 1)[1] if "-" in suffix else suffix
    return {
        "fun-and-browser-retest": "internal-test",
        "final-player-experience-gate": "supervisor-gate",
    }.get(phase, phase)


def _is_full_product_parity_feedback(gate: GateEvaluation) -> bool:
    text = " ".join(
        [
            gate.root_cause,
            gate.governance_signal,
            *gate.hard_gate_failures,
        ]
    ).lower()
    return any(
        marker in text
        for marker in [
            "full_parity",
            "final_game",
            "gamefeel",
            "player_feel",
            "ui",
            "visual",
            "world",
            "character",
            "animation",
            "scribblenauts",
            "涂鸦",
            "完全一致",
        ]
    )


def _is_rainflow_ad_mode(contract: ExecutionContract | None) -> bool:
    if contract is None or not isinstance(contract.delivery_contract, dict):
        return False
    return contract.delivery_contract.get("production_mode") == RAINFLOW_AD_PRODUCTION_MODE


def _is_rainflow_ad_context(
    mission: Mission,
    contract: ExecutionContract | None,
) -> bool:
    if _is_rainflow_ad_mode(contract):
        return True
    payloads: list[Mapping[str, object]] = []
    if contract is not None:
        payloads.extend(
            [contract.delivery_contract, contract.risk_policy, contract.rollback_policy]
        )
    text = " ".join(
        [
            mission.mission_id,
            mission.objective,
            mission.current_plan_version or "",
            *[json.dumps(payload, sort_keys=True, ensure_ascii=False) for payload in payloads],
        ]
    ).lower()
    return any(
        token in text
        for token in (
            "rainflow",
            "information-flow ad",
            "information_flow_ad",
            "adflow",
            "phase1_mixed_edit",
        )
    )


def _is_rainflow_ad_mission(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    mission = control_plane.missions.get(mission_id)
    contract = (
        control_plane.contracts.get(mission.execution_contract_ref or "") if mission else None
    )
    if _is_rainflow_ad_mode(contract):
        return True
    if mission is not None and _is_rainflow_ad_context(mission, contract):
        return True
    mission_text = " ".join(
        [
            mission_id,
            mission.objective if mission else "",
            mission.current_plan_version if mission and mission.current_plan_version else "",
        ]
    ).lower()
    if "rainflow" in mission_text:
        return True
    return any(
        item.mission_id == mission_id and item.work_item_id.startswith("work-rainflow-")
        for item in control_plane.work_items.values()
    )


def _acceptance_gate_recovered_by_later_clean_gate(
    control_plane: InMemoryControlPlane,
    gate: GateEvaluation,
) -> bool:
    order = _daemon_gate_order_keys(control_plane, gate.mission_id)
    current_order = order.get(gate.gate_evaluation_id, (-1, -1, "", gate.gate_evaluation_id))
    for candidate in control_plane.gate_evaluations.values():
        if candidate.mission_id != gate.mission_id or candidate.stage == "governance":
            continue
        candidate_order = order.get(
            candidate.gate_evaluation_id,
            (-1, -1, "", candidate.gate_evaluation_id),
        )
        if candidate_order <= current_order:
            continue
        if not _daemon_gate_is_clean_pass(candidate):
            continue
        if candidate.subject_ref == gate.subject_ref and candidate.stage == gate.stage:
            return True
        if gate.gate_evaluation_id in _daemon_gate_recovery_refs(control_plane, candidate):
            return True
    return False


def _daemon_gate_order_keys(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> dict[str, tuple[int, int, str, str]]:
    gate_run_order: dict[str, tuple[int, str]] = {}
    for index, run in enumerate(control_plane.runs.values()):
        if run.gate_evaluation_ref:
            ended_at = run.ended_at.isoformat() if run.ended_at else ""
            gate_run_order[run.gate_evaluation_ref] = (index, ended_at)
    gate_ledger_order: dict[str, int] = {}
    for event in control_plane.ledger_events.values():
        if event.mission_id != mission_id or event.event_type != "gate_evaluation":
            continue
        gate_ref = event.payload.get("gate_evaluation_id")
        if isinstance(gate_ref, str):
            gate_ledger_order[gate_ref] = max(
                event.sequence,
                gate_ledger_order.get(gate_ref, -1),
            )
    order: dict[str, tuple[int, int, str, str]] = {}
    for gate in control_plane.gate_evaluations.values():
        if gate.mission_id != mission_id:
            continue
        run_index, run_time = gate_run_order.get(gate.gate_evaluation_id, (-1, ""))
        order[gate.gate_evaluation_id] = (
            gate_ledger_order.get(gate.gate_evaluation_id, -1),
            run_index,
            run_time,
            gate.gate_evaluation_id,
        )
    return order


def _daemon_gate_is_clean_pass(gate: GateEvaluation) -> bool:
    return (
        gate.north_star_verdict == "pass"
        and gate.result_quality >= gate.thresholds.get("result_quality", 0.8)
        and not gate.hard_gate_failures
    )


def _daemon_gate_recovery_refs(
    control_plane: InMemoryControlPlane,
    gate: GateEvaluation,
) -> set[str]:
    refs = {gate.subject_ref, *gate.evidence_refs, *gate.artifact_refs, *gate.review_refs}
    for ref in [*gate.evidence_refs, *gate.artifact_refs, *gate.review_refs]:
        artifact = control_plane.artifacts.get(ref)
        if artifact is None:
            continue
        refs.update(artifact.supports)
        if artifact.work_item_id:
            refs.add(artifact.work_item_id)
    return refs


def _prioritize_ready_work_items_for_daemon(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    ready_items: Sequence[WorkItem],
) -> list[WorkItem]:
    if not _is_rainflow_ad_mission(control_plane, mission_id):
        return list(ready_items)

    def lane(item: WorkItem) -> int:
        if _is_rainflow_core_environment_recovery(item):
            return 0
        if _is_rainflow_core_work_item(item):
            return 1
        if _is_rainflow_failed_or_quality_recovery_followup(item):
            return 2
        if _is_quality_observation_followup(item):
            return 4
        return 3

    return sorted(
        ready_items,
        key=lambda item: (lane(item), -item.priority, item.work_item_id),
    )


def _is_rainflow_core_work_item(work_item: WorkItem) -> bool:
    if _is_rainflow_phase1_acceptance_review(work_item):
        return True
    if _is_rainflow_core_environment_recovery(work_item):
        return True
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.phase or "",
            work_item.task_plan_version or "",
            work_item.expected_output,
        ]
    ).lower()
    return (
        work_item.owner == "kun"
        and any(token in text for token in ("rainflow", "adflow", "seedance", "phase1", "stage1"))
        and work_item.type in {"execution", "test", "review", "research", "merge", "retest"}
    )


def _is_ready_product_core_work(
    control_plane: InMemoryControlPlane,
    *,
    mission: Mission,
    contract: ExecutionContract | None,
    work_item: WorkItem,
) -> bool:
    if work_item.owner == "kun" and work_item.type in {"execution", "test", "review", "merge"}:
        return True
    if work_item.owner == "kun-game-production-runner" and work_item.type in {
        "execution",
        "test",
        "review",
        "merge",
        "retest",
    }:
        return True
    if _is_product_review_or_delivery_gate(work_item):
        return True
    return _is_rainflow_ad_context(mission, contract) and _is_rainflow_core_work_item(work_item)


def _ready_current_plan_product_work(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    *,
    now: datetime | None = None,
) -> list[WorkItem]:
    contract = control_plane.contracts.get(mission.execution_contract_ref or "")
    current_plan_version = mission.current_plan_version
    return [
        item
        for item in control_plane.ready_work_items(mission.mission_id, now=now)
        if (current_plan_version is None or item.task_plan_version == current_plan_version)
        and _is_ready_product_core_work(
            control_plane,
            mission=mission,
            contract=contract,
            work_item=item,
        )
    ]


def _has_ready_current_plan_product_work(
    control_plane: InMemoryControlPlane,
    mission: Mission,
    *,
    now: datetime | None = None,
) -> bool:
    return bool(_ready_current_plan_product_work(control_plane, mission, now=now))


def _has_ready_state_preserving_followup(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    return any(
        _is_state_preserving_followup(item)
        for item in control_plane.ready_work_items(mission_id, now=now)
    )


def _is_rainflow_phase1_acceptance_review(work_item: WorkItem) -> bool:
    return (
        work_item.owner == MISSION_DIRECTOR_OWNER
        and "rainflow" in work_item.work_item_id.lower()
        and "phase1-acceptance-review" in work_item.work_item_id.lower()
    )


def _is_rainflow_core_environment_recovery(work_item: WorkItem) -> bool:
    text = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    ).lower()
    if "rainflow" not in text:
        return False
    if (
        work_item.owner == "nuo"
        and work_item.type == "retest"
        and (work_item.idempotency_key or "").startswith("nuo-clean-retest:")
    ):
        clean_retest_subject = (work_item.idempotency_key or "").split(":", 1)[-1]
        if (
            clean_retest_subject.startswith(("work-nuo-observation-", "work-qi-observation-"))
            or "-observation-" in clean_retest_subject
        ):
            return False
        return any(
            token in text
            for token in (
                "environment blocker",
                "workspace/write/environment",
                "sandbox_permission",
                "permission",
                "wrapper",
                "network",
                "transport",
                "timeout",
                "tool",
            )
        )
    if work_item.owner != "control-plane":
        return False
    if work_item.type not in {"repair", "retest", "test", "execution"}:
        return False
    return (
        "rerun the same subject" in text or "repair wrapper" in text or "fix_wrapper" in text
    ) and (
        "environment blocker" in text
        or "sandbox_permission" in text
        or "permission" in text
        or "wrapper" in text
        or "network" in text
        or "transport" in text
        or "timeout" in text
        or "tool" in text
    )


def _is_quality_observation_followup(work_item: WorkItem) -> bool:
    haystack = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    ).lower()
    if work_item.owner in {"qi", "nuo"} and (
        (work_item.idempotency_key or "").startswith("runtime-observation:")
        or "-observation-" in work_item.work_item_id
    ):
        return True
    return (
        "quality_gate_not_passed" in haystack
        or "quality-gate-not-passed" in haystack
        or "subjective_playtest_missing" in haystack
        or "subjective-playtest-missing" in haystack
        or "mechanical_acceptance_rework_loop" in haystack
    )


def _is_stale_quality_followup(work_item: WorkItem) -> bool:
    if work_item.owner not in {"qi", "nuo"} or work_item.type not in {"governance", "repair"}:
        return False
    haystack = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    ).lower()
    return "quality_gate_not_passed" in haystack or "quality-gate-not-passed" in haystack


def _is_rainflow_failed_or_quality_recovery_followup(work_item: WorkItem) -> bool:
    haystack = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    ).lower()
    if work_item.owner not in {"qi", "nuo"}:
        return False
    if not (
        (work_item.idempotency_key or "").startswith("runtime-observation:")
        or "-observation-" in work_item.work_item_id
    ):
        return False
    return any(
        token in haystack
        for token in (
            "failed_work_recovery_incomplete",
            "failed_work_without_recovery",
            "quality_gate_not_passed",
        )
    )


def _is_mechanical_acceptance_loop_observation_followup(work_item: WorkItem) -> bool:
    haystack = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    )
    return "mechanical_acceptance_rework_loop" in haystack


def _is_substantive_observation_evidence(ref: str) -> bool:
    """Ignore fresh wrapper reports when deciding whether to rerun a follow-up.

    Runtime observations are emitted on every tick.  A new observation artifact
    is useful context, but it is not by itself a new product, gate, or failure
    signal.  Requeue only when a substantive ref such as a gate, work item, or
    external artifact has changed.
    """

    return not (
        ref.startswith("artifact-runtime-observation-")
        or ref.startswith("artifact-daemon-progress-")
    )


def _is_resolved_observation_followup(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> bool:
    key = work_item.idempotency_key or ""
    if key.startswith("runtime-observation:"):
        return work_item.owner in {"qi", "nuo"} or work_item.type in {"governance", "repair"}
    if _is_kun_plan_change_work_item(work_item) and (
        key.startswith("qi-plan-change:work-qi-observation-")
        or any("work-qi-observation-" in ref for ref in work_item.recovery_refs)
    ):
        return True
    if not key.startswith("nuo-recovery:"):
        return False
    parts = key.split(":")
    if len(parts) < 2:
        return False
    subject = control_plane.work_items.get(parts[1])
    return subject is not None and subject.status in {"done", "delivered"}


def _observation_followup_matches_active_code(
    work_item: WorkItem,
    active_codes: set[str],
) -> bool:
    haystack = " ".join(
        [
            work_item.work_item_id,
            work_item.idempotency_key or "",
            work_item.expected_output,
            *work_item.recovery_refs,
        ]
    )
    return any(code in haystack or _slug(code) in haystack for code in active_codes)


def _is_human_collaboration_work_item(work_item: WorkItem) -> bool:
    return work_item.type == "collaboration" or work_item.owner in {"operator", "human"}


def _effective_resource_locks(
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
) -> set[str]:
    locks: set[str] = set()
    for value in work_item.resource_locks:
        normalized = normalize_resource_lock_ref(value)
        if not normalized:
            continue
        if is_pure_governance_work_item(work_item) and is_workspace_resource_lock_ref(normalized):
            continue
        locks.add(normalized)
    if (
        work_item.owner in {"qi", "nuo"}
        or work_item.type in {"governance", "repair", "rollback", "merge"}
        or "plan-change" in work_item.work_item_id
    ):
        locks.add(f"mission-state:{work_item.mission_id}")
    workspace_ref = work_item.workspace_ref
    if workspace_ref and work_item_requires_workspace_boundary(work_item):
        locks.add(normalize_resource_lock_ref(f"workspace:{workspace_ref}"))
    mission = control_plane.missions.get(work_item.mission_id)
    contract = (
        control_plane.contracts.get(mission.execution_contract_ref or "") if mission else None
    )
    workspace_path = _workspace_path_from_contract(contract)
    if workspace_path and work_item_requires_workspace_boundary(work_item):
        locks.add(normalize_resource_lock_ref(f"workspace:{workspace_path}"))
    if work_item_requires_workspace_boundary(work_item) and not _has_workspace_boundary_lock(locks):
        locks.add(f"mission-workspace:{work_item.mission_id}")
    return locks


def _has_workspace_boundary_lock(locks: set[str]) -> bool:
    return any(
        lock.startswith(("workspace:", "worktree:", "project:", "repo:", "mission-workspace:"))
        for lock in locks
    )


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


def _workspace_clean_retest_can_requeue(work_item: WorkItem) -> bool:
    """Re-probe stale human-blocked clean retests before requiring an operator."""

    if work_item.owner != "nuo" or work_item.type != "retest":
        return False
    if not (work_item.idempotency_key or "").startswith("nuo-clean-retest:"):
        return False
    workspace = _workspace_path_from_work_item(work_item)
    if workspace is None or not workspace.exists() or not workspace.is_dir():
        return False
    probe = workspace / f".kun-daemon-clean-retest-{_slug(work_item.work_item_id)}.tmp"
    try:
        probe.write_text("kun daemon clean retest\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _workspace_path_from_work_item(work_item: WorkItem) -> Path | None:
    candidates: list[str] = []
    if work_item.workspace_ref:
        candidates.append(work_item.workspace_ref)
    candidates.extend(lock for lock in work_item.resource_locks if lock.startswith("workspace:"))
    for candidate in candidates:
        path = _local_workspace_path(candidate)
        if path is not None:
            return path
    return None


def _local_workspace_path(value: str) -> Path | None:
    normalized = value.strip()
    if normalized.startswith("workspace://"):
        normalized = normalized.removeprefix("workspace://")
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
    elif normalized.startswith("workspace:"):
        normalized = normalized.removeprefix("workspace:")
    if normalized.startswith("//"):
        normalized = normalized[1:]
    path = Path(normalized).expanduser()
    if not path.is_absolute():
        return None
    return path


def _sandbox_ref_from_contract(contract: ExecutionContract | None) -> str | None:
    if contract is None:
        return None
    for payload in (contract.delivery_contract, contract.risk_policy, contract.rollback_policy):
        value = payload.get("sandbox_ref") if isinstance(payload, dict) else None
        if isinstance(value, str) and value.strip():
            return value
    return None


def _acceptance_rework_resource_locks(
    *,
    mission: Mission,
    contract: ExecutionContract | None,
    workspace_path: str | None,
) -> list[str]:
    locks: list[str] = []
    if workspace_path:
        locks.append(f"workspace:{workspace_path}")
    locks.append(f"mission:{mission.mission_id}")
    if contract is not None:
        delivery = (
            contract.delivery_contract if isinstance(contract.delivery_contract, dict) else {}
        )
        output_dir = delivery.get("output_dir")
        if isinstance(output_dir, str) and output_dir.strip():
            locks.append(f"output_dir:{output_dir}")
        for value in delivery.get("resource_locks", []):
            if isinstance(value, str) and value.strip():
                locks.append(value)
    return _merge_unique(locks)


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    resource_lock_backend: ResourceLockBackend,
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
        resource_lock_backend=resource_lock_backend,
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
