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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.activation import activate_work_item_features
from kun.control_plane.capability_execution import (
    CapabilityExecutionPolicy,
    build_capability_execution_policy,
)
from kun.control_plane.preflight import run_work_item_preflight
from kun.control_plane.runtime import ControlPlaneRunner, InMemoryControlPlane, WorkItemResult
from kun.control_plane.supervisor import MinimalSupervisor, SupervisorFinding
from kun.control_plane.v6 import (
    ArtifactRecord,
    CollaborationTicket,
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
    recovery_gate_refs: list[str] = Field(default_factory=list)
    created_work_item_ids: list[str] = Field(default_factory=list)
    created_collaboration_ticket_ids: list[str] = Field(default_factory=list)
    ran_work_item_ids: list[str] = Field(default_factory=list)
    run_refs: list[str] = Field(default_factory=list)
    no_runner_work_item_ids: list[str] = Field(default_factory=list)
    finalized_mission_ids: list[str] = Field(default_factory=list)
    final_gate_refs: list[str] = Field(default_factory=list)
    delivery_manifest_refs: list[str] = Field(default_factory=list)
    progress_artifact_refs: list[str] = Field(default_factory=list)
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
    ) -> None:
        self.control_plane = control_plane
        self.supervisor = supervisor or MinimalSupervisor()
        self.daemon_id = daemon_id
        self.runners_by_owner = dict(runners_by_owner or {})
        self.runners_by_type = dict(runners_by_type or {})
        self.default_runner = default_runner
        self.rule_engine = rule_engine

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

        for mission_id in selected_mission_ids:
            self._recover_stale_work(mission_id=mission_id, report=report, now=observed_at)

        remaining = max_work_items
        for mission_id in selected_mission_ids:
            while remaining > 0:
                work_item = self.control_plane.next_ready_work_item(mission_id)
                if work_item is None:
                    break
                runner = self._runner_for(work_item)
                if runner is None:
                    report.no_runner_work_item_ids.append(work_item.work_item_id)
                    break
                activation = activate_work_item_features(
                    control_plane=self.control_plane,
                    work_item=work_item,
                    capability_policy=capability_policy,
                    actor=self.daemon_id,
                    observed_at=observed_at,
                )
                self.control_plane.work_items[activation.work_item.work_item_id] = (
                    activation.work_item
                )
                self._persist_work_item(activation.work_item)
                for artifact in activation.artifacts:
                    self._upsert_artifact(artifact)
                    report.activation_artifact_refs.append(artifact.artifact_id)
                preflight = run_work_item_preflight(
                    control_plane=self.control_plane,
                    work_item=activation.work_item,
                    actor=self.daemon_id,
                    observed_at=observed_at,
                )
                preflight_artifact_refs: list[str] = []
                for artifact in preflight.artifacts:
                    self._upsert_artifact(artifact)
                    preflight_artifact_refs.append(artifact.artifact_id)
                    report.preflight_artifact_refs.append(artifact.artifact_id)
                report.preflight_failed_skill_ids.extend(preflight.failed_skill_ids)
                _bind_capability_policy(runner, capability_policy)
                run = self.control_plane.run_next_ready(mission_id=mission_id, runner=runner)
                if run is None:
                    break
                report.ran_work_item_ids.append(run.work_item_id)
                report.run_refs.append(run.run_id)
                fired, error_count = self._evaluate_watchtower_for_run(run)
                report.watchtower_fired_rule_ids.extend(fired)
                report.watchtower_error_count += error_count
                self._queue_preflight_followups(
                    work_item=activation.work_item,
                    failed_skill_ids=preflight.failed_skill_ids,
                    artifact_refs=preflight_artifact_refs,
                    report=report,
                )
                remaining -= 1
                if self.control_plane.missions[mission_id].status not in {"queued", "running"}:
                    break

        for mission_id in selected_mission_ids:
            self._finalize_idle_mission(mission_id=mission_id, report=report)

        if write_progress:
            for mission_id in selected_mission_ids:
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
                priority=85,
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

    def _upsert_artifact(self, artifact: ArtifactRecord) -> None:
        self.control_plane.artifacts[artifact.artifact_id] = artifact
        if self.control_plane.store is not None:
            self.control_plane.store.put_artifact_record(artifact)

    def _persist_work_item(self, work_item: WorkItem) -> None:
        if self.control_plane.store is not None:
            self.control_plane.store.put_work_item(work_item)

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


def _tick_is_idle(report: DaemonTickReport) -> bool:
    return not (
        report.recovered_work_item_ids
        or report.recovery_gate_refs
        or report.created_work_item_ids
        or report.created_collaboration_ticket_ids
        or report.ran_work_item_ids
        or report.run_refs
        or report.no_runner_work_item_ids
        or report.finalized_mission_ids
        or report.final_gate_refs
        or report.delivery_manifest_refs
        or report.watchtower_fired_rule_ids
    )


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
