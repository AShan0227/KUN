from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from kun.control_plane import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ControlPlaneDaemon,
    DaemonServiceConfig,
    DaemonServiceState,
    ExecutionContract,
    FileControlPlaneStore,
    FileDaemonServiceStateStore,
    FileResourceLockStore,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    RunRecord,
    SQLiteResourceLockStore,
    TaskPlan,
    WorkerPoolConfig,
    WorkerSlotSnapshot,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    create_workspace_snapshot,
)
from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.daemon import (
    DaemonTickReport,
    _acceptance_rework_plan_version,
    _acceptance_rework_work_items,
    _effective_resource_locks,
    _has_pending_mechanical_acceptance_loop_followup,
    _latest_acceptance_rework_gate,
    _latest_human_player_review_only_gate,
    _PreparedWorkItemRun,
)
from kun.control_plane.mission_director import MISSION_DIRECTOR_OWNER
from kun.control_plane.preflight import WorkItemPreflight
from kun.control_plane.productization import (
    ProductizationDogfoodRunner,
    build_productization_dogfood_mission,
    close_productization_collaboration_loop,
    distill_external_behavior_signals,
    materialize_external_behavior_distillation,
    submit_productization_dogfood_mission,
)
from kun.control_plane.rainflow_ad_mission import RAINFLOW_AD_PRODUCTION_MODE
from kun.control_plane.runtime_followups import NuoRuntimeRepairRunner
from kun.control_plane.runtime_observation import RuntimeObservationItem, RuntimeObservationReport
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger

NOW = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "daemon-test-runner"

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        return WorkItemResult(status="done", summary="daemon executed ready work")


class ReportArtifactRunner(StaticRunner):
    def run(self, work_item: WorkItem) -> WorkItemResult:
        artifact = ArtifactRecord(
            artifact_id=f"artifact-report-{work_item.work_item_id}",
            kind="report",
            path_or_uri=f"control-plane://test-report/{work_item.mission_id}/{work_item.work_item_id}",
            content_hash=f"sha256:{work_item.work_item_id}",
            created_by=self.runner_identity,
            supports=["runtime_report", "nuo_recovery_report"],
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
        )
        return WorkItemResult(
            status="done",
            summary="daemon executed ready work and produced report evidence",
            artifacts=[artifact],
        )


class PlanChangeRunner(StaticRunner):
    def __init__(self, control_plane: InMemoryControlPlane) -> None:
        self.control_plane = control_plane

    def run(self, work_item: WorkItem) -> WorkItemResult:
        mission = self.control_plane.missions[work_item.mission_id]
        next_plan = TaskPlan(
            plan_id=f"plan-{work_item.mission_id}-v2",
            mission_id=work_item.mission_id,
            version="v2",
            objective=mission.objective,
            acceptance_criteria=["continue only on the fresh plan"],
            approval_status="approved",
        )
        next_contract = ExecutionContract(
            contract_id=f"contract-{work_item.mission_id}-v2",
            mission_id=work_item.mission_id,
            task_plan_version=next_plan.version,
            allowed_actions=["continue fresh plan work"],
        )
        next_context = WorkingContext(
            working_context_id=f"ctx-{work_item.mission_id}-v2",
            mission_id=work_item.mission_id,
            task_plan_version=next_plan.version,
            audience="daemon",
            scope="fresh-plan",
            summary="Fresh plan context.",
            acceptance_criteria=next_plan.acceptance_criteria,
            constraints=["retire superseded plan work"],
        )
        next_work = WorkItem(
            work_item_id="work-fresh-plan",
            mission_id=work_item.mission_id,
            task_plan_version=next_plan.version,
            type="execution",
            owner="kun",
            priority=80,
            expected_output="fresh plan work",
        )
        self.control_plane.record_plan_change(
            mission_id=work_item.mission_id,
            task_plan=next_plan,
            execution_contract=next_contract,
            working_context=next_context,
            work_items=[next_work],
            actor=self.runner_identity,
            reason="runner produced a fresh plan during the daemon tick",
        )
        return WorkItemResult(status="done", summary="created a fresh plan")


def test_nuo_observation_diagnosis_does_not_take_workspace_lock() -> None:
    control_plane = InMemoryControlPlane()
    work = WorkItem(
        work_item_id="work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete-abc",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        workspace_ref="workspace:///tmp/rainflow-workspace",
        idempotency_key="runtime-observation:nuo:work-nuo-observation-msn-rainflow-abc",
        expected_output="Classify this runtime observation before agent scoring.",
    )

    locks = _effective_resource_locks(control_plane, work)

    assert locks == {"mission-state:msn-rainflow"}


def test_nuo_clean_retest_keeps_workspace_lock() -> None:
    control_plane = InMemoryControlPlane()
    work = WorkItem(
        work_item_id="work-nuo-clean-retest-work-nuo-observation-msn-rainflow-abc",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        workspace_ref="workspace:///tmp/rainflow-workspace",
        idempotency_key="nuo-clean-retest:work-nuo-observation-msn-rainflow-abc",
        expected_output="Run a Nuo clean retest for the workspace/write/environment blocker.",
    )

    locks = _effective_resource_locks(control_plane, work)

    assert "mission-state:msn-rainflow" in locks
    assert any(lock.startswith("workspace:") for lock in locks)


def test_qi_strategy_replay_does_not_take_workspace_lock() -> None:
    control_plane = InMemoryControlPlane()
    work = WorkItem(
        work_item_id="work-qi-strategy-replay-work-qi-observation-msn-rainflow-abc",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="research",
        owner="qi",
        workspace_ref="workspace:///tmp/rainflow-workspace",
        idempotency_key="qi-strategy-replay:work-qi-observation-msn-rainflow-abc",
        expected_output=(
            "Run an isolated Qi strategy replay and process audit for the same task slice. "
            "Do not deliver user-task output."
        ),
    )

    locks = _effective_resource_locks(control_plane, work)

    assert locks == {"mission-state:msn-rainflow"}


def test_clean_retest_does_not_requeue_subject_after_new_failed_run() -> None:
    control_plane = InMemoryControlPlane()
    subject = WorkItem(
        work_item_id="work-rainflow-retest",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="test",
        owner="kun",
        status="failed",
        expected_output="Run RainFlow regression checks.",
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        recovery_refs=[subject.work_item_id],
        expected_output="Classify failed_work_recovery_incomplete.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-work-nuo-observation-msn-rainflow",
        mission_id="msn-rainflow",
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        recovery_refs=[partial_repair.work_item_id],
        expected_output="Run clean retest for workspace/write/environment blocker.",
    )
    control_plane.work_items[subject.work_item_id] = subject
    control_plane.work_items[partial_repair.work_item_id] = partial_repair
    control_plane.work_items[clean_retest.work_item_id] = clean_retest
    control_plane.runs["run-clean"] = RunRecord(
        run_id="run-clean",
        work_item_id=clean_retest.work_item_id,
        runner_type="agent",
        runner_identity="nuo",
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=1),
        exit_status="succeeded",
    )
    control_plane.runs["run-subject"] = RunRecord(
        run_id="run-subject",
        work_item_id=subject.work_item_id,
        runner_type="tool",
        runner_identity="kun",
        started_at=NOW + timedelta(seconds=2),
        ended_at=NOW + timedelta(seconds=3),
        exit_status="failed",
        failure_category="environment_failure",
    )
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-test")
    report = DaemonTickReport(
        daemon_id="daemon-test",
        observed_at=NOW + timedelta(seconds=4),
        mission_ids=[subject.mission_id],
    )

    daemon._requeue_failed_subjects_for_completed_clean_retest(
        partial_repair=partial_repair,
        report=report,
    )

    assert control_plane.work_items[subject.work_item_id].status == "failed"
    assert report.recovered_work_item_ids == []


class ServiceHeartbeatProbeRunner(StaticRunner):
    def __init__(self, state_store: FileDaemonServiceStateStore) -> None:
        self.state_store = state_store
        self.observed_state: DaemonServiceState | None = None

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        self.observed_state = FileDaemonServiceStateStore(self.state_store.path).load()
        return WorkItemResult(status="done", summary="daemon observed service heartbeat")


class ContainerCapableRunner(StaticRunner):
    supports_container_sandbox = True


class PolicyAwareRunner(StaticRunner):
    def __init__(self) -> None:
        self.bound_policy: CapabilityExecutionPolicy | None = None

    def bind_capability_execution_policy(self, policy: CapabilityExecutionPolicy) -> None:
        self.bound_policy = policy


class ConcurrentProbeRunner(StaticRunner):
    runner_identity = "daemon-concurrent-probe"

    def __init__(self, *, sleep_sec: float = 0.15) -> None:
        self.sleep_sec = sleep_sec
        self._lock = threading.Lock()
        self.current_running = 0
        self.max_running = 0
        self.started_work_item_ids: list[str] = []

    def run(self, work_item: WorkItem) -> WorkItemResult:
        with self._lock:
            self.current_running += 1
            self.max_running = max(self.max_running, self.current_running)
            self.started_work_item_ids.append(work_item.work_item_id)
        try:
            time.sleep(self.sleep_sec)
            return WorkItemResult(status="done", summary=f"finished {work_item.work_item_id}")
        finally:
            with self._lock:
                self.current_running -= 1


def _runtime(tmp_path, *, retry_budget: int = 0):
    store = FileControlPlaneStore(tmp_path / "daemon-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-daemon",
        owner="kun",
        objective="Run a daemon-managed V6 mission",
        task_type="ops_tooling",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-daemon",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["ready work is executed by daemon"],
        constraints=["state must survive restart"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-daemon",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run local daemon tick"],
        forbidden_actions=["drop durable state"],
    )
    context = WorkingContext(
        working_context_id="ctx-daemon",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="daemon-test",
        summary="Daemon test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_item = WorkItem(
        work_item_id="work-daemon",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=80,
        retry_budget=retry_budget,
        expected_output="daemon-managed result",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    return control_plane, store, mission


def test_daemon_refreshes_shared_work_queue_before_auto_selecting_active_missions(
    tmp_path,
) -> None:
    store_path = tmp_path / "shared-control-plane.json"
    daemon_store = FileControlPlaneStore(store_path)
    daemon_runtime = InMemoryControlPlane(store=daemon_store)
    daemon = ControlPlaneDaemon(
        control_plane=daemon_runtime,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-shared-store-test",
    )

    external_runtime = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    mission = Mission(
        mission_id="msn-shared-store",
        owner="kun",
        objective="Run a mission added after daemon startup.",
        task_type="ops_tooling",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-shared-store",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["daemon sees external store writes"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-shared-store",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run queued work"],
    )
    context = WorkingContext(
        working_context_id="ctx-shared-store",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="shared-store",
        summary="External mission context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["state refresh must not require daemon restart"],
    )
    work_item = WorkItem(
        work_item_id="work-shared-store",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        expected_output="daemon should run this after refresh",
    )
    external_runtime.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )

    report = daemon.tick_once(now=NOW)
    recovered = InMemoryControlPlane(store=FileControlPlaneStore(store_path))

    assert report.mission_ids == ["msn-shared-store"]
    assert report.ran_work_item_ids == ["work-shared-store"]
    assert recovered.work_items["work-shared-store"].status == "done"


def test_daemon_refreshes_scoped_shared_work_queue_for_appended_work_item(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-scoped-refresh-test",
    )
    first = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    assert first.ran_work_item_ids == ["work-daemon"]

    appended = WorkItem(
        work_item_id="work-daemon-appended",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=70,
        expected_output="appended user-task work item from shared queue",
    )
    store.put_work_item(appended)

    second = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(seconds=1),
    )
    recovered = InMemoryControlPlane(store=store)

    assert second.ran_work_item_ids == ["work-daemon-appended"]
    assert recovered.work_items["work-daemon-appended"].status == "done"


def test_daemon_refreshes_shared_queue_even_when_memory_has_ready_work(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-ready-memory-refresh-test",
    )
    appended = WorkItem(
        work_item_id="work-daemon-high-priority-appended",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=100,
        expected_output="externally appended urgent work must outrank stale memory work",
    )
    store.put_work_item(appended)

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-daemon-high-priority-appended"]
    assert recovered.work_items["work-daemon-high-priority-appended"].status == "done"
    assert recovered.work_items["work-daemon"].status == "queued"


def test_daemon_does_not_reload_unchanged_shared_store_for_ready_delta(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-unchanged-store-refresh-test",
    )
    daemon._store_refresh_signature = daemon._current_store_signature()
    reload_calls = 0
    original_reload = store.reload

    def counted_reload() -> None:
        nonlocal reload_calls
        reload_calls += 1
        original_reload()

    store.reload = counted_reload  # type: ignore[method-assign]

    should_refresh = daemon._should_refresh_shared_work_queue(
        mission_ids=[mission.mission_id],
        observed_at=NOW,
    )

    assert should_refresh is False
    assert reload_calls == 0


def test_daemon_skips_human_collaboration_item_without_starving_ready_work(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    human_playtest = WorkItem(
        work_item_id="work-human-playtest-ticket",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="collaboration",
        owner="operator",
        priority=95,
        expected_output="Ask a human for playtest feedback before final delivery.",
    )
    control_plane.work_items[human_playtest.work_item_id] = human_playtest
    control_plane.store.put_work_item(human_playtest)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-human-skip-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )

    assert report.created_collaboration_ticket_ids
    assert report.ran_work_item_ids == ["work-daemon"]
    assert control_plane.work_items["work-daemon"].status == "done"
    assert control_plane.work_items["work-human-playtest-ticket"].status == "waiting_human"


def test_record_plan_change_requeues_waiting_human_mission(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    control_plane.transition_mission(
        mission_id=mission.mission_id,
        target="running",
        actor="test",
        reason="mission started before human wait",
    )
    control_plane.transition_mission(
        mission_id=mission.mission_id,
        target="waiting_human",
        actor="test",
        reason="human playtest ticket opened",
    )
    next_plan = TaskPlan(
        plan_id="plan-daemon-v2",
        mission_id=mission.mission_id,
        version="v2",
        objective="Continue after player feedback.",
        acceptance_criteria=["new approved plan should resume daemon execution"],
        constraints=["human wait must not freeze approved rework forever"],
        approval_status="approved",
    )
    next_contract = ExecutionContract(
        contract_id="contract-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        allowed_actions=["run local daemon tick"],
        forbidden_actions=["drop durable state"],
    )
    next_context = WorkingContext(
        working_context_id="ctx-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        audience="daemon",
        scope="daemon-test",
        summary="Approved rework plan should requeue the mission.",
        acceptance_criteria=next_plan.acceptance_criteria,
        constraints=next_plan.constraints,
    )
    next_work = WorkItem(
        work_item_id="work-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        type="execution",
        owner="kun",
        expected_output="approved rework result",
    )

    updated = control_plane.record_plan_change(
        mission_id=mission.mission_id,
        task_plan=next_plan,
        execution_contract=next_contract,
        working_context=next_context,
        work_items=[next_work],
        actor="test",
        reason="human/player feedback generated an approved rework plan",
    )

    assert updated.status == "queued"
    assert updated.current_plan_version == "v2"


def test_daemon_retires_superseded_stale_work_while_waiting_human(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    stale_old_work = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "running",
            "lease": "lease-old-plan",
            "heartbeat": NOW - timedelta(hours=2),
            "timeout": NOW - timedelta(hours=1),
        }
    )
    control_plane.work_items[stale_old_work.work_item_id] = stale_old_work
    store.put_work_item(stale_old_work)
    next_plan = TaskPlan(
        plan_id="plan-daemon-v2",
        mission_id=mission.mission_id,
        version="v2",
        objective="Continue with a new player-feedback plan.",
        acceptance_criteria=["old plan work is retired before stale recovery"],
        constraints=["waiting human state must not make daemon crash"],
        approval_status="approved",
    )
    next_contract = ExecutionContract(
        contract_id="contract-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        allowed_actions=["run local daemon tick"],
        forbidden_actions=["drop durable state"],
    )
    next_context = WorkingContext(
        working_context_id="ctx-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        audience="daemon",
        scope="daemon-test",
        summary="Superseded stale work is retired while mission waits for a human ticket.",
        acceptance_criteria=next_plan.acceptance_criteria,
        constraints=next_plan.constraints,
    )
    next_work = WorkItem(
        work_item_id="work-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        type="execution",
        owner="kun",
        expected_output="new plan result",
    )
    control_plane.record_plan_change(
        mission_id=mission.mission_id,
        task_plan=next_plan,
        execution_contract=next_contract,
        working_context=next_context,
        work_items=[next_work],
        actor="test",
        reason="approved rework supersedes the stale old plan",
    )
    waiting = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "waiting_human"}
    )
    control_plane.missions[mission.mission_id] = waiting
    store.put_mission(waiting)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-retire-waiting-human-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert stale_old_work.work_item_id in report.retired_work_item_ids
    assert control_plane.work_items[stale_old_work.work_item_id].status == "cancelled"


def test_daemon_requeues_fake_running_work_without_lease_or_run(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    fake_running = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "running",
            "lease": None,
            "heartbeat": None,
            "timeout": None,
        }
    )
    control_plane.work_items[fake_running.work_item_id] = fake_running
    store.put_work_item(fake_running)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-fake-running-recovery-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert fake_running.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[fake_running.work_item_id].status == "queued"
    assert recovered.work_items[fake_running.work_item_id].lease is None
    assert recovered.work_items[fake_running.work_item_id].heartbeat is None


def test_daemon_releases_own_orphaned_queued_lease_after_restart(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    orphaned = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "queued",
            "lease": "lease:daemon-test:worker-1:work-daemon:20260526T001000Z",
            "heartbeat": NOW - timedelta(minutes=1),
            "timeout": NOW + timedelta(minutes=10),
        }
    )
    control_plane.work_items[orphaned.work_item_id] = orphaned
    store.put_work_item(orphaned)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-test")

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert orphaned.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[orphaned.work_item_id].status == "queued"
    assert recovered.work_items[orphaned.work_item_id].lease is None
    assert recovered.work_items[orphaned.work_item_id].heartbeat is None
    assert recovered.work_items[orphaned.work_item_id].timeout is None


def test_daemon_retires_queued_final_delivery_after_cancelled_dependency(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "running",
            "task_type": "product_development",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    residual = WorkItem(
        work_item_id="work-residual-audit",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="review",
        owner="kun-game-production-runner",
        phase="benchmark-residual-audit",
        status="cancelled",
        expected_output="Audit benchmark residuals.",
    )
    delivery = WorkItem(
        work_item_id="work-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun-game-production-runner",
        phase="final-delivery",
        dependencies=[residual.work_item_id],
        expected_output="Write non-final delivery candidate.",
    )
    control_plane.work_items[residual.work_item_id] = residual
    control_plane.work_items[delivery.work_item_id] = delivery
    store.put_work_item(residual)
    store.put_work_item(delivery)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-test")

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert delivery.work_item_id in report.retired_work_item_ids
    assert recovered.work_items[delivery.work_item_id].status == "cancelled"


def test_daemon_retires_superseded_plan_work_created_during_tick(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "post-run-plan-cleanup.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-post-run-plan-cleanup",
        owner="kun",
        objective="Do not let old plan follow-ups pollute the fresh current plan.",
        task_type="product_development",
        status="contracted",
        current_plan_version="v1",
    )
    plan = TaskPlan(
        plan_id="plan-post-run-plan-cleanup-v1",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["old plan follow-ups are retired in the same tick"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-post-run-plan-cleanup-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["change plan"],
    )
    context = WorkingContext(
        working_context_id="ctx-post-run-plan-cleanup-v1",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="plan-cleanup",
        summary="Plan cleanup context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["old follow-ups must not pollute fresh plans"],
    )
    plan_change = WorkItem(
        work_item_id="work-create-fresh-plan",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner="kun",
        priority=100,
        expected_output="create a fresh plan during this tick",
    )
    stale_qi = WorkItem(
        work_item_id="work-stale-qi-followup",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner="qi",
        priority=95,
        expected_output="old strategy replay should not survive a fresh plan",
    )
    stale_nuo = WorkItem(
        work_item_id="work-stale-nuo-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="retest",
        owner="nuo",
        priority=94,
        expected_output="old clean retest should not survive a fresh plan",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[plan_change, stale_qi, stale_nuo],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": PlanChangeRunner(control_plane)},
        daemon_id="post-run-plan-cleanup-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
        write_progress=False,
    )

    assert report.ran_work_item_ids == [plan_change.work_item_id]
    assert control_plane.missions[mission.mission_id].current_plan_version == "v2"
    assert control_plane.work_items[stale_qi.work_item_id].status == "cancelled"
    assert control_plane.work_items[stale_nuo.work_item_id].status == "cancelled"
    assert stale_qi.work_item_id in report.retired_work_item_ids
    assert stale_nuo.work_item_id in report.retired_work_item_ids


def _add_ready_mission(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    work_item_id: str,
    priority: int = 80,
    resource_locks: list[str] | None = None,
    workspace_path: str | None = None,
) -> Mission:
    mission = Mission(
        mission_id=mission_id,
        owner="kun",
        objective=f"Run daemon-managed V6 mission {mission_id}",
        task_type="ops_tooling",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["ready work is executed by daemon"],
        constraints=["state must survive restart"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run local daemon tick"],
        forbidden_actions=["drop durable state"],
        delivery_contract={"workspace_path": workspace_path} if workspace_path else {},
    )
    context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="daemon-test",
        summary="Daemon test context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_item = WorkItem(
        work_item_id=work_item_id,
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=priority,
        expected_output="daemon-managed result",
        resource_locks=resource_locks or [],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    return mission


def _write_passing_ab_round(tmp_path):
    round_dir = tmp_path / "ab-round"
    round_dir.mkdir()
    (round_dir / "report.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "rankings": [
                    {
                        "agent_ref": "kun",
                        "avg_overall_score": 0.95,
                        "avg_effect_score": 0.93,
                        "avg_speed_score": 0.82,
                        "avg_cost_score": 0.81,
                        "avg_engineering_score": 1.0,
                    },
                    {
                        "agent_ref": "hermes",
                        "avg_overall_score": 0.9,
                        "avg_effect_score": 0.89,
                        "avg_speed_score": 0.8,
                        "avg_cost_score": 0.8,
                        "avg_engineering_score": 0.92,
                    },
                ],
                "task_scores": [{"task_id": f"frontier50-r02-t{index}"} for index in range(1, 6)],
                "gaps": [{"capability": "workflow", "delta": 0.0367}],
            }
        ),
        encoding="utf-8",
    )
    (round_dir / "comparator_health.json").write_text(
        json.dumps({"comparator_unhealthy": False}),
        encoding="utf-8",
    )
    (round_dir / "repair_tickets.json").write_text("[]", encoding="utf-8")
    (round_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps({"run": index}) for index in range(20)) + "\n",
        encoding="utf-8",
    )
    (round_dir / "reviews.jsonl").write_text(
        "\n".join(json.dumps({"review": index}) for index in range(45)) + "\n",
        encoding="utf-8",
    )
    return round_dir


def test_daemon_reopens_product_mission_when_acceptance_feedback_rejects_delivery(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "acceptance-rework-control-plane.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-v27",
        mission_id=mission.mission_id,
        version="wordforge-v27",
        objective=mission.objective,
        acceptance_criteria=["final player experience is good enough for the user"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-v27",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["KUN must not close after rejected acceptance feedback."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "awaiting_acceptance"})
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-human-feedback-rejected",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="user-feedback-current-thread",
        stage="acceptance",
        task_type="product_development",
        rubric_version="human-product-acceptance-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.35,
        speed=0.8,
        cost=0.8,
        risk=0.8,
        evidence_quality=0.7,
        collaboration_quality=0.9,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=[
            "ui_visual_parity_gap",
            "image_object_interaction_gap",
            "label_card_generated_objects",
            "copy_on_drag_regression",
        ],
        failure_category="delivery_failure",
        root_cause=(
            "User rejected the delivery: generated objects looked like text labels, dragging "
            "duplicated objects, and stage objects did not interact like game entities."
        ),
        responsibility_scope="kun_auto",
        confidence=0.96,
        next_action="rejected",
        next_state="repairing",
        governance_signal="human_product_feedback_rejected",
        created_by="human-acceptance-gate",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="acceptance-rework-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert reopened.status == "queued"
    assert reopened.current_plan_version is not None
    assert "acceptance-rework" in reopened.current_plan_version
    created = [control_plane.work_items[item_id] for item_id in report.created_work_item_ids]
    created_ids = {item.work_item_id for item in created}
    assert any("visual-product-iteration" in item_id for item_id in created_ids)
    assert any("sandbox-dynamics-iteration" in item_id for item_id in created_ids)
    assert any("image-object-interaction-iteration" in item_id for item_id in created_ids)
    assert any("commercial-game-polish-iteration" in item_id for item_id in created_ids)
    assert any("final-player-experience-gate" in item_id for item_id in created_ids)
    assert any("benchmark-residual-audit" in item_id for item_id in created_ids)
    assert all(item.task_plan_version == reopened.current_plan_version for item in created)
    game_rework_items = [
        item
        for item in created
        if item.owner in {"kun-game-production-runner", "external-supervisor-gpt5.5"}
    ]
    assert game_rework_items
    expected_workspace = f"workspace:{(tmp_path / 'wordforge').resolve()}"
    expected_mission_lock = f"mission:{mission.mission_id}"
    assert all(
        expected_workspace in item.resource_locks and expected_mission_lock in item.resource_locks
        for item in game_rework_items
        if item.type in {"execution", "test", "review", "merge"}
    )
    control_plane.missions[mission.mission_id] = reopened.model_copy(
        update={"status": "delivering"}
    )
    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=False,
    )

    assert second_report.created_work_item_ids == []
    assert control_plane.missions[mission.mission_id].status == "delivering"


def test_daemon_opens_acceptance_rework_from_running_failed_delivery_gate(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "running-delivery-rework.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-running",
        owner="kun",
        objective="Keep improving a word-to-world game until player feel is acceptable.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-running-v56",
        mission_id=mission.mission_id,
        version="wordforge-v56",
        objective=mission.objective,
        acceptance_criteria=["final player experience gate passes with real product evidence"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-running",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-running-v56",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["A failed delivery gate must keep opening product rework."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    running_mission = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "running", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = running_mission
    store.put_mission(running_mission)
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-running-player-gate-failed",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="work-wordforge-running-final-player-experience-gate",
        stage="delivery",
        task_type="product_development",
        rubric_version="external-supervisor-final-player-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.58,
        speed=0.8,
        cost=0.8,
        risk=0.7,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        thresholds={"result_quality": 0.9},
        hard_gate_failures=[
            "player_feel_rework_required",
            "image_object_interaction_gap",
        ],
        failure_category="delivery_failure",
        root_cause=(
            "External supervisor found the latest browser build still lacks "
            "Scribblenauts-like image objects, dragging, and causal feedback."
        ),
        responsibility_scope="kun_auto",
        confidence=0.94,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="external_supervisor_product_gate_failed",
        created_by="external-supervisor-gpt5.5",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="running-delivery-rework-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert reopened.status == "running"
    assert reopened.current_plan_version is not None
    assert "acceptance-rework" in reopened.current_plan_version
    assert reopened.current_plan_version != plan.version
    created = [control_plane.work_items[item_id] for item_id in report.created_work_item_ids]
    created_ids = {item.work_item_id for item in created}
    assert any("image-object-interaction-iteration" in item_id for item_id in created_ids)
    assert any("final-player-experience-gate" in item_id for item_id in created_ids)
    assert any("benchmark-residual-audit" in item_id for item_id in created_ids)
    assert all(item.task_plan_version == reopened.current_plan_version for item in created)
    assert gate.gate_evaluation_id in report.recovery_gate_refs


def test_acceptance_rework_uses_rainflow_steps_without_explicit_production_mode(
    tmp_path: Path,
) -> None:
    mission = Mission(
        mission_id="msn-rainflow-adflow-test",
        owner="kun",
        objective="Improve RainFlow information-flow ad videos.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="rainflow-adflow-v1",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-adflow-test",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-adflow-v1",
        allowed_actions=["write isolated RainFlow workspace"],
        delivery_contract={
            "project_path": str(tmp_path / "rainflow"),
            "output_dir": str(tmp_path / "rainflow-output"),
        },
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-rejected",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow-adflow-v1",
        subject_ref="manifest-rainflow-old",
        stage="acceptance",
        task_type="product_development",
        rubric_version="rainflow-acceptance-v1",
        metric_pack_version="rainflow-ad-v1",
        north_star_verdict="partial",
        result_quality=0.4,
        speed=0.7,
        cost=0.7,
        risk=0.5,
        evidence_quality=0.5,
        collaboration_quality=0.8,
        hard_gate_failures=["opening_hook_weak", "cta_not_clear"],
        next_action="needs_plan_change",
        next_state="changing_plan",
        created_by="mission-director",
    )

    items = _acceptance_rework_work_items(
        mission=mission,
        gate=gate,
        plan_version="rainflow-adflow-v1-acceptance-rework-12345678",
        contract=contract,
    )

    ids = [item.work_item_id for item in items]
    assert any("phase1-mixed-edit-repair" in item_id for item_id in ids)
    assert any("phase1-demo-comparison-and-retest" in item_id for item_id in ids)
    assert any("stage1-transition-generation" in item_id for item_id in ids)
    assert any("stage2-visual-regeneration" in item_id for item_id in ids)
    assert any("stage3-advanced-creative-generation" in item_id for item_id in ids)
    assert any("final-delivery" in item_id for item_id in ids)
    assert not any("acceptance-feedback-repair" in item_id for item_id in ids)


def test_daemon_creates_rainflow_rework_from_changing_plan_human_rejection(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-changing-plan-rework.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-changing-plan",
        owner="kun",
        objective="Improve RainFlow information-flow ad videos.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-changing-plan",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["Human Phase 1 visual acceptance is required before AI stages."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-changing-plan",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write isolated RainFlow workspace"],
        delivery_contract={
            "production_mode": RAINFLOW_AD_PRODUCTION_MODE,
            "project_path": str(tmp_path / "rainflow"),
        },
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-changing-plan",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not continue stale merge work after human rejection."],
    )
    retest = WorkItem(
        work_item_id="work-rainflow-retest-before-human-reject",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        status="done",
        expected_output="Internal retest self-accepted the demo.",
    )
    stale_merge = WorkItem(
        work_item_id="work-kun-merge-after-work-kun-plan-change-rainflow-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun",
        priority=94,
        dependencies=[retest.work_item_id],
        resource_locks=[f"workspace:{tmp_path / 'rainflow'}"],
        expected_output="Stale merge must not run after human rejection.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[retest, stale_merge],
    )
    changing_plan_mission = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "changing_plan"}
    )
    control_plane.missions[mission.mission_id] = changing_plan_mission
    store.put_mission(changing_plan_mission)
    human_gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-human-reject-changing-plan",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref=retest.work_item_id,
        stage="acceptance",
        task_type="product_development",
        rubric_version="rainflow-human-acceptance-v1",
        metric_pack_version="rainflow-ad-v1",
        north_star_verdict="partial",
        result_quality=0.45,
        speed=0.7,
        cost=0.7,
        risk=0.5,
        evidence_quality=0.9,
        collaboration_quality=0.9,
        hard_gate_failures=[
            "first_3s_hook_not_visible_enough",
            "cta_or_offer_not_visible_enough",
        ],
        failure_category="delivery_failure",
        root_cause="Human review rejected the internally accepted RainFlow retest.",
        responsibility_scope="human_collaboration",
        confidence=0.95,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="human_product_feedback_plan_change_required",
        created_by="human-supervisor",
    )
    control_plane.gate_evaluations[human_gate.gate_evaluation_id] = human_gate
    store.put_gate_evaluation(human_gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-rainflow-changing-plan-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert stale_merge.work_item_id in report.retired_work_item_ids
    assert control_plane.work_items[stale_merge.work_item_id].status == "cancelled"
    assert any("phase1-mixed-edit-repair" in item_id for item_id in report.created_work_item_ids)
    assert report.ran_work_item_ids
    assert "phase1-mixed-edit-repair" in report.ran_work_item_ids[0]


def test_rainflow_mechanical_loop_governance_does_not_block_core_rework(
    tmp_path: Path,
) -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rainflow-adflow-test",
        owner="kun",
        objective="Improve RainFlow information-flow ad videos.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-adflow-v1-acceptance-rework-12345678",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-adflow-test",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-adflow-v1",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 core rework continues while governance observes."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-adflow-test",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write isolated RainFlow workspace"],
        delivery_contract={"project_path": str(tmp_path / "rainflow")},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-adflow-test",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow governance should not block core product work.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Qi/Nuo governance is observational for RainFlow core rework."],
    )
    governance = WorkItem(
        work_item_id="work-qi-observation-msn-rainflow-mechanical_acceptance_rework_loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        status="queued",
        expected_output="Audit mechanical_acceptance_rework_loop while product rework continues.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[governance],
    )
    control_plane.missions[mission.mission_id] = control_plane.missions[
        mission.mission_id
    ].model_copy(update={"status": "repairing"})

    assert not _has_pending_mechanical_acceptance_loop_followup(
        control_plane,
        control_plane.missions[mission.mission_id],
    )


def test_acceptance_rework_plan_version_collapses_prior_rework_chain() -> None:
    mission = Mission(
        mission_id="msn-wordforge",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version=(
            "wordforge-v27-acceptance-rework-52b36a00-acceptance-rework-5db5e55c"
        ),
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-external-final-feel-rejected",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "wordforge-v27",
        subject_ref="external-supervisor-final-feel",
        stage="acceptance",
        task_type="product_development",
        rubric_version="external-final-game-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="partial",
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.35,
        evidence_quality=0.86,
        collaboration_quality=0.9,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=["external_final_game_feel_below_95"],
        failure_category="delivery_failure",
        root_cause="External final-game feel review estimated completion below 95%.",
        responsibility_scope="kun_auto",
        confidence=0.91,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="external_supervisor_final_game_feel_below_95",
        created_by="external-supervisor",
    )

    plan_version = _acceptance_rework_plan_version(mission=mission, gate=gate)

    assert plan_version.startswith("wordforge-v27-acceptance-rework-")
    assert plan_version.count("acceptance-rework") == 1
    assert "52b36a00" not in plan_version
    assert "5db5e55c" not in plan_version


def test_daemon_opens_fresh_acceptance_ticket_when_manifest_slug_collides(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "acceptance-ticket-collision.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-v27-rework",
        mission_id=mission.mission_id,
        version="wordforge-v27-acceptance-rework-52b36a00",
        objective=mission.objective,
        acceptance_criteria=["fresh delivery must get fresh human acceptance"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["request product acceptance"],
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun",
        scope="game delivery",
        summary="Delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not reuse stale acceptance tickets across deliveries."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )

    old_manifest_ref = (
        "manifest-msn-wordforge-scribble-adventure-v1-"
        "wordforge-scribble-adventure-v27-fresh-browser-delivery-evidence-playable-game"
    )
    fresh_manifest_ref = (
        "manifest-msn-wordforge-scribble-adventure-v1-"
        "wordforge-scribble-adventure-v27-fresh-browser-delivery-evidence-"
        "acceptance-rework-52b36a00-playable-game"
    )
    for manifest_ref in [old_manifest_ref, fresh_manifest_ref]:
        manifest = ArtifactManifest(
            manifest_id=manifest_ref,
            mission_id=mission.mission_id,
            kind="delivery",
            artifact_refs=[f"artifact-{manifest_ref[-12:]}"],
            primary_artifact_ref=f"artifact-{manifest_ref[-12:]}",
            evidence_refs=[f"artifact-{manifest_ref[-12:]}"],
            rollback_refs=[f"snapshot-{manifest_ref[-12:]}"],
            created_by="kun",
            content_hash=manifest_ref,
            supports_delivery=True,
        )
        control_plane.artifact_manifests[manifest.manifest_id] = manifest
        store.put_artifact_manifest(manifest)

    old_ticket = CollaborationTicket(
        ticket_id=(
            "collab-acceptance-msn-wordforge-manifest-msn-wordforge-scribble-"
            "adventure-v1-wordforge-scribble-adventure-v27-fr"
        ),
        mission_id=mission.mission_id,
        type="review",
        role_needed="kun",
        why_needed="Old delivery needs acceptance.",
        context_ref=old_manifest_ref,
        risk_if_skipped="Old ticket must not hide a fresh delivery.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Accept or reject.",
    )
    control_plane.collaboration_tickets[old_ticket.ticket_id] = old_ticket
    store.put_collaboration_ticket(old_ticket)
    submitted = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [old_manifest_ref, fresh_manifest_ref],
        }
    )
    control_plane.missions[mission.mission_id] = submitted
    store.put_mission(submitted)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="acceptance-ticket-collision-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert len(report.created_collaboration_ticket_ids) == 1
    fresh_ticket = control_plane.collaboration_tickets[report.created_collaboration_ticket_ids[0]]
    assert fresh_ticket.context_ref == fresh_manifest_ref
    assert fresh_ticket.ticket_id != old_ticket.ticket_id
    assert control_plane.collaboration_tickets[old_ticket.ticket_id].status == "cancelled"
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"


def test_daemon_opens_fresh_acceptance_ticket_when_same_manifest_is_refreshed(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "acceptance-ticket-refresh.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow",
        owner="kun",
        objective="Ship a stronger information-flow ad edit.",
        task_type="product_development",
        status="contracted",
    )
    plan_v1 = TaskPlan(
        plan_id="plan-rainflow-v1",
        mission_id=mission.mission_id,
        version="rainflow-v1",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow",
        mission_id=mission.mission_id,
        task_plan_version=plan_v1.version,
        allowed_actions=["request product acceptance"],
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow",
        mission_id=mission.mission_id,
        task_plan_version=plan_v1.version,
        audience="kun",
        scope="ad delivery",
        summary="Delivery context.",
        acceptance_criteria=plan_v1.acceptance_criteria,
        constraints=["Do not reuse answered acceptance tickets across refreshed deliveries."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan_v1,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    manifest_id = "manifest-kun-runtime-delivery-msn-rainflow"
    first_manifest = ArtifactManifest(
        manifest_id=manifest_id,
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-rainflow-v1"],
        primary_artifact_ref="artifact-rainflow-v1",
        evidence_refs=["artifact-rainflow-v1"],
        rollback_refs=["snapshot-rainflow-v1"],
        created_by="kun",
        content_hash="rainflow-v1-hash",
        supports_delivery=True,
    )
    control_plane.artifact_manifests[first_manifest.manifest_id] = first_manifest
    store.put_artifact_manifest(first_manifest)
    delivering_v1 = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "current_plan_version": plan_v1.version,
            "artifact_manifest_refs": [manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = delivering_v1
    store.put_mission(delivering_v1)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="acceptance-ticket-refresh-daemon-test",
    )

    first_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )
    first_ticket_id = first_report.created_collaboration_ticket_ids[0]
    answered_ticket = control_plane.collaboration_tickets[first_ticket_id].model_copy(
        update={"status": "answered"}
    )
    control_plane.collaboration_tickets[first_ticket_id] = answered_ticket
    store.put_collaboration_ticket(answered_ticket)
    second_manifest = first_manifest.model_copy(
        update={
            "artifact_refs": ["artifact-rainflow-v2"],
            "primary_artifact_ref": "artifact-rainflow-v2",
            "evidence_refs": ["artifact-rainflow-v2"],
            "rollback_refs": ["snapshot-rainflow-v2"],
            "content_hash": "rainflow-v2-hash",
        }
    )
    control_plane.artifact_manifests[second_manifest.manifest_id] = second_manifest
    store.put_artifact_manifest(second_manifest)
    delivering_v2 = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "current_plan_version": "rainflow-v2",
            "artifact_manifest_refs": [manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = delivering_v2
    store.put_mission(delivering_v2)

    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=False,
    )
    third_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=2),
        max_work_items=0,
        write_progress=False,
    )

    assert len(second_report.created_collaboration_ticket_ids) == 1
    second_ticket_id = second_report.created_collaboration_ticket_ids[0]
    assert second_ticket_id != first_ticket_id
    assert control_plane.collaboration_tickets[second_ticket_id].context_ref == manifest_id
    assert control_plane.collaboration_tickets[second_ticket_id].status == "open"
    assert third_report.created_collaboration_ticket_ids == []


def test_acceptance_rework_work_item_ids_keep_plan_hash_after_long_slug() -> None:
    mission = Mission(
        mission_id="msn-wordforge-scribble-adventure-v1",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="queued",
        current_plan_version=(
            "wordforge-scribble-adventure-v36-input-visible-tap-fallback-retest-"
            "20260523-acceptance-rework-11111111"
        ),
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "v1",
        allowed_actions=["write_game_project"],
        delivery_contract={
            "project_path": "/tmp/wordforge",
            "production_mode": "scribble_adventure_functional_parity_v1",
        },
    )
    first_gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-acceptance-pressure-1",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "v1",
        stage="acceptance",
        task_type="product_development",
        rubric_version="rubric",
        metric_pack_version="metrics",
        subject_ref="ticket-1",
        north_star_verdict="partial",
        result_quality=0.5,
        speed=0.7,
        cost=0.7,
        risk=0.3,
        evidence_quality=0.6,
        collaboration_quality=0.8,
        next_action="needs_repair",
        next_state="repairing",
        root_cause="visual ui animation gamefeel gap",
        hard_gate_failures=["visual_gap"],
        created_by="test",
    )
    second_plan_version = (
        "wordforge-scribble-adventure-v36-input-visible-tap-fallback-retest-"
        "20260523-acceptance-rework-22222222"
    )
    second_gate = first_gate.model_copy(
        update={
            "gate_evaluation_id": "gate-wordforge-acceptance-pressure-2",
            "task_plan_version": second_plan_version,
        }
    )

    first_ids = {
        item.work_item_id
        for item in _acceptance_rework_work_items(
            mission=mission,
            gate=first_gate,
            plan_version=mission.current_plan_version or "v1",
            contract=contract,
        )
    }
    second_ids = {
        item.work_item_id
        for item in _acceptance_rework_work_items(
            mission=mission,
            gate=second_gate,
            plan_version=second_plan_version,
            contract=contract,
        )
    }

    assert first_ids.isdisjoint(second_ids)
    assert any("01-visual-product-iteration" in item_id for item_id in first_ids)
    assert any("01-visual-product-iteration" in item_id for item_id in second_ids)


def test_daemon_moves_completed_running_product_delivery_to_awaiting_acceptance(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "running-delivery-acceptance.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-running-delivery",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-running-delivery",
        mission_id=mission.mission_id,
        version="wordforge-v28",
        objective=mission.objective,
        acceptance_criteria=["fresh delivery must get human acceptance"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-running-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["request product acceptance"],
        delivery_contract={"project_path": str(tmp_path / "wordforge")},
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-running-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun",
        scope="game delivery",
        summary="Delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Completed delivery should not remain running."],
    )
    work_item = WorkItem(
        work_item_id="work-wordforge-running-delivery-final",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun",
        status="done",
        expected_output="final delivery",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-wordforge-running-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-wordforge-running-delivery"],
        primary_artifact_ref="artifact-wordforge-running-delivery",
        evidence_refs=["artifact-wordforge-running-evidence"],
        rollback_refs=["snapshot-wordforge-running-delivery"],
        created_by="kun",
        content_hash="running-delivery",
        supports_delivery=True,
    )
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    store.put_artifact_manifest(manifest)
    submitted = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "running",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = submitted
    store.put_mission(submitted)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="running-delivery-acceptance-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert len(report.created_collaboration_ticket_ids) == 1

    regressed = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "delivering", "acceptance_ref": None}
    )
    control_plane.missions[mission.mission_id] = regressed
    store.put_mission(regressed)
    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(seconds=1),
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert second_report.created_collaboration_ticket_ids == []


def test_daemon_keeps_product_pressure_while_acceptance_is_open(tmp_path: Path) -> None:
    store = FileControlPlaneStore(tmp_path / "open-acceptance-pressure.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-pressure",
        owner="kun",
        objective="Ship a commercial-feeling word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-pressure",
        mission_id=mission.mission_id,
        version="wordforge-v28",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-pressure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-pressure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not idle in awaiting_acceptance without human acceptance."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-wordforge-pressure-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-wordforge-pressure-delivery"],
        primary_artifact_ref="artifact-wordforge-pressure-delivery",
        evidence_refs=["artifact-wordforge-pressure-delivery"],
        rollback_refs=["snapshot-wordforge-pressure-delivery"],
        created_by="kun-game-production-runner",
        content_hash="pressure-delivery",
        supports_delivery=True,
    )
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    store.put_artifact_manifest(manifest)
    submitted = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = submitted
    store.put_mission(submitted)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="open-acceptance-pressure-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    created_ids = set(report.created_work_item_ids)
    assert reopened.status == "queued"
    assert "acceptance-rework" in (reopened.current_plan_version or "")
    assert any("open-acceptance-pressure" in gate_ref for gate_ref in report.recovery_gate_refs)
    assert any("commercial-game-polish-iteration" in item_id for item_id in created_ids)
    assert any("visual-product-iteration" in item_id for item_id in created_ids)
    assert len(report.created_collaboration_ticket_ids) == 1


def test_daemon_reopens_product_pressure_in_same_tick_after_finalization(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "same-tick-product-pressure.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-same-tick-pressure",
        owner="kun",
        objective="Do not stop after automated delivery gates pass.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-same-tick-pressure",
        mission_id=mission.mission_id,
        version="wordforge-v51",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-same-tick-pressure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
            "auto_continue_until_human_acceptance": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-same-tick-pressure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Automated delivery gates must not replace human acceptance."],
    )
    done_work = WorkItem(
        work_item_id="work-wordforge-v51-final-browser-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        status="done",
        expected_output="Browser gate passed, but this is not human acceptance.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[done_work],
    )
    artifact = ArtifactRecord(
        artifact_id="artifact-wordforge-same-tick-final-browser-gate",
        kind="review",
        path_or_uri="control-plane://wordforge/final-browser-gate",
        content_hash="same-tick-browser-gate",
        created_by="external-real-player-supervisor",
        mission_id=mission.mission_id,
        supports=["final_player_experience_gate", "browser_playtest"],
        freshness="fresh",
        source_quality="primary",
    )
    control_plane.artifacts[artifact.artifact_id] = artifact
    store.put_artifact_record(artifact)
    running = control_plane.missions[mission.mission_id].model_copy(update={"status": "running"})
    control_plane.missions[mission.mission_id] = running
    store.put_mission(running)

    class SameTickDeliveryRunner:
        runner_type: Literal["agent"] = "agent"
        runner_identity = "same-tick-delivery-runner"

        def finalize_mission(self, mission_id: str) -> dict[str, object]:
            current = control_plane.missions[mission_id]
            manifest = ArtifactManifest(
                manifest_id="manifest-wordforge-same-tick-delivery",
                mission_id=mission_id,
                kind="delivery",
                artifact_refs=[artifact.artifact_id],
                primary_artifact_ref=artifact.artifact_id,
                evidence_refs=[artifact.artifact_id],
                rollback_refs=["snapshot-wordforge-same-tick"],
                created_by=self.runner_identity,
                content_hash="same-tick-delivery",
                supports_delivery=True,
            )
            control_plane.artifact_manifests[manifest.manifest_id] = manifest
            store.put_artifact_manifest(manifest)
            gate = GateEvaluation(
                gate_evaluation_id="gate-wordforge-same-tick-delivery",
                mission_id=mission_id,
                task_plan_version=current.current_plan_version or plan.version,
                subject_ref=manifest.manifest_id,
                stage="delivery",
                task_type="product_development",
                rubric_version="same-tick-delivery-v1",
                metric_pack_version="same-tick-delivery-v1",
                north_star_verdict="pass",
                result_quality=0.9,
                speed=0.7,
                cost=0.7,
                risk=0.2,
                evidence_quality=0.86,
                collaboration_quality=0.7,
                score_breakdown={"automated_delivery_gate_passed": 1.0},
                thresholds={"result_quality": 0.8},
                evidence_refs=[artifact.artifact_id],
                artifact_refs=[artifact.artifact_id],
                source_freshness="fresh",
                responsibility_scope="kun_auto",
                confidence=0.8,
                next_action="ready_to_deliver",
                next_state="delivering",
                governance_signal="automated_delivery_ready",
                created_by=self.runner_identity,
            )
            control_plane.apply_gate(gate)
            updated = control_plane.missions[mission_id].model_copy(
                update={"artifact_manifest_refs": [manifest.manifest_id]}
            )
            control_plane.missions[mission_id] = updated
            store.put_mission(updated)
            return {
                "finalized": True,
                "delivery_manifest_ref": manifest.manifest_id,
                "final_gate_ref": gate.gate_evaluation_id,
            }

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": SameTickDeliveryRunner()},
        daemon_id="same-tick-product-pressure-daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert report.finalized_mission_ids == [mission.mission_id]
    assert len(report.created_collaboration_ticket_ids) == 1
    assert any("open-acceptance-pressure" in gate_ref for gate_ref in report.recovery_gate_refs)
    assert reopened.status == "queued"
    assert "acceptance-rework" in (reopened.current_plan_version or "")


def test_daemon_waits_for_qi_nuo_when_acceptance_rework_loop_is_under_audit(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "mechanical-loop-pressure.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-loop",
        owner="kun",
        objective="Ship a finished word-to-world game without mechanical rework churn.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-loop",
        mission_id=mission.mission_id,
        version="wordforge-v36-acceptance-rework-11111111",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Qi/Nuo loop audit must complete before more product rework."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-wordforge-loop-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-wordforge-loop-delivery"],
        primary_artifact_ref="artifact-wordforge-loop-delivery",
        evidence_refs=["artifact-wordforge-loop-delivery"],
        rollback_refs=["snapshot-wordforge-loop-delivery"],
        created_by="kun-game-production-runner",
        content_hash="loop-delivery",
        supports_delivery=True,
    )
    ticket = CollaborationTicket(
        ticket_id="ticket-wordforge-loop-acceptance",
        mission_id=mission.mission_id,
        type="review",
        role_needed="mission-owner",
        why_needed="Human acceptance is required.",
        context_ref=manifest.manifest_id,
        risk_if_skipped="Mechanical delivery loops may hide product stagnation.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Return accept / rework / reject with concrete product feedback.",
    )
    loop_followup = WorkItem(
        work_item_id=(
            "work-qi-observation-msn-wordforge-loop-mechanical_acceptance_rework_loop-123456789abc"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        status="queued",
        priority=98,
        expected_output=(
            "Run strategy replay for mechanical_acceptance_rework_loop before more rework."
        ),
        recovery_refs=["gate-loop-pressure-1", "gate-mission-director-block-1"],
    )
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.collaboration_tickets[ticket.ticket_id] = ticket
    control_plane.work_items[loop_followup.work_item_id] = loop_followup
    reopened = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = reopened
    store.put_artifact_manifest(manifest)
    store.put_collaboration_ticket(ticket)
    store.put_work_item(loop_followup)
    store.put_mission(reopened)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="mechanical-loop-pressure-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert report.created_work_item_ids == []
    assert report.recovery_gate_refs == []
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert control_plane.missions[mission.mission_id].current_plan_version == plan.version


def test_daemon_waits_for_acceptance_after_fresh_rework_evidence(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "wordforge"
    docs_path = project_path / "docs"
    docs_path.mkdir(parents=True)
    (docs_path / "final-player-experience-gate.json").write_text(
        json.dumps(
            {
                "pass": True,
                "score": 0.96,
                "threshold": 0.95,
                "dimension_floor": 0.9,
                "dimensions": {
                    "visual_product": {"score": 0.94},
                    "drag_feel": {"score": 0.93},
                    "causality": {"score": 0.92},
                },
            }
        )
    )
    (docs_path / "benchmark-residual-audit.json").write_text(
        json.dumps({"pass": True, "overall_residual": 0.001, "threshold": 0.003})
    )
    store = FileControlPlaneStore(tmp_path / "fresh-rework-evidence-waits.json")
    control_plane = InMemoryControlPlane(store=store)
    plan_version = "wordforge-v56-acceptance-rework-current"
    mission = Mission(
        mission_id="msn-wordforge-fresh-evidence",
        owner="kun",
        objective="Ship a polished word-to-world game.",
        task_type="product_development",
        status="contracted",
        current_plan_version=plan_version,
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-fresh-evidence",
        mission_id=mission.mission_id,
        version=plan_version,
        objective=mission.objective,
        acceptance_criteria=["Fresh product evidence should wait for human acceptance."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-fresh-evidence",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(project_path),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
            "benchmark_residual_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-fresh-evidence",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Fresh product evidence exists.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not open another mechanical acceptance-rework branch."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    ticket = CollaborationTicket(
        ticket_id="ticket-wordforge-fresh-evidence-acceptance",
        mission_id=mission.mission_id,
        type="review",
        role_needed="mission-owner",
        why_needed="Human acceptance is required after fresh product evidence.",
        context_ref="manifest-wordforge-fresh-evidence",
        risk_if_skipped="KUN may keep opening mechanical rework branches.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Return accept / rework / reject with concrete product feedback.",
    )
    control_plane.collaboration_tickets[ticket.ticket_id] = ticket
    store.put_collaboration_ticket(ticket)
    awaiting = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "awaiting_acceptance", "current_plan_version": plan_version}
    )
    control_plane.missions[mission.mission_id] = awaiting
    store.put_mission(awaiting)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="fresh-rework-evidence-waits-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert report.recovery_gate_refs == []
    assert report.created_work_item_ids == []
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert control_plane.missions[mission.mission_id].current_plan_version == plan_version
    assert not any(
        gate.governance_signal == "open_acceptance_requires_continued_product_pressure"
        for gate in control_plane.gate_evaluations.values()
    )


def test_daemon_does_not_open_new_rework_when_current_report_detects_loop(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "current-report-loop-block.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-current-loop",
        owner="kun",
        objective="Ship a finished word-to-world game without mechanical rework churn.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-current-loop",
        mission_id=mission.mission_id,
        version="wordforge-v56-acceptance-rework-11111111",
        objective=mission.objective,
        acceptance_criteria=[
            "current strategy must change before another acceptance rework branch"
        ],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-current-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-current-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not spawn another rework branch while the current tick reports a loop."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-current-loop-failed-player-feel",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="work-wordforge-current-loop-final-player-experience-gate",
        stage="delivery",
        task_type="product_development",
        rubric_version="external-final-game-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.78,
        speed=0.7,
        cost=0.7,
        risk=0.42,
        evidence_quality=0.82,
        collaboration_quality=0.76,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=["mechanical_acceptance_rework_loop", "real_player_feel_gap"],
        failure_category="delivery_failure",
        root_cause="Repeated acceptance rework produced no clear product-feel strategy delta.",
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="external_supervisor_real_player_gap_requires_iteration",
        created_by="external-supervisor-gpt5.5",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    current = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "changing_plan", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = current
    store.put_mission(current)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="current-report-loop-block-daemon-test",
    )
    report = DaemonTickReport(
        daemon_id=daemon.daemon_id,
        observed_at=NOW,
        mission_ids=[mission.mission_id],
        runtime_observations={
            mission.mission_id: RuntimeObservationReport(
                mission_id=mission.mission_id,
                requires_external_supervision=True,
                items=[
                    RuntimeObservationItem(
                        code="mechanical_acceptance_rework_loop",
                        severity="high",
                        title="验收返工可能在机械循环",
                        why_watch="当前 tick 已经发现同类返工循环。",
                        recommended_action="先让 Qi/Nuo/Mission Director 改策略再继续。",
                        routes=["qi", "nuo", "external_supervisor"],
                        evidence_refs=[gate.gate_evaluation_id],
                    )
                ],
            )
        },
    )

    daemon._ensure_acceptance_rework_from_feedback(
        mission_id=mission.mission_id,
        observed_at=NOW,
        report=report,
    )

    assert report.created_work_item_ids == []
    assert report.recovery_gate_refs == []
    assert control_plane.missions[mission.mission_id].status == "changing_plan"
    assert control_plane.missions[mission.mission_id].current_plan_version == plan.version


def test_daemon_queues_nuo_clean_retest_after_partial_loop_repair(tmp_path: Path) -> None:
    store = FileControlPlaneStore(tmp_path / "mechanical-loop-clean-retest.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    mission = Mission(
        mission_id="msn-wordforge-loop",
        owner="kun",
        objective="Ship a finished word-to-world game without mechanical rework churn.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-loop",
        mission_id=mission.mission_id,
        version="wordforge-v36-acceptance-rework-11111111",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(project_path),
            "production_mode": "scribble_adventure_functional_parity_v1",
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Nuo diagnosis must produce clean retest evidence before closure."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-observation-msn-wordforge-loop-mechanical_acceptance_rework_loop-abc123",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="nuo",
        status="partial",
        priority=98,
        expected_output="Classify mechanical_acceptance_rework_loop before agent scoring.",
        recovery_refs=["gate-loop-pressure-1"],
    )
    reopened = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "awaiting_acceptance", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = reopened
    control_plane.work_items[partial_repair.work_item_id] = partial_repair
    store.put_mission(reopened)
    store.put_work_item(partial_repair)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="mechanical-loop-clean-retest-daemon-test",
        runners_by_owner={"nuo": NuoRuntimeRepairRunner(control_plane=control_plane)},
    )
    report = DaemonTickReport(
        daemon_id="mechanical-loop-clean-retest-daemon-test",
        observed_at=NOW,
    )

    daemon._queue_observation_followup(
        mission_id=mission.mission_id,
        owner="nuo",
        item_type="repair",
        work_item_id=partial_repair.work_item_id,
        expected_output=partial_repair.expected_output,
        evidence_refs=["artifact-runtime-observation-loop"],
        required=True,
        priority=98,
        report=report,
    )

    assert _has_pending_mechanical_acceptance_loop_followup(control_plane, reopened)
    retest_id = report.observation_followup_ids[0]
    retest = control_plane.work_items[retest_id]
    assert retest.owner == "nuo"
    assert retest.type == "retest"
    assert retest.workspace_ref == str(project_path)
    assert "mechanical_acceptance_rework_loop" in retest.expected_output
    assert "workspace/write" not in retest.expected_output
    assert partial_repair.work_item_id in retest.recovery_refs

    daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    clean_retest = control_plane.work_items[retest_id]
    assert clean_retest.status == "done"
    loop_gates = [
        gate for gate in control_plane.gate_evaluations.values() if gate.subject_ref == retest_id
    ]
    assert loop_gates
    assert loop_gates[0].governance_signal == "nuo_loop_clean_retest_passed"
    assert not _has_pending_mechanical_acceptance_loop_followup(control_plane, reopened)


def test_daemon_clean_retest_ids_do_not_collide_for_long_partial_repair_ids(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "clean-retest-collision.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-clean-retest-collision",
        owner="kun",
        objective="Recover each failed work item independently.",
        task_type="product_development",
        status="repairing",
        current_plan_version="v1",
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    common_prefix = (
        "work-nuo-observation-msn-wordforge-scribble-adventure-v1-failed_work_recovery_incomplete-"
    )
    partials = [
        WorkItem(
            work_item_id=f"{common_prefix}{suffix}",
            mission_id=mission.mission_id,
            task_plan_version="v1",
            type="repair",
            owner="nuo",
            status="partial",
            recovery_refs=[f"work-failed-{suffix}"],
            expected_output="diagnosis still needs a clean retest",
        )
        for suffix in ("first-long-tail", "second-long-tail")
    ]
    for partial in partials:
        control_plane.work_items[partial.work_item_id] = partial
        store.put_work_item(partial)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="clean-retest-collision-daemon-test",
        runners_by_owner={"nuo": NuoRuntimeRepairRunner(control_plane=control_plane)},
    )
    report = DaemonTickReport(
        daemon_id="clean-retest-collision-daemon-test",
        observed_at=NOW,
    )

    for partial in partials:
        daemon._queue_observation_followup(
            mission_id=mission.mission_id,
            owner="nuo",
            item_type="repair",
            work_item_id=partial.work_item_id,
            expected_output=partial.expected_output,
            evidence_refs=partial.recovery_refs,
            required=True,
            priority=90,
            report=report,
        )

    assert len(report.observation_followup_ids) == 2
    assert len(set(report.observation_followup_ids)) == 2
    for partial in partials:
        assert any(
            partial.work_item_id in control_plane.work_items[followup_id].recovery_refs
            for followup_id in report.observation_followup_ids
        )


def test_daemon_does_not_let_stale_quality_strategy_replay_block_acceptance_rework(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "quality-strategy-loop-pressure.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-loop",
        owner="kun",
        objective="Ship a finished word-to-world game without mechanical rework churn.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-loop",
        mission_id=mission.mission_id,
        version="wordforge-v36-acceptance-rework-11111111",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Pure Qi strategy replay must not block core product rework."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-wordforge-loop-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-wordforge-loop-delivery"],
        primary_artifact_ref="artifact-wordforge-loop-delivery",
        evidence_refs=["artifact-wordforge-loop-delivery"],
        rollback_refs=["snapshot-wordforge-loop-delivery"],
        created_by="kun-game-production-runner",
        content_hash="loop-delivery",
        supports_delivery=True,
    )
    ticket = CollaborationTicket(
        ticket_id="ticket-wordforge-loop-acceptance",
        mission_id=mission.mission_id,
        type="review",
        role_needed="mission-owner",
        why_needed="Human acceptance is required.",
        context_ref=manifest.manifest_id,
        risk_if_skipped="Mechanical delivery loops may hide product stagnation.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Return accept / rework / reject with concrete product feedback.",
    )
    strategy_replay = WorkItem(
        work_item_id=(
            "work-qi-strategy-replay-work-qi-observation-msn-wordforge-loop-"
            "quality_gate_not_passed-123456789abc"
        ),
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v35",
        type="governance",
        owner="qi",
        status="queued",
        priority=90,
        expected_output="Run an isolated Qi strategy replay before more acceptance rework.",
        recovery_refs=["gate-quality-gate-not-passed"],
    )
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.collaboration_tickets[ticket.ticket_id] = ticket
    control_plane.work_items[strategy_replay.work_item_id] = strategy_replay
    reopened = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = reopened
    store.put_artifact_manifest(manifest)
    store.put_collaboration_ticket(ticket)
    store.put_work_item(strategy_replay)
    store.put_mission(reopened)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="quality-strategy-loop-pressure-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert any("open-acceptance-pressure" in gate for gate in report.recovery_gate_refs)
    assert any("visual-product-iteration" in item_id for item_id in report.created_work_item_ids)
    assert any("final-delivery" in item_id for item_id in report.created_work_item_ids)
    assert control_plane.work_items[strategy_replay.work_item_id].status == "cancelled"
    assert control_plane.missions[mission.mission_id].status == "queued"
    assert "acceptance-rework" in (
        control_plane.missions[mission.mission_id].current_plan_version or ""
    )


def test_daemon_reopens_product_iteration_from_delivery_stage_player_gap(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "delivery-stage-player-gap.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-delivery-gap",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-delivery-gap",
        mission_id=mission.mission_id,
        version="wordforge-v58-acceptance-rework-old",
        objective=mission.objective,
        acceptance_criteria=["fresh real-player review must pass before final delivery"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-delivery-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-delivery-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Delivery-stage player gap must reopen product iteration."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-v58-real-player-gap",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="real-player-supervisor-review-v58",
        stage="delivery",
        task_type="product_development",
        rubric_version="external-final-game-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.82,
        speed=0.75,
        cost=0.8,
        risk=0.42,
        evidence_quality=0.88,
        collaboration_quality=0.82,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=[
            "fresh_real_player_review_pass",
            "final_game_commercial_ui_gap",
            "visual_object_entity_realism_gap",
            "scribblenauts_causal_interaction_depth_gap",
            "player_feel_touch_drag_affordance_gap",
        ],
        failure_category="delivery_failure",
        root_cause="Real player review says the game improved but is not final product parity.",
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="external_supervisor_real_player_gap_requires_iteration",
        created_by="external-supervisor-gpt5.5",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    changing = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "changing_plan", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = changing
    store.put_mission(changing)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="delivery-stage-player-gap-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert reopened.status == "queued"
    assert reopened.current_plan_version != plan.version
    assert "acceptance-rework" in (reopened.current_plan_version or "")
    assert any("visual-product-iteration" in item_id for item_id in report.created_work_item_ids)
    assert any(
        "commercial-game-polish-iteration" in item_id for item_id in report.created_work_item_ids
    )
    assert gate.gate_evaluation_id in report.recovery_gate_refs


def test_daemon_requests_human_playtest_for_player_review_only_gate(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "player-review-only-gate.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-player-review-only",
        owner="kun",
        objective="Ship a finished word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-player-review-only",
        mission_id=mission.mission_id,
        version="wordforge-v58-acceptance-rework",
        objective=mission.objective,
        acceptance_criteria=["fresh real-player review must pass before final delivery"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-player-review-only",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-player-review-only",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Missing player review must request human playtest, not product rework."],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-v58-player-review-only",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="external-supervisor-gate-v58",
        stage="delivery",
        task_type="product_development",
        rubric_version="external-final-game-feel-v1",
        metric_pack_version="final-player-feel-v1",
        north_star_verdict="fail",
        result_quality=0.93,
        speed=0.75,
        cost=0.8,
        risk=0.42,
        evidence_quality=0.88,
        collaboration_quality=0.82,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=["fresh_real_player_review_pass"],
        failure_category="delivery_failure",
        root_cause="Automated gates need a fresh real player review before final delivery.",
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="external_supervisor_product_gate_failed",
        created_by="external-supervisor-gpt5.5",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    changing = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "changing_plan", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = changing
    store.put_mission(changing)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="player-review-only-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert reopened.status == "waiting_human"
    assert reopened.current_plan_version == plan.version
    assert report.created_work_item_ids == []
    assert report.recovery_gate_refs == []
    assert len(report.created_collaboration_ticket_ids) == 1
    ticket = control_plane.collaboration_tickets[report.created_collaboration_ticket_ids[0]]
    assert ticket.type == "review"
    assert ticket.context_ref == gate.gate_evaluation_id
    assert "target-player" in ticket.role_needed


def test_daemon_treats_human_or_target_acceptance_missing_as_review_only() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-wordforge-human-or-target-review-only",
        owner="kun",
        objective="Wait for human or target-user validation without opening product rework.",
        task_type="product_development",
        status="changing_plan",
        current_plan_version="wordforge-v58-acceptance-rework",
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-human-or-target-review-only",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "wordforge-v58-acceptance-rework",
        subject_ref="work-mission-director-final",
        stage="acceptance",
        task_type="product_development",
        rubric_version="mission-director-final-v1",
        metric_pack_version="human-target-acceptance-v1",
        north_star_verdict="partial",
        result_quality=0.94,
        speed=0.75,
        cost=0.8,
        risk=0.25,
        evidence_quality=0.92,
        collaboration_quality=0.85,
        hard_gate_failures=["human_or_target_user_acceptance_missing"],
        failure_category="delivery_failure",
        root_cause="Automated gates passed; human or target-user acceptance is still pending.",
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="mission_director_requires_human_acceptance",
        created_by=MISSION_DIRECTOR_OWNER,
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate

    assert _latest_acceptance_rework_gate(control_plane, mission) is None
    assert _latest_human_player_review_only_gate(control_plane, mission) == gate


def test_daemon_retires_stale_product_gates_while_new_rework_branch_is_active(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "stale-product-gates.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-stale-gates",
        owner="kun",
        objective="Keep improving the game until final player feel passes.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-stale-gates",
        mission_id=mission.mission_id,
        version="wordforge-v58-acceptance-rework",
        objective=mission.objective,
        acceptance_criteria=["fresh player-feel evidence must be current"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-stale-gates",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-stale-gates",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["stale product gates must not review old evidence"],
    )
    active_iteration = WorkItem(
        work_item_id="work-new-05-commercial-game-polish-iteration",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun-game-production-runner",
        phase="commercial-game-polish-iteration",
        status="failed",
        priority=94,
        expected_output="Polish the commercial game feel before review.",
    )
    protected_retest = WorkItem(
        work_item_id="work-new-06-fun-and-browser-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun-game-production-runner",
        phase="internal-test",
        status="queued",
        priority=94,
        dependencies=[active_iteration.work_item_id],
        expected_output="Run fresh tests after the new rework.",
    )
    protected_gate = WorkItem(
        work_item_id="work-new-07-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=94,
        dependencies=[protected_retest.work_item_id],
        expected_output="Review product feel against the final game standard.",
    )
    old_retest = WorkItem(
        work_item_id="work-old-06-fun-and-browser-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun-game-production-runner",
        phase="internal-test",
        status="done",
        priority=92,
        expected_output="Old branch test evidence.",
    )
    stale_gate = WorkItem(
        work_item_id="work-old-07-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=92,
        dependencies=["work-old-06-fun-and-browser-retest"],
        expected_output="Review product feel against old evidence.",
    )
    stale_delivery = WorkItem(
        work_item_id="work-old-09-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun-game-production-runner",
        phase="final-delivery",
        status="queued",
        priority=92,
        dependencies=[stale_gate.work_item_id],
        expected_output="Deliver again only if the old gate passes.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[
            active_iteration,
            protected_retest,
            protected_gate,
            old_retest,
            stale_gate,
            stale_delivery,
        ],
    )
    repairing = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "repairing", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = repairing
    store.put_mission(repairing)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="stale-gate-daemon-test")
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.work_items[stale_gate.work_item_id].status == "cancelled"
    assert control_plane.work_items[stale_delivery.work_item_id].status == "cancelled"
    assert control_plane.work_items[active_iteration.work_item_id].status == "failed"
    assert control_plane.work_items[protected_retest.work_item_id].status == "queued"
    assert control_plane.work_items[protected_gate.work_item_id].status == "queued"
    assert stale_gate.work_item_id in report.retired_work_item_ids
    assert stale_delivery.work_item_id in report.retired_work_item_ids


def test_daemon_cancels_downstream_delivery_after_failed_player_gate(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "failed-player-gate-downstream.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-failed-player-gate",
        owner="kun",
        objective="Do not deliver when final player feel failed.",
        task_type="product_development",
        status="contracted",
        current_plan_version="wordforge-v58-acceptance-rework",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-failed-player-gate",
        mission_id=mission.mission_id,
        version=mission.current_plan_version,
        objective=mission.objective,
        acceptance_criteria=["final player experience must pass before delivery"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-failed-player-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-failed-player-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="game delivery",
        summary="Game delivery context.",
        constraints=["failed player gates block downstream delivery"],
        acceptance_criteria=plan.acceptance_criteria,
    )
    failed_gate = WorkItem(
        work_item_id="work-current-03-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="failed",
        priority=94,
        expected_output="Review final player experience against Scribblenauts-like feel.",
    )
    residual_audit = WorkItem(
        work_item_id="work-current-04-benchmark-residual-audit",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="kun-game-production-runner",
        phase="benchmark-residual-audit",
        status="queued",
        priority=94,
        dependencies=[failed_gate.work_item_id],
        expected_output="Audit benchmark residual only after final player gate passes.",
    )
    final_delivery = WorkItem(
        work_item_id="work-current-05-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun-game-production-runner",
        phase="final-delivery",
        status="queued",
        priority=94,
        dependencies=[residual_audit.work_item_id],
        expected_output="Deliver only after residual audit passes.",
    )
    qi_followup = WorkItem(
        work_item_id="work-qi-observation-msn-wordforge-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        status="queued",
        priority=90,
        dependencies=[failed_gate.work_item_id],
        expected_output="Create a different strategy for the failed product gate.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[failed_gate, residual_audit, final_delivery, qi_followup],
    )
    repairing = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "repairing", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = repairing
    store.put_mission(repairing)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="failed-gate-cleanup-test")
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.work_items[residual_audit.work_item_id].status == "cancelled"
    assert control_plane.work_items[final_delivery.work_item_id].status == "cancelled"
    assert control_plane.work_items[qi_followup.work_item_id].status == "queued"
    assert residual_audit.work_item_id in report.retired_work_item_ids
    assert final_delivery.work_item_id in report.retired_work_item_ids
    assert qi_followup.work_item_id not in report.retired_work_item_ids


def test_daemon_cancels_downstream_delivery_when_player_gate_fails_in_same_tick(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "same-tick-failed-player-gate.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-same-tick-failed-player-gate",
        owner="kun",
        objective="Clean delivery queue immediately when player feel fails.",
        task_type="product_development",
        status="contracted",
        current_plan_version="wordforge-v58-acceptance-rework",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-same-tick-failed-player-gate",
        mission_id=mission.mission_id,
        version=mission.current_plan_version,
        objective=mission.objective,
        acceptance_criteria=["final player experience must pass before delivery"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-same-tick-failed-player-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run_player_experience_review"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-same-tick-failed-player-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="game delivery",
        summary="Game delivery context.",
        constraints=["same tick product gate failures block delivery"],
        acceptance_criteria=plan.acceptance_criteria,
    )
    gate = WorkItem(
        work_item_id="work-current-03-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=94,
        expected_output="Review final player experience against Scribblenauts-like feel.",
    )
    residual_audit = WorkItem(
        work_item_id="work-current-04-benchmark-residual-audit",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="kun-game-production-runner",
        phase="benchmark-residual-audit",
        status="queued",
        priority=94,
        dependencies=[gate.work_item_id],
        expected_output="Audit benchmark residual only after final player gate passes.",
    )
    final_delivery = WorkItem(
        work_item_id="work-current-05-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun-game-production-runner",
        phase="final-delivery",
        status="queued",
        priority=94,
        dependencies=[residual_audit.work_item_id],
        expected_output="Deliver only after residual audit passes.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[gate, residual_audit, final_delivery],
    )
    running = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "running", "current_plan_version": plan.version}
    )
    control_plane.missions[mission.mission_id] = running
    store.put_mission(running)

    class FailingPlayerGateRunner:
        runner_type: Literal["agent"] = "agent"
        runner_identity = "failing-player-gate-runner"

        def run(self, work_item: WorkItem) -> WorkItemResult:
            return WorkItemResult(
                status="failed",
                summary="Player feel is not final.",
                failure_category="delivery_failure",
            )

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"external-supervisor-gpt5.5": FailingPlayerGateRunner()},
        daemon_id="same-tick-failed-gate-cleanup-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
        write_progress=False,
    )

    assert report.ran_work_item_ids == [gate.work_item_id]
    assert control_plane.work_items[gate.work_item_id].status == "failed"
    assert control_plane.work_items[residual_audit.work_item_id].status == "cancelled"
    assert control_plane.work_items[final_delivery.work_item_id].status == "cancelled"
    assert residual_audit.work_item_id in report.retired_work_item_ids
    assert final_delivery.work_item_id in report.retired_work_item_ids


def test_daemon_keeps_one_current_game_rework_branch(tmp_path: Path) -> None:
    store = FileControlPlaneStore(tmp_path / "single-game-rework-branch.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-single-rework-branch",
        owner="kun",
        objective="Keep only one active product rework branch.",
        task_type="product_development",
        status="contracted",
        current_plan_version="wordforge-v58-acceptance-rework",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-single-rework-branch",
        mission_id=mission.mission_id,
        version=mission.current_plan_version,
        objective=mission.objective,
        acceptance_criteria=["one current rework branch owns product execution"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-single-rework-branch",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-single-rework-branch",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="game delivery",
        summary="Game delivery context.",
        constraints=["preserve one current game rework branch"],
        acceptance_criteria=plan.acceptance_criteria,
    )
    stale_branch_iteration = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "aaaa1111aaaa-01-visual-product-iteration"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun-game-production-runner",
        phase="visual-product-iteration",
        status="queued",
        priority=94,
    )
    stale_branch_gate = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "aaaa1111aaaa-06-supervisor-gate"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=94,
        dependencies=[stale_branch_iteration.work_item_id],
    )
    active_branch_iteration = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "bbbb2222bbbb-01-visual-product-iteration"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun-game-production-runner",
        phase="visual-product-iteration",
        status="running",
        priority=94,
    )
    active_branch_gate = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "bbbb2222bbbb-06-supervisor-gate"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=94,
        dependencies=[active_branch_iteration.work_item_id],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[
            stale_branch_iteration,
            stale_branch_gate,
            active_branch_iteration,
            active_branch_gate,
        ],
    )
    mission.status = "repairing"
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="single-branch-test")
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.work_items[stale_branch_iteration.work_item_id].status == "cancelled"
    assert control_plane.work_items[stale_branch_gate.work_item_id].status == "cancelled"
    assert control_plane.work_items[active_branch_iteration.work_item_id].status == "running"
    assert control_plane.work_items[active_branch_gate.work_item_id].status == "queued"
    assert stale_branch_iteration.work_item_id in report.retired_work_item_ids
    assert stale_branch_gate.work_item_id in report.retired_work_item_ids


def test_daemon_keeps_current_game_rework_branch_during_test_phase(tmp_path: Path) -> None:
    store = FileControlPlaneStore(tmp_path / "single-game-rework-test-branch.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-single-rework-test-branch",
        owner="kun",
        objective="Keep the branch that has advanced past implementation.",
        task_type="product_development",
        status="contracted",
        current_plan_version="wordforge-v58-acceptance-rework",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-single-rework-test-branch",
        mission_id=mission.mission_id,
        version=mission.current_plan_version,
        objective=mission.objective,
        acceptance_criteria=["one current test branch owns product gates"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-single-rework-test-branch",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(tmp_path / "wordforge"),
            "production_mode": "scribble_adventure_functional_parity_v1",
            "final_player_experience_required": True,
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-single-rework-test-branch",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="daemon",
        scope="game delivery",
        summary="Game delivery context.",
        constraints=["preserve one current game rework branch"],
        acceptance_criteria=plan.acceptance_criteria,
    )
    stale_branch_gate = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "aaaa1111aaaa-06-supervisor-gate"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="failed",
        priority=94,
    )
    stale_branch_delivery = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "aaaa1111aaaa-08-final-delivery"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="merge",
        owner="kun-game-production-runner",
        phase="final-delivery",
        status="queued",
        priority=94,
    )
    active_branch_test = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "bbbb2222bbbb-05-internal-test"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun-game-production-runner",
        phase="internal-test",
        status="running",
        priority=94,
    )
    active_branch_gate = WorkItem(
        work_item_id=(
            "work-game-rework-work-kun-plan-change-work-qi-observation-msn-wordforge-"
            "bbbb2222bbbb-06-supervisor-gate"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="queued",
        priority=94,
        dependencies=[active_branch_test.work_item_id],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[
            stale_branch_gate,
            stale_branch_delivery,
            active_branch_test,
            active_branch_gate,
        ],
    )
    mission = control_plane.missions[mission.mission_id].model_copy(update={"status": "running"})
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="single-test-branch-test")
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.work_items[stale_branch_gate.work_item_id].status == "cancelled"
    assert control_plane.work_items[stale_branch_delivery.work_item_id].status == "cancelled"
    assert control_plane.work_items[active_branch_test.work_item_id].status == "running"
    assert control_plane.work_items[active_branch_gate.work_item_id].status == "queued"
    assert stale_branch_gate.work_item_id in report.retired_work_item_ids
    assert stale_branch_delivery.work_item_id in report.retired_work_item_ids


def test_daemon_clean_retest_does_not_requeue_failed_product_gate(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "clean-retest-product-gate.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-clean-retest-product-gate",
        owner="kun",
        objective="Keep product gates tied to fresh product evidence.",
        task_type="product_development",
        status="repairing",
        current_plan_version="wordforge-v58",
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    subject = WorkItem(
        work_item_id="work-final-player-experience-gate",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="review",
        owner="external-supervisor-gpt5.5",
        phase="supervisor-gate",
        status="failed",
        priority=92,
        expected_output="Review product feel against the final game standard.",
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-final-player-gate-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="repair",
        owner="nuo",
        status="partial",
        priority=90,
        recovery_refs=[subject.work_item_id],
        expected_output="Diagnose wrapper issue for the failed product gate.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-final-player-gate",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        type="retest",
        owner="nuo",
        status="done",
        priority=90,
        recovery_refs=[partial_repair.work_item_id],
        expected_output="Clean retest proves the workspace can write.",
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-final-player-product-gap",
        mission_id=mission.mission_id,
        task_plan_version="wordforge-v58",
        subject_ref=subject.work_item_id,
        stage="delivery",
        task_type="product_development",
        rubric_version="final-player-feel-v1",
        metric_pack_version="kun-v6-north-star-v1",
        north_star_verdict="fail",
        result_quality=0.82,
        speed=0.8,
        cost=0.8,
        risk=0.4,
        evidence_quality=0.9,
        collaboration_quality=0.8,
        thresholds={"result_quality": 0.95},
        hard_gate_failures=["fresh_real_player_review_pass", "commercial_ui_gap"],
        failure_category="delivery_failure",
        root_cause="The product needs another iteration before player-feel review can pass.",
        responsibility_scope="kun_auto",
        confidence=0.9,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="player experience still below final product bar",
        created_by="external-supervisor-gpt5.5",
    )
    for item in (subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="clean-retest-product-gate-daemon-test",
    )
    report = DaemonTickReport(
        daemon_id=daemon.daemon_id,
        observed_at=NOW,
        mission_ids=[mission.mission_id],
    )
    daemon._requeue_failed_subjects_for_completed_clean_retest(
        partial_repair=partial_repair,
        report=report,
    )

    assert control_plane.work_items[subject.work_item_id].status == "failed"
    assert subject.work_item_id not in report.recovered_work_item_ids


def test_done_mechanical_loop_followup_accumulates_evidence_without_requeue(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "mechanical-loop-followup.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-wordforge-loop",
        owner="kun",
        objective="Keep improving WordForge until accepted.",
        task_type="product_development",
        status="running",
        current_plan_version="wordforge-v56-acceptance-rework-abc12345",
    )
    followup = WorkItem(
        work_item_id=(
            "work-qi-observation-msn-wordforge-loop-"
            "mechanical_acceptance_rework_loop-strategy-replay"
        ),
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version,
        type="governance",
        owner="qi",
        priority=98,
        status="done",
        idempotency_key=(
            "runtime-observation:qi:work-qi-observation-msn-wordforge-loop-"
            "mechanical_acceptance_rework_loop-strategy-replay"
        ),
        expected_output="Run strategy replay for mechanical_acceptance_rework_loop.",
        recovery_refs=["artifact-old-observation"],
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.work_items[followup.work_item_id] = followup
    store.put_mission(mission)
    store.put_work_item(followup)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="mechanical-loop-test")
    report = DaemonTickReport(
        daemon_id=daemon.daemon_id,
        observed_at=NOW,
        mission_ids=[mission.mission_id],
    )

    daemon._queue_observation_followup(
        mission_id=mission.mission_id,
        owner="qi",
        item_type="governance",
        work_item_id=followup.work_item_id,
        expected_output="Run strategy replay for mechanical_acceptance_rework_loop.",
        evidence_refs=["artifact-old-observation", "artifact-new-observation"],
        required=True,
        priority=98,
        report=report,
    )

    updated = control_plane.work_items[followup.work_item_id]
    assert updated.status == "done"
    assert updated.recovery_refs == ["artifact-old-observation", "artifact-new-observation"]
    assert followup.work_item_id not in report.observation_followup_ids


def test_daemon_clean_retest_does_not_requeue_rainflow_evidence_failure(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "clean-retest-rainflow-evidence.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-adflow-extreme-v1",
        owner="kun",
        objective="Make RainFlow ad editing excellent.",
        task_type="product_development",
        status="repairing",
        current_plan_version="rainflow-adflow-v1-acceptance-rework-0524bb04",
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    subject = WorkItem(
        work_item_id=(
            "work-msn-rainflow-adflow-extreme-v1-rainflow-adflow-v1-acceptance-"
            "rework-0524bb04-2b31b7e9-02-material-screening-and-selection"
        ),
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "rainflow",
        type="execution",
        owner="kun",
        status="failed",
        priority=90,
        expected_output="Improve material screening and selection with local evidence.",
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-rainflow-material-screening-rerun",
        mission_id=mission.mission_id,
        task_plan_version=subject.task_plan_version,
        type="repair",
        owner="control-plane",
        status="partial",
        priority=90,
        idempotency_key=f"nuo-recovery:{subject.work_item_id}:rerun:timeout,network_blocked",
        recovery_refs=[subject.work_item_id],
        expected_output="Rerun the same subject after the environment blocker is cleared.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-rainflow-material-screening",
        mission_id=mission.mission_id,
        task_plan_version=subject.task_plan_version,
        type="retest",
        owner="nuo",
        status="done",
        priority=90,
        recovery_refs=[partial_repair.work_item_id],
        expected_output="Clean retest proves transport is available.",
    )
    failed_run = RunRecord(
        run_id="run-rainflow-material-screening-evidence-missing",
        work_item_id=subject.work_item_id,
        runner_type="agent",
        runner_identity="kun-runtime-task-runner",
        started_at=NOW - timedelta(minutes=12),
        ended_at=NOW,
        exit_status="failed",
        failure_category="delivery_failure",
    )
    for item in (subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    control_plane.runs[failed_run.run_id] = failed_run
    store.put_run_record(failed_run)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="clean-retest-rainflow-evidence-daemon-test",
    )
    report = DaemonTickReport(
        daemon_id=daemon.daemon_id,
        observed_at=NOW,
        mission_ids=[mission.mission_id],
    )
    daemon._requeue_failed_subjects_for_completed_clean_retest(
        partial_repair=partial_repair,
        report=report,
    )

    assert control_plane.work_items[subject.work_item_id].status == "failed"
    assert subject.work_item_id not in report.recovered_work_item_ids


def test_daemon_keeps_product_pressure_even_when_final_product_evidence_passes(
    tmp_path: Path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "open-acceptance-passed-evidence.json")
    control_plane = InMemoryControlPlane(store=store)
    project_path = tmp_path / "wordforge"
    docs_path = project_path / "docs"
    docs_path.mkdir(parents=True)
    (docs_path / "final-player-experience-gate.json").write_text(
        json.dumps(
            {
                "score": 0.992,
                "threshold": 0.98,
                "pass": True,
                "failures": [],
                "dimension_floor": 0.97,
                "dimensions": {
                    "immersive_game_stage": {"score": 0.99},
                    "tablet_direct_manipulation": {"score": 0.991},
                    "image_object_interaction": {"score": 0.992},
                },
            }
        ),
        encoding="utf-8",
    )
    (docs_path / "benchmark-residual-audit.json").write_text(
        json.dumps({"overall_residual": 0.0023, "threshold": 0.003, "pass": True}),
        encoding="utf-8",
    )
    mission = Mission(
        mission_id="msn-wordforge-passed-evidence",
        owner="kun",
        objective="Ship a commercial-feeling word-to-world game.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-wordforge-passed-evidence",
        mission_id=mission.mission_id,
        version="wordforge-v28",
        objective=mission.objective,
        acceptance_criteria=["human acceptance is required before stopping"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-wordforge-passed-evidence",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["write_game_project", "run_build", "run_browser_playtest"],
        delivery_contract={
            "project_path": str(project_path),
            "production_mode": "scribble_adventure_functional_parity_v1",
        },
    )
    context = WorkingContext(
        working_context_id="ctx-wordforge-passed-evidence",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="kun-game-production-runner",
        scope="game delivery",
        summary="Game delivery context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=[
            "Do not idle in awaiting_acceptance without human acceptance.",
            "Passed automated evidence does not replace final human acceptance.",
        ],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-wordforge-passed-evidence-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-wordforge-passed-evidence-delivery"],
        primary_artifact_ref="artifact-wordforge-passed-evidence-delivery",
        evidence_refs=["artifact-wordforge-passed-evidence-delivery"],
        rollback_refs=["snapshot-wordforge-passed-evidence-delivery"],
        created_by="kun-game-production-runner",
        content_hash="passed-evidence-delivery",
        supports_delivery=True,
    )
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    store.put_artifact_manifest(manifest)
    submitted = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "delivering",
            "current_plan_version": plan.version,
            "artifact_manifest_refs": [manifest.manifest_id],
        }
    )
    control_plane.missions[mission.mission_id] = submitted
    store.put_mission(submitted)

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="open-acceptance-passed-evidence-daemon-test",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    reopened = control_plane.missions[mission.mission_id]
    assert reopened.status == "queued"
    assert reopened.current_plan_version is not None
    assert "acceptance-rework" in reopened.current_plan_version
    assert len(report.created_collaboration_ticket_ids) == 1
    assert any("open-acceptance-pressure" in gate for gate in report.recovery_gate_refs)
    assert any(
        "commercial-game-polish-iteration" in item_id for item_id in report.created_work_item_ids
    )
    created_items = [
        control_plane.work_items[item_id]
        for item_id in report.created_work_item_ids
        if item_id in control_plane.work_items
    ]
    assert any(item.phase == "visual-product-iteration" for item in created_items)
    assert any(item.phase == "internal-test" for item in created_items)
    assert any(item.phase == "supervisor-gate" for item in created_items)


def _prepare_productization_closures(control_plane, mission_id: str) -> None:
    signals = distill_external_behavior_signals(
        {
            "external_repos/openclaw/README.md": (
                "Gateway sessions, tools, and multi-agent isolated workspaces."
            ),
            "external_repos/hermes-agent/RELEASE_v0.8.0.md": (
                "Background completion notifications, approval buttons, structured logs, "
                "and persisted large tool results."
            ),
        }
    )
    materialize_external_behavior_distillation(control_plane, mission_id, signals)
    profile = CapabilityProfile(
        capability_id="cap-daemon-productization-runtime-default",
        capability_name="KUN-native daemon productization runtime default",
        evidence_refs=["artifact-productization-distillation"],
        known_limits=["Keep runtime default rollbackable."],
        promotion_stage="production",
        holdout_refs=["artifact-productization-holdout"],
        regression_refs=["artifact-productization-regression"],
        rollback_plan=["disable productization runtime default"],
        runtime_enabled=True,
    )
    control_plane.capability_profiles[profile.capability_id] = profile
    if control_plane.store is not None:
        control_plane.store.put_capability_profile(profile)
    close_productization_collaboration_loop(
        control_plane,
        mission_id,
        context_ref=f"ctx-{mission_id}",
    )


def test_daemon_tick_runs_ready_work_and_persists_progress(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-daemon"]
    assert report.no_runner_work_item_ids == []
    assert report.progress_artifact_refs == ["artifact-daemon-progress-msn-daemon-20260519T090000Z"]
    assert report.observation_artifact_refs == [
        "artifact-runtime-observation-msn-daemon-20260519T090000Z"
    ]
    assert recovered.work_items["work-daemon"].status == "done"
    assert len(recovered.runs) == 1
    assert next(iter(recovered.runs.values())).exit_status == "succeeded"
    assert report.progress_artifact_refs[0] in recovered.artifacts
    observation = recovered.artifacts[report.observation_artifact_refs[0]]
    assert "runtime_observation" in observation.supports
    observation_report = report.runtime_observations["msn-daemon"]
    assert observation_report.max_severity == "high"
    assert [item.code for item in observation_report.items] == ["delivery_manifest_missing"]


def test_daemon_tick_fairly_runs_ready_work_across_multiple_missions(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    second_mission = _add_ready_mission(
        control_plane,
        "msn-daemon-b",
        work_item_id="work-daemon-b",
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id, second_mission.mission_id],
        now=NOW,
        max_work_items=2,
    )

    assert report.ran_work_item_ids == ["work-daemon", "work-daemon-b"]
    assert control_plane.work_items["work-daemon"].status == "done"
    assert control_plane.work_items["work-daemon-b"].status == "done"


def test_daemon_worker_pool_runs_independent_items_in_parallel(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    first_item = control_plane.work_items["work-daemon"].model_copy(
        update={"workspace_ref": str(tmp_path / "workspace-a")}
    )
    control_plane.work_items[first_item.work_item_id] = first_item
    store.put_work_item(first_item)
    second_item = WorkItem(
        work_item_id="work-daemon-independent",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=79,
        expected_output="independent same-mission work",
        workspace_ref=str(tmp_path / "workspace-b"),
    )
    control_plane.work_items[second_item.work_item_id] = second_item
    store.put_work_item(second_item)
    runner = ConcurrentProbeRunner()
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-parallel-test",
        worker_pool=WorkerPoolConfig(worker_count=2),
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=2,
    )

    assert report.ran_work_item_ids == ["work-daemon", "work-daemon-independent"]
    assert runner.max_running == 2
    assert control_plane.work_items["work-daemon"].status == "done"
    assert control_plane.work_items["work-daemon-independent"].status == "done"


def test_daemon_serializes_same_mission_execution_without_workspace_boundary(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    second_item = WorkItem(
        work_item_id="work-daemon-unbounded",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=79,
        expected_output="same-mission work without workspace boundary",
    )
    control_plane.work_items[second_item.work_item_id] = second_item
    store.put_work_item(second_item)
    runner = ConcurrentProbeRunner()
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-safe-default-lock-test",
        worker_pool=WorkerPoolConfig(worker_count=2),
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=2,
    )

    assert report.ran_work_item_ids == ["work-daemon", "work-daemon-unbounded"]
    assert report.resource_lock_skipped_work_item_ids == ["work-daemon-unbounded"]
    assert runner.max_running == 1


def test_daemon_tick_respects_resource_locks_within_one_wakeup(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    locked_a = control_plane.work_items["work-daemon"].model_copy(
        update={"resource_locks": ["workspace:/tmp/shared-kun-workspace"], "priority": 90}
    )
    second_mission = _add_ready_mission(
        control_plane,
        "msn-daemon-b",
        work_item_id="work-daemon-conflicting",
        resource_locks=["workspace:/tmp/shared-kun-workspace"],
    )
    control_plane.work_items[locked_a.work_item_id] = locked_a
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
        worker_pool=WorkerPoolConfig(worker_count=2),
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id, second_mission.mission_id],
        now=NOW,
        max_work_items=2,
    )

    assert report.ran_work_item_ids == ["work-daemon", "work-daemon-conflicting"]
    assert report.resource_lock_skipped_work_item_ids == ["work-daemon-conflicting"]
    assert control_plane.work_items["work-daemon-conflicting"].status == "done"


def test_stale_recovery_releases_resource_lock_holder(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    lock_store = FileResourceLockStore(tmp_path / "resource-locks.json")
    holder_id = "lease:daemon-test:worker-1:work-daemon:stale"
    locked = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "running",
            "lease": holder_id,
            "heartbeat": NOW - timedelta(minutes=30),
            "timeout": NOW - timedelta(seconds=1),
            "resource_locks": [
                "mission-state:msn-daemon",
                "workspace:/tmp/shared-kun-workspace",
            ],
        }
    )
    control_plane.work_items[locked.work_item_id] = locked
    store.put_work_item(locked)
    lock_store.acquire_many(
        resources=locked.resource_locks,
        holder_id=holder_id,
        daemon_id="daemon-test",
        worker_id="worker-1",
        work_item=locked,
        now=NOW - timedelta(minutes=30),
        ttl=timedelta(hours=1),
    )
    assert lock_store.list_active(now=NOW)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
        resource_lock_store=lock_store,
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert locked.work_item_id in report.recovered_work_item_ids
    assert lock_store.list_active(now=NOW) == []


def test_daemon_releases_orphaned_resource_lock_before_scheduling(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    lock_store = FileResourceLockStore(tmp_path / "resource-locks.json")
    holder_id = "lease:daemon-test:worker-1:dead-work"
    dead = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "failed",
            "lease": None,
            "timeout": None,
            "resource_locks": ["workspace:/tmp/shared-kun-workspace"],
        }
    )
    next_work = WorkItem(
        work_item_id="work-after-orphan-lock",
        mission_id=mission.mission_id,
        owner="kun",
        type="execution",
        expected_output="Run after dead holder lock is released.",
        status="queued",
        resource_locks=["workspace:/tmp/shared-kun-workspace"],
        priority=95,
        task_plan_version="v1",
    )
    control_plane.work_items[dead.work_item_id] = dead
    control_plane.work_items[next_work.work_item_id] = next_work
    store.put_work_item(dead)
    store.put_work_item(next_work)
    lock_store.acquire_many(
        resources=dead.resource_locks,
        holder_id=holder_id,
        daemon_id="daemon-test",
        worker_id="worker-1",
        work_item=dead.model_copy(update={"lease": holder_id, "status": "running"}),
        now=NOW - timedelta(minutes=5),
        ttl=timedelta(hours=1),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
        resource_lock_store=lock_store,
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.resource_lock_skipped_work_item_ids == []
    assert report.ran_work_item_ids == [next_work.work_item_id]
    assert lock_store.list_active(now=NOW) == []


def test_daemon_releases_claimed_work_item_lease_when_start_declines(
    tmp_path,
    monkeypatch,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    lock_store = FileResourceLockStore(tmp_path / "resource-locks.json")

    def fake_start_work_item_run(**_kwargs):
        return None

    monkeypatch.setattr(control_plane, "start_work_item_run", fake_start_work_item_run)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-start-declines-test",
        resource_lock_store=lock_store,
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    after = InMemoryControlPlane(store=store)
    work_item = after.work_items["work-daemon"]

    assert report.ran_work_item_ids == []
    assert work_item.status == "queued"
    assert work_item.lease is None
    assert work_item.heartbeat is None
    assert work_item.timeout is None
    assert lock_store.list_active(now=NOW) == []


def test_daemon_records_worker_pool_distributed_lock_and_sandbox_state(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = control_plane.contracts["contract-daemon"].model_copy(
        update={"delivery_contract": {"workspace_path": str(workspace)}}
    )
    control_plane.contracts[contract.contract_id] = contract
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-state.json")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": ContainerCapableRunner()},
        daemon_id="daemon-worker-pool-test",
        worker_pool=WorkerPoolConfig(
            pool_id="pool-test",
            machine_id="machine-a",
            worker_count=2,
        ),
        resource_lock_store=FileResourceLockStore(tmp_path / "resource-locks.json"),
        sandbox_mode="container_required",
        container_runtime="docker",
    )

    report = daemon.run_managed_loop(
        mission_ids=[mission.mission_id],
        config=DaemonServiceConfig(
            max_ticks=1,
            max_work_items_per_tick=1,
            worker_pool_size=2,
            sandbox_mode="container_required",
            container_runtime="docker",
        ),
        state_store=state_store,
        now_factory=lambda: NOW,
        sleeper=lambda _seconds: None,
    ).tick_reports[0]
    state = state_store.load()

    assert report.ran_work_item_ids == ["work-daemon"]
    assert len(report.worker_slots) == 2
    assert report.worker_slots[0].worker_id == "worker-1"
    assert str(workspace) in report.worker_slots[0].resource_locks[0]
    assert report.sandbox_specs[0].mode == "container_required"
    assert report.sandbox_specs[0].container_runtime == "docker"
    assert state is not None
    assert state.worker_pool_size == 2
    assert state.resource_lock_backend == "file"
    assert state.last_tick_worker_slots[0].worker_id == "worker-1"
    assert state.last_tick_sandbox_specs[0].mode == "container_required"


def test_managed_loop_switches_to_configured_sqlite_resource_lock_backend(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-state.json")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-sqlite-lock-backend-test",
    )

    daemon.run_managed_loop(
        mission_ids=[mission.mission_id],
        config=DaemonServiceConfig(
            max_ticks=1,
            max_work_items_per_tick=1,
            resource_lock_backend="sqlite",
        ),
        state_store=state_store,
        now_factory=lambda: NOW,
        sleeper=lambda _seconds: None,
    )
    state = state_store.load()

    assert isinstance(daemon.resource_lock_store, SQLiteResourceLockStore)
    assert state is not None
    assert state.resource_lock_backend == "sqlite"


def test_daemon_blocks_container_required_when_runner_lacks_container_sandbox(
    tmp_path,
) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-container-required-test",
        sandbox_mode="container_required",
        container_runtime="docker",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    work_item = control_plane.work_items["work-daemon"]
    run = control_plane.runs[report.run_refs[0]]
    artifact = next(
        artifact
        for artifact in control_plane.artifacts.values()
        if "container_sandbox_required" in artifact.supports
    )

    assert report.ran_work_item_ids == ["work-daemon"]
    assert report.sandbox_specs[0].mode == "container_required"
    assert work_item.status == "failed"
    assert run.failure_category == "environment_failure"
    assert "sandbox_blocked" in artifact.supports


def test_daemon_activation_attaches_workspace_resource_lock(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    mission = control_plane.missions[mission.mission_id]
    contract = control_plane.contracts[mission.execution_contract_ref or ""].model_copy(
        update={"delivery_contract": {"workspace_path": str(tmp_path / "workspace")}}
    )
    control_plane.contracts[contract.contract_id] = contract
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == ["work-daemon"]
    assert (
        f"workspace:{tmp_path / 'workspace'}"
        in control_plane.work_items["work-daemon"].resource_locks
    )


def test_daemon_keeps_pure_qi_governance_off_workspace_lock_lane(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = control_plane.missions[mission.mission_id]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = control_plane.contracts[mission.execution_contract_ref or ""].model_copy(
        update={"delivery_contract": {"workspace_path": str(workspace)}}
    )
    control_plane.contracts[contract.contract_id] = contract
    store.put_execution_contract(contract)
    governance = WorkItem(
        work_item_id="work-qi-runtime-learning-work-daemon",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        priority=100,
        workspace_ref=f"workspace://{workspace}",
        resource_locks=[f"workspace:{workspace}"],
        expected_output="Governance-only runtime learning; do not touch project files.",
    )
    control_plane.work_items[governance.work_item_id] = governance
    store.put_work_item(governance)
    runner = ConcurrentProbeRunner(sleep_sec=0.05)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner, "qi": runner},
        daemon_id="daemon-governance-lane-test",
        worker_pool=WorkerPoolConfig(worker_count=2),
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=2,
    )

    assert set(report.ran_work_item_ids) == {"work-daemon", governance.work_item_id}
    assert report.resource_lock_skipped_work_item_ids == []
    assert runner.max_running == 2
    updated_governance = control_plane.work_items[governance.work_item_id]
    assert updated_governance.resource_locks == []
    governance_slot = next(
        slot for slot in report.worker_slots if slot.work_item_id == governance.work_item_id
    )
    assert governance_slot.resource_locks == [f"mission-state:{mission.mission_id}"]
    governance_sandbox = next(
        spec for spec in report.sandbox_specs if spec.work_item_id == governance.work_item_id
    )
    assert governance_sandbox.writable_refs == []


def test_daemon_retires_resolved_observation_followups_when_report_is_clean(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = control_plane.missions[mission.mission_id]
    resolved_followup = WorkItem(
        work_item_id="work-nuo-observation-msn-daemon-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        priority=90,
        idempotency_key=(
            "runtime-observation:nuo:work-nuo-observation-msn-daemon-quality_gate_not_passed-abc123"
        ),
        expected_output="Classify a now-resolved runtime observation.",
        recovery_refs=["artifact-old-observation", "gate-old-fail"],
    )
    control_plane.work_items[resolved_followup.work_item_id] = resolved_followup
    store.put_work_item(resolved_followup)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-cleanup-test")
    report = DaemonTickReport(
        daemon_id="daemon-cleanup-test",
        observed_at=NOW,
        mission_ids=[mission.mission_id],
    )

    daemon._retire_resolved_observation_followups(
        mission_id=mission.mission_id,
        observation=RuntimeObservationReport(mission_id=mission.mission_id, items=[]),
        observed_at=NOW,
        report=report,
    )

    assert report.retired_work_item_ids == [resolved_followup.work_item_id]
    assert control_plane.work_items[resolved_followup.work_item_id].status == "cancelled"
    recovered = InMemoryControlPlane(store=store)
    assert recovered.work_items[resolved_followup.work_item_id].status == "cancelled"


def test_daemon_retires_only_resolved_observation_followups_when_other_items_remain(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = control_plane.missions[mission.mission_id]
    stale = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-mechanical_acceptance_rework_loop-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=90,
        idempotency_key=(
            "runtime-observation:qi:"
            "work-qi-observation-msn-daemon-mechanical_acceptance_rework_loop-abc123"
        ),
        expected_output="Audit a now-resolved mechanical_acceptance_rework_loop observation.",
    )
    active = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-human_acceptance_ticket_missing-def456",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=90,
        idempotency_key=(
            "runtime-observation:qi:"
            "work-qi-observation-msn-daemon-human_acceptance_ticket_missing-def456"
        ),
        expected_output="Audit the active human_acceptance_ticket_missing observation.",
    )
    control_plane.work_items[stale.work_item_id] = stale
    control_plane.work_items[active.work_item_id] = active
    store.put_work_item(stale)
    store.put_work_item(active)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-cleanup-test")
    report = DaemonTickReport(
        daemon_id="daemon-cleanup-test",
        observed_at=NOW,
        mission_ids=[mission.mission_id],
    )

    daemon._retire_resolved_observation_followups(
        mission_id=mission.mission_id,
        observation=RuntimeObservationReport(
            mission_id=mission.mission_id,
            items=[
                {
                    "code": "human_acceptance_ticket_missing",
                    "severity": "high",
                    "title": "missing ticket",
                    "why_watch": "needs ticket",
                    "recommended_action": "open ticket",
                    "routes": ["human"],
                }
            ],
        ),
        observed_at=NOW,
        report=report,
    )

    assert report.retired_work_item_ids == [stale.work_item_id]
    assert control_plane.work_items[stale.work_item_id].status == "cancelled"
    assert control_plane.work_items[active.work_item_id].status == "queued"


def test_daemon_activation_normalizes_legacy_workspace_ref(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    mission = control_plane.missions[mission.mission_id]
    legacy_workspace = tmp_path / "workspace"
    contract = control_plane.contracts[mission.execution_contract_ref or ""].model_copy(
        update={"delivery_contract": {"workspace_path": str(legacy_workspace)}}
    )
    control_plane.contracts[contract.contract_id] = contract
    control_plane.work_items["work-daemon"] = control_plane.work_items["work-daemon"].model_copy(
        update={"workspace_ref": str(legacy_workspace)}
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
    )

    daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert (
        control_plane.work_items["work-daemon"].workspace_ref == f"workspace://{legacy_workspace}"
    )


def test_daemon_marks_missing_runner_for_external_supervision(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-no-runner-test")

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.no_runner_work_item_ids == ["work-daemon"]
    assert len(report.created_collaboration_ticket_ids) == 1
    assert report.observation_artifact_refs == [
        "artifact-runtime-observation-msn-daemon-20260519T090000Z"
    ]
    observation = recovered.artifacts[report.observation_artifact_refs[0]]
    assert "observation:runner_missing" in observation.supports
    assert "requires_external_supervision" in observation.supports
    assert "observation_route:qi" in observation.supports
    observation_report = report.runtime_observations["msn-daemon"]
    assert observation_report.requires_external_supervision is True
    assert observation_report.items[0].code == "runner_missing"
    assert recovered.collaboration_tickets[report.created_collaboration_ticket_ids[0]].type == (
        "operator_action"
    )
    assert "external_supervisor" in observation_report.items[0].routes


def test_daemon_creates_restorable_workspace_snapshot_and_rolls_back(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.txt"
    target.write_text("v1", encoding="utf-8")
    contract = control_plane.contracts["contract-daemon"]
    contract = contract.model_copy(update={"delivery_contract": {"project_path": str(workspace)}})
    control_plane.contracts[contract.contract_id] = contract
    store.put_execution_contract(contract)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    snapshot_ref = next(
        artifact_ref
        for artifact_ref in report.activation_artifact_refs
        if "checkpoint" in artifact_ref
    )
    snapshot = control_plane.artifacts[snapshot_ref]
    assert "restore_mode:file_copy" in snapshot.supports
    assert json.loads(Path(snapshot.path_or_uri).read_text(encoding="utf-8"))["restore_mode"] == (
        "file_copy"
    )

    target.write_text("broken", encoding="utf-8")
    (workspace / "extra.txt").write_text("extra", encoding="utf-8")
    rollback = WorkItem(
        work_item_id="work-rollback",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="rollback",
        owner="control-plane",
        priority=100,
        idempotency_key="rollback:test",
        expected_output="restore workspace snapshot",
        rollback_refs=[snapshot_ref],
    )
    control_plane.work_items[rollback.work_item_id] = rollback
    store.put_work_item(rollback)

    rollback_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
    )
    assert rollback_report.ran_work_item_ids == ["work-rollback"]
    assert target.read_text(encoding="utf-8") == "v1"
    assert not (workspace / "extra.txt").exists()
    restored = InMemoryControlPlane(store=store)
    assert restored.work_items["work-rollback"].status == "done"
    assert any("workspace_restore" in artifact.supports for artifact in restored.artifacts.values())


def test_workspace_snapshot_prunes_excluded_directories(tmp_path) -> None:
    control_plane, _store, _mission = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.txt").write_text("source", encoding="utf-8")
    (workspace / "node_modules" / "big-lib").mkdir(parents=True)
    (workspace / "node_modules" / "big-lib" / "ignored.txt").write_text(
        "ignored",
        encoding="utf-8",
    )
    (workspace / ".git" / "objects").mkdir(parents=True)
    (workspace / ".git" / "objects" / "ignored").write_text("ignored", encoding="utf-8")

    snapshot = create_workspace_snapshot(
        control_plane=control_plane,
        work_item=control_plane.work_items["work-daemon"],
        workspace_path=str(workspace),
        actor="daemon-test",
        observed_at=NOW,
    )

    assert snapshot is not None
    manifest = json.loads(Path(snapshot.manifest_path).read_text(encoding="utf-8"))
    assert [file_record["path"] for file_record in manifest["files"]] == ["src/app.txt"]
    assert manifest["omitted_file_count"] == 2
    assert manifest["complete_restore"] is False
    assert not (Path(snapshot.snapshot_dir) / "files" / "node_modules").exists()
    assert not (Path(snapshot.snapshot_dir) / "files" / ".git").exists()


def test_daemon_bridges_v6_gate_events_to_watchtower(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    rule = GuardRule(
        id="v6_gate_pass",
        kind="guard",
        trigger=RuleTrigger(
            event_type="control_plane.gate_evaluated",
            when="event['payload']['north_star_verdict'] == 'pass'",
        ),
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-test",
        rule_engine=RuleEngine([rule]),
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)

    assert "v6_gate_pass" in report.watchtower_fired_rule_ids
    assert report.watchtower_error_count == 0


def test_daemon_binds_production_capabilities_to_runner_and_progress(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    profile = CapabilityProfile(
        capability_id="cap-daemon-structured-background-runtime",
        capability_name="Structured logs, background resume, approval tickets, and timeout recovery",
        governance_key="structured-background-runtime",
        source_refs=["external_repos/hermes-agent/RELEASE_v0.8.0.md"],
        source_versions=["hermes:release-v0.8.0"],
        evidence_refs=["artifact-source-behavior"],
        known_limits=["KUN-native adaptation only."],
        promotion_stage="production",
        holdout_refs=["artifact-holdout"],
        regression_refs=["artifact-regression"],
        rollback_plan=["disable structured background runtime"],
        runtime_enabled=True,
    )
    control_plane.capability_profiles[profile.capability_id] = profile
    store.put_capability_profile(profile)
    runner = PolicyAwareRunner()
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-policy-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)
    progress_artifact = recovered.artifacts[report.progress_artifact_refs[0]]

    assert runner.bound_policy is not None
    assert runner.bound_policy.capability_profile_refs == [profile.capability_id]
    assert {directive.category for directive in runner.bound_policy.directives} >= {
        "approval",
        "diagnostics",
        "supervisor",
    }
    assert report.capability_profile_refs == [profile.capability_id]
    assert report.capability_directive_count >= 3
    assert report.activation_artifact_refs
    activated_item = recovered.work_items["work-daemon"]
    assert activated_item.required_capability_refs == [profile.capability_id]
    activation_artifact = recovered.artifacts[report.activation_artifact_refs[0]]
    assert "runtime_feature_activation" in activation_artifact.supports
    assert profile.capability_id in activation_artifact.supports
    assert "capability_execution_policy" in progress_artifact.supports
    assert profile.capability_id in progress_artifact.supports


def test_daemon_resolves_duplicate_production_capabilities_before_runtime(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    kept = CapabilityProfile(
        capability_id="cap-runtime-kept",
        capability_name="Structured background runtime kept",
        governance_key="structured-background-runtime",
        source_refs=["external/hermes"],
        source_versions=["hermes:v2"],
        evidence_refs=["artifact-strong", "artifact-extra"],
        known_limits=["KUN-native adaptation only."],
        promotion_stage="production",
        holdout_refs=["artifact-holdout"],
        regression_refs=["artifact-regression"],
        last_verified_at=NOW,
        rollback_plan=["disable kept profile"],
        runtime_enabled=True,
    )
    duplicate = CapabilityProfile(
        capability_id="cap-runtime-duplicate",
        capability_name="Structured background runtime duplicate",
        governance_key="structured-background-runtime",
        source_refs=["external/openclaw"],
        source_versions=["openclaw:v1"],
        evidence_refs=["artifact-weak"],
        known_limits=["duplicate behavior"],
        promotion_stage="production",
        holdout_refs=["artifact-holdout"],
        regression_refs=["artifact-regression"],
        rollback_plan=["disable duplicate profile"],
        runtime_enabled=True,
    )
    control_plane.capability_profiles[kept.capability_id] = kept
    control_plane.capability_profiles[duplicate.capability_id] = duplicate
    store.put_capability_profile(kept)
    store.put_capability_profile(duplicate)
    runner = PolicyAwareRunner()
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner, "qi": StaticRunner()},
        daemon_id="daemon-capability-dedupe-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.retired_capability_profile_ids == [duplicate.capability_id]
    assert any(
        work_id.startswith("work-qi-capability-dedupe-msn-daemon-")
        for work_id in report.created_work_item_ids
    )
    assert report.capability_profile_refs == [kept.capability_id]
    assert runner.bound_policy is not None
    assert runner.bound_policy.capability_profile_refs == [kept.capability_id]
    disabled = recovered.capability_profiles[duplicate.capability_id]
    assert disabled.runtime_enabled is False
    assert disabled.rolled_back_at == NOW
    assert disabled.rollback_refs == ["artifact-capability-dedupe-20260519T090000Z"]
    assert recovered.list_default_runtime_capabilities() == [
        recovered.capability_profiles[kept.capability_id]
    ]
    assert "artifact-capability-dedupe-20260519T090000Z" in recovered.artifacts


def test_daemon_turns_quality_gate_failure_into_qi_strategy_followup(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    gate = GateEvaluation(
        gate_evaluation_id="gate-low-quality",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-daemon",
        stage="delivery",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="fail",
        result_quality=0.42,
        speed=0.9,
        cost=0.9,
        risk=0.3,
        evidence_quality=0.5,
        collaboration_quality=0.5,
        thresholds={"result_quality": 0.8},
        evidence_refs=["artifact-low-quality"],
        failure_category="model_quality_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="product_quality_residual",
        created_by="external-supervisor",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-quality-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert any(
        work_id.startswith("work-qi-observation-msn-daemon-quality_gate_not_passed-")
        for work_id in report.observation_followup_ids
    )
    followup_id = next(
        work_id
        for work_id in report.observation_followup_ids
        if work_id.startswith("work-qi-observation-msn-daemon-quality_gate_not_passed-")
    )
    followup = recovered.work_items[followup_id]
    assert followup.owner == "qi"
    assert followup.type == "governance"
    assert "Audit, score, and optimize" in followup.expected_output
    assert "gate-low-quality" in followup.recovery_refs


def test_daemon_retires_human_acceptance_quality_followup_before_running(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "running",
            "task_type": "product_development",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    done_work = control_plane.work_items["work-daemon"].model_copy(update={"status": "done"})
    control_plane.work_items[done_work.work_item_id] = done_work
    store.put_work_item(done_work)
    gate = GateEvaluation(
        gate_evaluation_id="gate-human-only-quality-misclassified",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-mission-director-final",
        stage="acceptance",
        task_type="product_development",
        rubric_version="mission-director-final-v1",
        metric_pack_version="human-target-acceptance-v1",
        north_star_verdict="partial",
        result_quality=0.94,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.92,
        collaboration_quality=0.9,
        hard_gate_failures=["human_or_target_user_acceptance_missing"],
        failure_category="delivery_failure",
        next_action="needs_human",
        next_state="waiting_human",
        governance_signal="mission_director_requires_human_acceptance",
        created_by=MISSION_DIRECTOR_OWNER,
    )
    stale_followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-quality_gate_not_passed-stale",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=99,
        idempotency_key="runtime-observation:qi:quality_gate_not_passed:stale",
        expected_output="Audit stale quality_gate_not_passed evidence.",
        recovery_refs=[gate.gate_evaluation_id],
    )
    stale_plan_change = WorkItem(
        work_item_id="work-kun-plan-change-work-qi-observation-msn-daemon-quality_gate_not_passed-stale",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="research",
        owner="kun",
        status="queued",
        priority=98,
        idempotency_key=f"qi-plan-change:{stale_followup.work_item_id}",
        expected_output="Revise a stricter plan from stale quality evidence.",
        recovery_refs=[stale_followup.work_item_id, gate.gate_evaluation_id],
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    control_plane.work_items[stale_followup.work_item_id] = stale_followup
    store.put_work_item(stale_followup)
    control_plane.work_items[stale_plan_change.work_item_id] = stale_plan_change
    store.put_work_item(stale_plan_change)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner()},
        daemon_id="daemon-human-only-quality-cleanup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert stale_followup.work_item_id not in report.ran_work_item_ids
    assert stale_followup.work_item_id in report.retired_work_item_ids
    assert stale_plan_change.work_item_id in report.retired_work_item_ids
    assert recovered.work_items[stale_followup.work_item_id].status == "cancelled"
    assert recovered.work_items[stale_plan_change.work_item_id].status == "cancelled"


def test_daemon_resumes_waiting_human_when_current_product_work_is_ready(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "waiting_human",
            "task_type": "product_development",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-waiting-human-product-work-resume-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert "work-daemon" in report.ran_work_item_ids


def test_daemon_resumes_awaiting_acceptance_when_current_product_work_is_ready(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "awaiting_acceptance",
            "task_type": "product_development",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-awaiting-acceptance-product-work-resume-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert "work-daemon" in report.ran_work_item_ids
    assert recovered.work_items["work-daemon"].status == "done"
    assert recovered.missions[mission.mission_id].status == "running"


def test_daemon_runtime_observation_followup_id_is_stable_with_more_evidence(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    gate = GateEvaluation(
        gate_evaluation_id="gate-low-quality-01",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-daemon",
        stage="delivery",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="fail",
        result_quality=0.42,
        speed=0.9,
        cost=0.9,
        risk=0.3,
        evidence_quality=0.5,
        collaboration_quality=0.5,
        thresholds={"result_quality": 0.8},
        evidence_refs=["artifact-low-quality-01"],
        failure_category="model_quality_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="product_quality_residual",
        created_by="external-supervisor",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-quality-followup-stability-test",
    )

    first = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )
    first_followup_id = next(
        work_id
        for work_id in first.observation_followup_ids
        if work_id.startswith("work-qi-observation-msn-daemon-quality_gate_not_passed-")
    )
    second_gate = gate.model_copy(
        update={
            "gate_evaluation_id": "gate-low-quality-02",
            "subject_ref": "work-daemon-second",
            "evidence_refs": ["artifact-low-quality-02"],
        }
    )
    control_plane.gate_evaluations[second_gate.gate_evaluation_id] = second_gate
    store.put_gate_evaluation(second_gate)

    second = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=True,
    )
    recovered = InMemoryControlPlane(store=store)
    followups = [
        work_id
        for work_id in recovered.work_items
        if work_id.startswith("work-qi-observation-msn-daemon-quality_gate_not_passed-")
    ]

    assert followups == [first_followup_id]
    assert not second.observation_followup_ids
    assert "gate-low-quality-01" in recovered.work_items[first_followup_id].recovery_refs
    assert "gate-low-quality-02" in recovered.work_items[first_followup_id].recovery_refs


def test_daemon_requeues_done_observation_followup_when_fresh_evidence_arrives(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    gate = GateEvaluation(
        gate_evaluation_id="gate-low-quality-01",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-daemon",
        stage="delivery",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="fail",
        result_quality=0.42,
        speed=0.9,
        cost=0.9,
        risk=0.3,
        evidence_quality=0.5,
        collaboration_quality=0.5,
        thresholds={"result_quality": 0.8},
        evidence_refs=["artifact-low-quality-01"],
        failure_category="model_quality_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="product_quality_residual",
        created_by="external-supervisor",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-quality-followup-requeue-test",
    )

    first = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )
    followup_id = next(
        work_id
        for work_id in first.observation_followup_ids
        if work_id.startswith("work-qi-observation-msn-daemon-quality_gate_not_passed-")
    )
    done_followup = control_plane.work_items[followup_id].model_copy(update={"status": "done"})
    control_plane.work_items[followup_id] = done_followup
    store.put_work_item(done_followup)

    second_gate = gate.model_copy(
        update={
            "gate_evaluation_id": "gate-low-quality-02",
            "subject_ref": "work-daemon-second",
            "evidence_refs": ["artifact-low-quality-02"],
        }
    )
    control_plane.gate_evaluations[second_gate.gate_evaluation_id] = second_gate
    store.put_gate_evaluation(second_gate)

    second = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=True,
    )
    recovered = InMemoryControlPlane(store=store)
    followup = recovered.work_items[followup_id]

    assert second.observation_followup_ids == [followup_id]
    assert followup.status == "queued"
    assert "gate-low-quality-01" in followup.recovery_refs
    assert "gate-low-quality-02" in followup.recovery_refs
    assert "Continue iteration" in followup.expected_output


def test_daemon_ignores_quality_gate_after_same_subject_later_passes(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    failed_gate = GateEvaluation(
        gate_evaluation_id="gate-work-daemon-failed",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref="work-daemon",
        stage="workitem",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="fail",
        result_quality=0.42,
        speed=0.8,
        cost=0.8,
        risk=0.4,
        evidence_quality=0.5,
        collaboration_quality=0.5,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["subjective_playtest_missing"],
        failure_category="evidence_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_human",
        next_state="waiting_human",
        created_by="validation-pipeline",
    )
    passing_gate = failed_gate.model_copy(
        update={
            "gate_evaluation_id": "gate-work-daemon-passed",
            "north_star_verdict": "pass",
            "result_quality": 0.9,
            "risk": 0.1,
            "hard_gate_failures": [],
            "failure_category": None,
            "next_action": "continue",
            "next_state": "running",
        }
    )
    control_plane.gate_evaluations[failed_gate.gate_evaluation_id] = failed_gate
    control_plane.gate_evaluations[passing_gate.gate_evaluation_id] = passing_gate
    store.put_gate_evaluation(failed_gate)
    store.put_gate_evaluation(passing_gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-quality-superseded-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )

    assert not [
        work_id
        for work_id in report.observation_followup_ids
        if "quality_gate_not_passed" in work_id
    ]


def test_daemon_does_not_recurse_on_observation_followup_quality_gate(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="done",
        expected_output="Audit a prior quality gate.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    gate = GateEvaluation(
        gate_evaluation_id="gate-recursive-followup-failed",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref=followup.work_item_id,
        stage="workitem",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="fail",
        result_quality=0.3,
        speed=0.8,
        cost=0.8,
        risk=0.4,
        evidence_quality=0.4,
        collaboration_quality=0.3,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["subjective_playtest_missing"],
        failure_category="evidence_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_human",
        next_state="waiting_human",
        created_by="validation-pipeline",
    )
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    store.put_gate_evaluation(gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner()},
        daemon_id="daemon-quality-recursion-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=True,
    )

    assert not [
        work_id
        for work_id in report.observation_followup_ids
        if "quality_gate_not_passed" in work_id
    ]


def test_daemon_prioritizes_ready_rainflow_core_work_over_stale_quality_followup(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-priority",
        owner="kun",
        objective="Improve RainFlow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-priority",
        mission_id=mission.mission_id,
        version="rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 demo tests run before stale governance loops."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run tests"],
        delivery_contract={},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow isolated mission context.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Stale quality observations must not starve core phase tests."],
    )
    core = WorkItem(
        work_item_id="work-rainflow-phase1-demo-and-tests",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        priority=90,
        resource_locks=["workspace:/tmp/rainflow"],
        expected_output="Run the Phase 1 RainFlow demo and tests.",
    )
    stale_followup = WorkItem(
        work_item_id=("work-nuo-observation-msn-rainflow-priority-quality_gate_not_passed-abc123"),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="nuo",
        status="partial",
        priority=95,
        resource_locks=["workspace:/tmp/rainflow"],
        expected_output="Classify stale quality_gate_not_passed evidence.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[core, stale_followup],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-rainflow-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [core.work_item_id]
    assert recovered.work_items[core.work_item_id].status == "done"
    assert recovered.work_items[stale_followup.work_item_id].status == "cancelled"
    assert stale_followup.work_item_id in report.retired_work_item_ids


def test_daemon_prioritizes_built_rainflow_phase1_work_ids_over_quality_followups(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-built-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-built-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 core execution should run before stale quality loops."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-built-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run isolated RainFlow work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-built-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with real builder-style work ids.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Core Phase 1 work must not be starved by stale followups."],
    )
    core = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-03-phase1-mixed-edit-repair",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=90,
        resource_locks=["workspace:/tmp/rainflow-built"],
        expected_output="Run the real Phase 1 RainFlow mixed-edit repair work.",
    )
    stale_followup = WorkItem(
        work_item_id="work-qi-observation-msn-rainflow-ad-video-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="qi",
        priority=95,
        resource_locks=["workspace:/tmp/rainflow-built"],
        expected_output="Classify stale quality_gate_not_passed evidence.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[core, stale_followup],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-rainflow-built-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [core.work_item_id]
    assert recovered.work_items[core.work_item_id].status == "done"
    assert recovered.work_items[stale_followup.work_item_id].status == "cancelled"


def test_daemon_prioritizes_rainflow_core_work_over_mechanical_loop_followups(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-mechanical-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-mechanical-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Core Phase 1 work should run before mechanical loop governance."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-mechanical-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run isolated RainFlow work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-mechanical-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with mechanical loop governance.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Qi/Nuo mechanical loop work must not starve core Phase 1 work."],
    )
    core = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-02-material-screening-and-selection",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=92,
        resource_locks=["workspace:/tmp/rainflow-mechanical"],
        expected_output="Run the next core RainFlow Phase 1 material-screening slice.",
    )
    qi_followup = WorkItem(
        work_item_id=(
            "work-qi-observation-msn-rainflow-ad-video-"
            "mechanical_acceptance_rework_loop-strategy-replay-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        priority=98,
        resource_locks=["workspace:/tmp/rainflow-mechanical"],
        idempotency_key=(
            "runtime-observation:qi:work-qi-observation-msn-rainflow-ad-video-"
            "mechanical_acceptance_rework_loop-strategy-replay-abc123"
        ),
        expected_output="Run strategy replay for mechanical_acceptance_rework_loop.",
    )
    nuo_followup = WorkItem(
        work_item_id=(
            "work-nuo-observation-msn-rainflow-ad-video-mechanical_acceptance_rework_loop-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="nuo",
        priority=98,
        resource_locks=["workspace:/tmp/rainflow-mechanical"],
        idempotency_key=(
            "runtime-observation:nuo:work-nuo-observation-msn-rainflow-ad-video-"
            "mechanical_acceptance_rework_loop-abc123"
        ),
        expected_output="Classify mechanical_acceptance_rework_loop before agent scoring.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[core, qi_followup, nuo_followup],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-rainflow-mechanical-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [core.work_item_id]


def test_daemon_finalizes_current_delivery_candidate_despite_old_mechanical_loop(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-finalize-with-mechanical-loop.json")
    control_plane = InMemoryControlPlane(store=store)
    current_version = "rainflow-adflow-v1-acceptance-rework-new"
    mission = Mission(
        mission_id="msn-rainflow-finalize-loop",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version=current_version,
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-finalize-loop",
        mission_id=mission.mission_id,
        version=current_version,
        objective=mission.objective,
        acceptance_criteria=["Current delivery candidate is complete and can be finalized."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-finalize-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["finalize current isolated RainFlow delivery"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-finalize-loop",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Old rework-loop governance should not block a completed current delivery.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Finalize only the current RainFlow plan evidence."],
    )
    final_delivery = WorkItem(
        work_item_id="work-rainflow-new-12-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="merge",
        owner="kun",
        priority=90,
        expected_output="Merge current Phase 1 and Stage 1 evidence into final delivery.",
        status="done",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[final_delivery],
    )
    old_rework = WorkItem(
        work_item_id="work-rainflow-old-01-phase1-mixed-edit-repair",
        mission_id=mission.mission_id,
        task_plan_version="rainflow-adflow-v1-acceptance-rework-old",
        type="execution",
        owner="kun",
        priority=90,
        expected_output="Old rejected rework evidence.",
        status="done",
    )
    control_plane.work_items[old_rework.work_item_id] = old_rework
    store.put_work_item(old_rework)
    queued_qi_governance = WorkItem(
        work_item_id="work-qi-runtime-learning-rainflow-current-delivery",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="governance",
        owner="qi",
        priority=98,
        expected_output="Record runtime learning after product delivery.",
        status="queued",
        idempotency_key="runtime-observation:qi:rainflow-current-delivery-learning",
    )
    control_plane.work_items[queued_qi_governance.work_item_id] = queued_qi_governance
    store.put_work_item(queued_qi_governance)
    queued_mission_director = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-finalize-loop-review",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Review the completed delivery without blocking manifest finalization.",
        status="queued",
    )
    control_plane.work_items[queued_mission_director.work_item_id] = queued_mission_director
    store.put_work_item(queued_mission_director)
    running_mission = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "running"}
    )
    control_plane.missions[mission.mission_id] = running_mission
    store.put_mission(running_mission)
    control_plane.apply_gate(
        GateEvaluation(
            gate_evaluation_id="gate-rainflow-current-final-delivery",
            mission_id=mission.mission_id,
            task_plan_version=current_version,
            subject_ref=final_delivery.work_item_id,
            stage="merge",
            task_type=mission.task_type,
            rubric_version="test",
            metric_pack_version="test",
            north_star_verdict="pass",
            result_quality=0.9,
            speed=0.7,
            cost=0.7,
            risk=0.1,
            evidence_quality=0.9,
            collaboration_quality=0.8,
            score_breakdown={"current_delivery_candidate": 1.0},
            thresholds={"result_quality": 0.8},
            evidence_refs=["artifact-current-delivery"],
            artifact_refs=["artifact-current-delivery"],
            source_freshness="fresh",
            responsibility_scope="kun_auto",
            confidence=0.9,
            next_action="continue",
            next_state="running",
            governance_signal="test_current_delivery_candidate_passed",
            created_by="test",
        )
    )

    class FinalizingRunner:
        runner_identity = "finalizing-runner"

        def finalize_mission(self, mission_id: str) -> dict[str, object]:
            manifest = ArtifactManifest(
                manifest_id="manifest-rainflow-current-delivery",
                mission_id=mission_id,
                kind="delivery",
                artifact_refs=["artifact-current-delivery"],
                primary_artifact_ref="artifact-current-delivery",
                evidence_refs=["artifact-current-delivery"],
                review_refs=["artifact-current-review"],
                rollback_refs=["artifact-current-rollback"],
                created_by=self.runner_identity,
                content_hash="sha256:manifest-rainflow-current-delivery",
                supports_delivery=True,
            )
            control_plane.artifact_manifests[manifest.manifest_id] = manifest
            store.put_artifact_manifest(manifest)
            gate = GateEvaluation(
                gate_evaluation_id="gate-rainflow-runtime-delivery",
                mission_id=mission_id,
                task_plan_version=current_version,
                subject_ref=manifest.manifest_id,
                stage="delivery",
                task_type=mission.task_type,
                rubric_version="test",
                metric_pack_version="test",
                north_star_verdict="pass",
                result_quality=0.9,
                speed=0.7,
                cost=0.7,
                risk=0.1,
                evidence_quality=0.9,
                collaboration_quality=0.8,
                score_breakdown={"finalized": 1.0},
                thresholds={"result_quality": 0.8},
                evidence_refs=manifest.evidence_refs,
                review_refs=manifest.review_refs,
                artifact_refs=manifest.artifact_refs,
                source_freshness="fresh",
                responsibility_scope="kun_auto",
                confidence=0.9,
                next_action="ready_to_deliver",
                next_state="delivering",
                governance_signal="test_delivery_ready",
                created_by=self.runner_identity,
            )
            control_plane.apply_gate(gate)
            updated = control_plane.missions[mission_id].model_copy(
                update={"artifact_manifest_refs": [manifest.manifest_id]}
            )
            control_plane.missions[mission_id] = updated
            store.put_mission(updated)
            return {
                "finalized": True,
                "final_gate_ref": gate.gate_evaluation_id,
                "delivery_manifest_ref": manifest.manifest_id,
            }

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": FinalizingRunner()},
        daemon_id="daemon-rainflow-finalize-loop-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=0,
        write_progress=False,
    )

    assert any(
        item.code == "mechanical_acceptance_rework_loop"
        for item in report.runtime_observations[mission.mission_id].items
    )
    assert report.finalized_mission_ids == [mission.mission_id]
    assert report.delivery_manifest_refs == ["manifest-rainflow-current-delivery"]
    assert report.created_collaboration_ticket_ids
    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"


def test_latest_acceptance_rework_gate_ignores_same_subject_clean_pass() -> None:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-rainflow-stale-acceptance",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="awaiting_acceptance",
        current_plan_version="rainflow-adflow-v1-acceptance-rework-current",
    )
    control_plane.missions[mission.mission_id] = mission
    failed = GateEvaluation(
        gate_evaluation_id="gate-rainflow-acceptance-failed",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version or "current",
        subject_ref="work-rainflow-05-phase1-acceptance-review",
        stage="acceptance",
        task_type=mission.task_type,
        rubric_version="test",
        metric_pack_version="test",
        north_star_verdict="partial",
        result_quality=0.5,
        speed=0.7,
        cost=0.7,
        risk=0.7,
        evidence_quality=0.5,
        collaboration_quality=0.7,
        score_breakdown={"human_acceptance_missing": 1.0},
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["rainflow_phase1_human_acceptance_missing"],
        evidence_refs=[],
        artifact_refs=[],
        source_freshness="fresh",
        failure_category="delivery_failure",
        responsibility_scope="kun_auto",
        confidence=0.8,
        next_action="needs_plan_change",
        next_state="changing_plan",
        governance_signal="mission_director_supervision",
        created_by="mission-director",
    )
    passed = failed.model_copy(
        update={
            "gate_evaluation_id": "gate-rainflow-acceptance-passed",
            "north_star_verdict": "pass",
            "result_quality": 0.9,
            "risk": 0.2,
            "evidence_quality": 0.85,
            "score_breakdown": {"mission_director_alignment": 0.9},
            "hard_gate_failures": [],
            "failure_category": None,
            "next_action": "continue",
            "next_state": "running",
        }
    )

    control_plane.gate_evaluations[failed.gate_evaluation_id] = failed
    control_plane.gate_evaluations[passed.gate_evaluation_id] = passed

    assert _latest_acceptance_rework_gate(control_plane, mission) is None


def test_daemon_prioritizes_rainflow_failed_recovery_over_mechanical_loop_followups(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-recovery-before-mechanical.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-recovery-before-mechanical",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Failed work recovery should not be starved by loop governance."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-recovery-before-mechanical",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run isolated RainFlow recovery"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-recovery-before-mechanical",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow recovery should run before mechanical loop governance.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Mechanical governance must not starve recovery."],
    )
    recovery_followup = WorkItem(
        work_item_id=(
            "work-nuo-observation-msn-rainflow-ad-video-failed_work_recovery_incomplete-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="nuo",
        priority=90,
        idempotency_key=(
            "runtime-observation:nuo:work-nuo-observation-msn-rainflow-ad-video-"
            "failed_work_recovery_incomplete-abc123"
        ),
        expected_output="Classify failed_work_recovery_incomplete and unblock core recovery.",
    )
    mechanical_followup = WorkItem(
        work_item_id=(
            "work-qi-observation-msn-rainflow-ad-video-"
            "mechanical_acceptance_rework_loop-strategy-replay-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        priority=98,
        idempotency_key=(
            "runtime-observation:qi:work-qi-observation-msn-rainflow-ad-video-"
            "mechanical_acceptance_rework_loop-strategy-replay-abc123"
        ),
        expected_output="Run strategy replay for mechanical_acceptance_rework_loop.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[recovery_followup, mechanical_followup],
    )
    repairing_mission = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "repairing"}
    )
    control_plane.missions[mission.mission_id] = repairing_mission
    store.put_mission(repairing_mission)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-rainflow-recovery-before-mechanical-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [recovery_followup.work_item_id]


def test_daemon_prioritizes_rainflow_phase1_acceptance_review_over_stale_supervision(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-review-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-review-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 acceptance review is core product work."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-review-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run isolated RainFlow review"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-review-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with core acceptance review.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Stale Mission Director reviews must not starve Phase 1 acceptance."],
    )
    phase1_review = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-05-phase1-acceptance-review",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=90,
        resource_locks=["workspace:/tmp/rainflow-review"],
        expected_output="Review Phase 1 acceptance before unlocking AI stages.",
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[phase1_review, stale_review],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={MISSION_DIRECTOR_OWNER: StaticRunner()},
        daemon_id="daemon-rainflow-review-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [phase1_review.work_item_id]


def test_daemon_prioritizes_rainflow_environment_recovery_over_stale_supervision(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-recovery-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-recovery-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=[
            "Environment recovery for core Phase 1 work must resume before stale supervision."
        ],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-recovery-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["rerun isolated RainFlow work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-recovery-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with an environment recovery rerun.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Governance must observe without starving core recovery."],
    )
    recovery = WorkItem(
        work_item_id=(
            "work-nuo-work-msn-rainflow-ad-video-rainflow-phase1-"
            "02-material-screening-and-selection-rerun"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="control-plane",
        priority=95,
        expected_output=(
            "Rerun the same subject after the environment blocker is cleared; "
            "network EOF must not count as product completion."
        ),
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    stale_quality = WorkItem(
        work_item_id="work-qi-observation-msn-rainflow-ad-video-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="governance",
        owner="qi",
        priority=98,
        expected_output="Classify stale quality_gate_not_passed evidence.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[recovery, stale_review, stale_quality],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "control-plane": StaticRunner(),
            "qi": StaticRunner(),
            MISSION_DIRECTOR_OWNER: StaticRunner(),
        },
        daemon_id="daemon-rainflow-recovery-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [recovery.work_item_id]


def test_daemon_prioritizes_rainflow_sandbox_wrapper_recovery_over_stale_supervision(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-wrapper-recovery-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-wrapper-recovery-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=[
            "Sandbox wrapper recovery for core Phase 1 tests must run before stale supervision."
        ],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-wrapper-recovery-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["repair isolated RainFlow test wrapper"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-wrapper-recovery-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with sandbox wrapper recovery.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Mission Director must not starve core recovery."],
    )
    recovery = WorkItem(
        work_item_id=(
            "work-nuo-work-msn-rainflow-ad-video-rainflow-phase1-"
            "04-phase1-demo-comparison-and-retest-fix_wrapper"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="repair",
        owner="control-plane",
        priority=95,
        idempotency_key=(
            "nuo-recovery:work-msn-rainflow-ad-video-rainflow-phase1-"
            "04-phase1-demo-comparison-and-retest:fix_wrapper:sandbox_permission_blocked"
        ),
        expected_output=(
            "Repair wrapper availability or contract compatibility, then rerun the same subject. "
            "Nuo findings: sandbox_permission_blocked."
        ),
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    plan_change = WorkItem(
        work_item_id="work-kun-plan-change-work-mission-director-msn-rainflow-ad-video",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner="kun",
        priority=100,
        expected_output="Revise the RainFlow plan after a Mission Director blocker.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[recovery, stale_review, plan_change],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "control-plane": StaticRunner(),
            MISSION_DIRECTOR_OWNER: StaticRunner(),
        },
        daemon_id="daemon-rainflow-wrapper-recovery-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [recovery.work_item_id]


def test_daemon_retires_stale_mission_director_when_plan_change_is_ready(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-plan-change-handoff.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-plan-change-handoff",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Plan-change handoff should run before another review loop."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-plan-change-handoff",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["revise isolated RainFlow plan"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-plan-change-handoff",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with a Mission Director plan-change handoff.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Mission Director review must not stack while plan-change is ready."],
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    plan_change = WorkItem(
        work_item_id="work-kun-plan-change-work-mission-director-msn-rainflow-ad-video",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="research",
        owner="kun",
        priority=100,
        expected_output="Revise the RainFlow plan after a Mission Director blocker.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[stale_review, plan_change],
    )
    control_plane.missions[mission.mission_id] = mission.model_copy(
        update={"status": "changing_plan"}
    )
    control_plane.store.put_mission(control_plane.missions[mission.mission_id])
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), MISSION_DIRECTOR_OWNER: StaticRunner()},
        daemon_id="daemon-rainflow-plan-change-handoff-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert stale_review.work_item_id in report.retired_work_item_ids
    assert report.ran_work_item_ids == [plan_change.work_item_id]
    assert recovered.work_items[stale_review.work_item_id].status == "cancelled"
    assert recovered.work_items[plan_change.work_item_id].status == "done"
    assert not [
        item
        for item in recovered.work_items.values()
        if item.mission_id == mission.mission_id
        and item.owner == MISSION_DIRECTOR_OWNER
        and item.status in {"queued", "running", "retrying"}
    ]


def test_daemon_retires_mission_director_after_accepted_delivery(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-accepted-director-cleanup.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-accepted-director-cleanup",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Accepted delivery should not keep stale review work queued."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-accepted-director-cleanup",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["clean stale control-plane work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-accepted-director-cleanup",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Accepted RainFlow delivery should leave no active review residue.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not run stale Mission Director review after human acceptance."],
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[stale_review],
    )
    accepted_mission = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "learning_writeback",
            "acceptance_ref": "accept-rainflow-delivery",
        }
    )
    control_plane.missions[mission.mission_id] = accepted_mission
    store.put_mission(accepted_mission)
    control_plane.acceptance_reviews["accept-rainflow-delivery"] = AcceptanceReview(
        acceptance_id="accept-rainflow-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        delivery_manifest_ref="manifest-rainflow-delivery",
        gate_evaluation_ref="gate-rainflow-delivery",
        reviewer="human-simulator",
        decision="accepted",
        satisfaction=0.9,
        reason="Accepted current RainFlow delivery.",
    )
    store.put_acceptance_review(control_plane.acceptance_reviews["accept-rainflow-delivery"])
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={MISSION_DIRECTOR_OWNER: StaticRunner()},
        daemon_id="daemon-rainflow-accepted-director-cleanup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert stale_review.work_item_id in report.retired_work_item_ids
    assert report.ran_work_item_ids == []
    assert recovered.work_items[stale_review.work_item_id].status == "cancelled"


def test_daemon_retires_followup_residue_and_old_plan_work_after_accepted_delivery(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-accepted-followup-cleanup.json")
    control_plane = InMemoryControlPlane(store=store)
    current_version = "rainflow-phase1-mixed-edit-v2"
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version=current_version,
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-accepted-followup-cleanup",
        mission_id=mission.mission_id,
        version=current_version,
        objective=mission.objective,
        acceptance_criteria=["Accepted delivery should not keep stale followup-only work queued."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-accepted-followup-cleanup",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["clean stale control-plane work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-accepted-followup-cleanup",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="Accepted RainFlow delivery should collapse stale followup residue.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Do not keep accepted missions busy with stale governance loops."],
    )
    queued_qi_observation = WorkItem(
        work_item_id="work-qi-observation-msn-rainflow-ad-video-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="governance",
        owner="qi",
        status="queued",
        expected_output="Audit stale quality gate evidence.",
        idempotency_key="runtime-observation:qi:accepted-quality-gap",
    )
    queued_nuo_observation = WorkItem(
        work_item_id="work-nuo-observation-msn-rainflow-ad-video-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="repair",
        owner="nuo",
        status="queued",
        expected_output="Classify stale quality gate evidence.",
        idempotency_key="runtime-observation:nuo:accepted-quality-gap",
    )
    queued_runtime_learning = WorkItem(
        work_item_id="work-qi-runtime-learning-work-rainflow-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="governance",
        owner="qi",
        status="queued",
        expected_output="Record runtime learning after delivery.",
        idempotency_key="qi-runtime-learning:work-rainflow-final-delivery:status:failed",
    )
    queued_plan_change = WorkItem(
        work_item_id="work-kun-plan-change-rainflow-accepted",
        mission_id=mission.mission_id,
        task_plan_version=current_version,
        type="execution",
        owner="kun",
        status="queued",
        expected_output="Stale accepted-plan change handoff.",
        idempotency_key="mission-director-plan-change:accepted-rainflow",
    )
    old_plan_work = WorkItem(
        work_item_id="work-rainflow-old-plan-repair",
        mission_id=mission.mission_id,
        task_plan_version="rainflow-phase1-mixed-edit-v1",
        type="execution",
        owner="kun",
        status="queued",
        expected_output="Old rejected plan work should be retired.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[
            queued_qi_observation,
            queued_nuo_observation,
            queued_runtime_learning,
            queued_plan_change,
        ],
    )
    control_plane.work_items[old_plan_work.work_item_id] = old_plan_work
    store.put_work_item(old_plan_work)
    accepted_mission = control_plane.missions[mission.mission_id].model_copy(
        update={
            "status": "learning_writeback",
            "acceptance_ref": "accept-rainflow-delivery",
        }
    )
    control_plane.missions[mission.mission_id] = accepted_mission
    store.put_mission(accepted_mission)
    control_plane.acceptance_reviews["accept-rainflow-delivery"] = AcceptanceReview(
        acceptance_id="accept-rainflow-delivery",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        delivery_manifest_ref="manifest-rainflow-delivery",
        gate_evaluation_ref="gate-rainflow-delivery",
        reviewer="human-simulator",
        decision="accepted",
        satisfaction=0.9,
        reason="Accepted current RainFlow delivery.",
    )
    store.put_acceptance_review(control_plane.acceptance_reviews["accept-rainflow-delivery"])
    daemon = ControlPlaneDaemon(
        control_plane=control_plane, daemon_id="daemon-rainflow-accepted-followup-cleanup-test"
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == []
    assert set(report.retired_work_item_ids) >= {
        queued_qi_observation.work_item_id,
        queued_nuo_observation.work_item_id,
        queued_runtime_learning.work_item_id,
        queued_plan_change.work_item_id,
        old_plan_work.work_item_id,
    }
    assert recovered.work_items[queued_qi_observation.work_item_id].status == "cancelled"
    assert recovered.work_items[queued_nuo_observation.work_item_id].status == "cancelled"
    assert recovered.work_items[queued_runtime_learning.work_item_id].status == "cancelled"
    assert recovered.work_items[queued_plan_change.work_item_id].status == "cancelled"
    assert recovered.work_items[old_plan_work.work_item_id].status == "cancelled"


def test_daemon_restores_cancelled_rainflow_phase1_acceptance_review(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-review-restore.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-review-restore",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Phase 1 acceptance review must stay in the core chain."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-review-restore",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["repair isolated RainFlow plan state"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-review-restore",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with a cancelled Phase 1 review.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Governance cleanup must not remove core Phase 1 acceptance."],
    )
    demo_retest = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-04-phase1-demo-comparison-and-retest",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        priority=92,
        status="done",
        expected_output="Run Phase 1 demo comparison.",
    )
    phase1_review = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-05-phase1-acceptance-review",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=92,
        status="cancelled",
        dependencies=[demo_retest.work_item_id],
        expected_output="Review Phase 1 acceptance before unlocking AI stages.",
    )
    stage1 = WorkItem(
        work_item_id="work-msn-rainflow-ad-video-06-stage1-transition-generation",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        priority=92,
        dependencies=[phase1_review.work_item_id],
        expected_output="Only after Phase 1 passes, generate transition clips.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[demo_retest, phase1_review, stage1],
    )
    control_plane.missions[mission.mission_id] = mission.model_copy(update={"status": "running"})
    control_plane.store.put_mission(control_plane.missions[mission.mission_id])
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), MISSION_DIRECTOR_OWNER: StaticRunner()},
        daemon_id="daemon-rainflow-review-restore-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert phase1_review.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[phase1_review.work_item_id].status == "queued"


def test_daemon_prioritizes_rainflow_environment_clean_retest_over_stale_supervision(
    tmp_path,
) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-clean-retest-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-clean-retest-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Nuo clean retest must close environment recovery first."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-clean-retest-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["rerun isolated RainFlow work"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-clean-retest-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with a clean retest recovery step.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Clean retest is part of core recovery, not stale governance."],
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-work-nuo-work-msn-rainflow-ad-video-rerun-abc123",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="retest",
        owner="nuo",
        priority=95,
        idempotency_key="nuo-clean-retest:work-nuo-work-msn-rainflow-ad-video-rerun",
        expected_output=(
            "Run a Nuo clean retest for the prior partial repair diagnosis. "
            "Verify whether the workspace/write/environment blocker is cleared."
        ),
        recovery_refs=["work-nuo-work-msn-rainflow-ad-video-rerun"],
    )
    stale_review = WorkItem(
        work_item_id="work-mission-director-msn-rainflow-ad-video-stale",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        priority=100,
        expected_output="Stale general Mission Director supervision.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[clean_retest, stale_review],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "nuo": StaticRunner(),
            MISSION_DIRECTOR_OWNER: StaticRunner(),
        },
        daemon_id="daemon-rainflow-clean-retest-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [clean_retest.work_item_id]


def test_daemon_keeps_observation_clean_retest_behind_rainflow_core_test(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "rainflow-observation-clean-retest-priority.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id="msn-rainflow-ad-video",
        owner="kun",
        objective="Improve RainFlow information-flow ad mixed-edit quality.",
        task_type="product_development",
        status="contracted",
        current_plan_version="rainflow-phase1-mixed-edit-v1",
    )
    plan = TaskPlan(
        plan_id="plan-rainflow-observation-clean-retest-priority",
        mission_id=mission.mission_id,
        version=mission.current_plan_version or "rainflow-phase1-mixed-edit-v1",
        objective=mission.objective,
        acceptance_criteria=["Core RainFlow tests should not be starved by observation retests."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-rainflow-observation-clean-retest-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        allowed_actions=["run isolated RainFlow tests"],
        delivery_contract={"production_mode": RAINFLOW_AD_PRODUCTION_MODE},
    )
    context = WorkingContext(
        working_context_id="ctx-rainflow-observation-clean-retest-priority",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        audience="rainflow",
        scope="isolated-rainflow",
        summary="RainFlow mission context with an observation clean retest.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["Qi/Nuo observation clean retests must not block core product tests."],
    )
    core_test = WorkItem(
        work_item_id="work-kun-retest-stage1-seedance-evidence-link-after-fix-20260525",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="test",
        owner="kun",
        priority=98,
        resource_locks=["workspace:/tmp/rainflow"],
        expected_output="Run fresh isolated RainFlow Stage 1 Seedance retest evidence.",
    )
    observation_clean_retest = WorkItem(
        work_item_id=(
            "work-nuo-clean-retest-work-nuo-observation-msn-rainflow-ad-video-"
            "failed_work_recovery_incomplete-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        type="retest",
        owner="nuo",
        priority=90,
        idempotency_key=(
            "nuo-clean-retest:work-nuo-observation-msn-rainflow-ad-video-"
            "failed_work_recovery_incomplete-abc123"
        ),
        resource_locks=["workspace:/tmp/rainflow"],
        expected_output=(
            "Run a Nuo clean retest for the prior partial repair diagnosis. "
            "Verify whether the workspace/write/environment blocker is cleared."
        ),
        recovery_refs=[
            "work-nuo-observation-msn-rainflow-ad-video-failed_work_recovery_incomplete-abc123"
        ],
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[core_test, observation_clean_retest],
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-rainflow-observation-clean-retest-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)

    assert report.ran_work_item_ids == [core_test.work_item_id]


def test_daemon_turns_human_collaboration_work_item_into_ticket(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    human_work = WorkItem(
        work_item_id="work-nuo-quality-gate-request_human_playtest",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="collaboration",
        owner="operator",
        priority=99,
        expected_output="Simulate a bounded human playtest decision before final closure.",
    )
    control_plane.work_items[human_work.work_item_id] = human_work
    store.put_work_item(human_work)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-human-ticket-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)
    ticket_id = f"collab-work-item-{human_work.work_item_id}"

    assert human_work.work_item_id not in report.no_runner_work_item_ids
    assert report.created_collaboration_ticket_ids == [ticket_id]
    assert recovered.collaboration_tickets[ticket_id].context_ref == human_work.work_item_id
    assert recovered.work_items[human_work.work_item_id].status == "waiting_human"


def test_daemon_runs_delivery_state_governance_followup_without_leaving_acceptance(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    original = control_plane.work_items["work-daemon"].model_copy(update={"status": "cancelled"})
    control_plane.work_items[original.work_item_id] = original
    store.put_work_item(original)
    followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        expected_output="Audit, score, and optimize quality gate residuals.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner()},
        daemon_id="daemon-acceptance-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.missions[mission.mission_id].status == "awaiting_acceptance"


def test_daemon_runs_info_gap_governance_followup_without_resuming_product_work(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "info_gap",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    plan = control_plane.task_plans["plan-daemon"].model_copy(
        update={"info_gaps": ["Need acceptance criteria before product work resumes."]}
    )
    control_plane.task_plans[plan.plan_id] = plan
    store.put_task_plan(plan)
    product_work = control_plane.work_items["work-daemon"].model_copy(
        update={"status": "queued", "priority": 99}
    )
    control_plane.work_items[product_work.work_item_id] = product_work
    store.put_work_item(product_work)
    followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-human_ticket_opened-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=10,
        expected_output="Audit the information-gap ticket before product work resumes.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-info-gap-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.work_items[product_work.work_item_id].status == "queued"
    assert recovered.collaboration_tickets["collab-info-gap-msn-daemon-v1"].status == "open"
    assert recovered.missions[mission.mission_id].status == "waiting_human"


def test_daemon_does_not_open_generic_info_gap_ticket_without_real_gaps(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "info_gap",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=10,
        expected_output="Audit quality gate recovery without asking for fake missing info.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner()},
        daemon_id="daemon-no-generic-info-gap-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert report.created_collaboration_ticket_ids == []
    assert "collab-info-gap-msn-daemon-v1" not in recovered.collaboration_tickets


def test_daemon_runs_existing_info_gap_followup_without_invalid_resume(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "info_gap",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    product_work = control_plane.work_items["work-daemon"].model_copy(
        update={"status": "queued", "priority": 99}
    )
    control_plane.work_items[product_work.work_item_id] = product_work
    store.put_work_item(product_work)
    ticket = CollaborationTicket(
        ticket_id="collab-info-gap-msn-daemon-v1",
        mission_id=mission.mission_id,
        type="expert_input",
        role_needed="product-owner",
        why_needed="Need acceptance criteria before product work resumes.",
        context_ref="plan-daemon",
        risk_if_skipped="Product work may resume against missing constraints.",
        deadline=NOW + timedelta(hours=24),
        output_contract="Resolve the information gap.",
    )
    control_plane.collaboration_tickets[ticket.ticket_id] = ticket
    store.put_collaboration_ticket(ticket)
    followup = WorkItem(
        work_item_id="work-qi-observation-msn-daemon-quality_gate_not_passed-abc123",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=10,
        expected_output="Audit the existing information-gap follow-up.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-existing-info-gap-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.work_items[product_work.work_item_id].status == "queued"
    assert recovered.missions[mission.mission_id].status == "info_gap"


def test_daemon_resumes_stale_info_gap_when_no_open_ticket_or_plan_gap(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    stale_info_gap = mission.model_copy(update={"status": "info_gap"})
    control_plane.missions[mission.mission_id] = stale_info_gap
    store.put_mission(stale_info_gap)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-stale-info-gap-resume-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-daemon"]
    assert recovered.work_items["work-daemon"].status == "done"
    assert not recovered.progress_report(mission.mission_id).open_collaboration_ticket_ids


def test_daemon_runs_nuo_recovery_followup_in_info_gap_without_product_work(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "info_gap",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    product_work = control_plane.work_items["work-daemon"].model_copy(
        update={"status": "queued", "priority": 99}
    )
    control_plane.work_items[product_work.work_item_id] = product_work
    store.put_work_item(product_work)
    followup = WorkItem(
        work_item_id=(
            "work-nuo-work-game-rework-work-kun-plan-change-msn-daemon-final-delivery"
            "-collect_report"
        ),
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="research",
        owner="qi",
        status="queued",
        priority=10,
        idempotency_key="nuo-recovery:work-game-rework-msn-daemon-final-delivery:collect_report",
        expected_output="Collect or regenerate the missing report artifact before delivery.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": ReportArtifactRunner()},
        daemon_id="daemon-info-gap-nuo-recovery-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.work_items[product_work.work_item_id].status == "queued"
    assert recovered.missions[mission.mission_id].status == "info_gap"
    assert "collab-info-gap-msn-daemon-v1" not in recovered.collaboration_tickets


def test_daemon_runs_lower_priority_delivery_followup_before_product_work(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    product_work = control_plane.work_items["work-daemon"].model_copy(
        update={
            "priority": 99,
            "owner": "kun",
            "type": "execution",
            "status": "queued",
        }
    )
    control_plane.work_items[product_work.work_item_id] = product_work
    store.put_work_item(product_work)
    followup = WorkItem(
        work_item_id=(
            "work-qi-strategy-replay-work-qi-observation-msn-daemon-quality_gate_not_passed-abc123"
        ),
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        priority=10,
        expected_output="Run an isolated Qi strategy replay before more product work.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner(), "qi": StaticRunner()},
        daemon_id="daemon-acceptance-followup-priority-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.work_items[product_work.work_item_id].status == "queued"
    assert recovered.missions[mission.mission_id].status == "awaiting_acceptance"


def test_daemon_refreshes_existing_work_item_requeued_by_external_process(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    original = control_plane.work_items["work-daemon"]
    blocked = original.model_copy(update={"status": "blocked"})
    control_plane.work_items[blocked.work_item_id] = blocked
    store.put_work_item(blocked)
    external_store = FileControlPlaneStore(store.path)
    external_store.put_work_item(blocked.model_copy(update={"status": "queued"}))
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-refresh-requeued-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-daemon"]
    assert recovered.work_items["work-daemon"].status == "done"


def test_daemon_runs_runtime_learning_followup_while_awaiting_acceptance(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": "v1",
        }
    )
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    original = control_plane.work_items["work-daemon"].model_copy(update={"status": "cancelled"})
    control_plane.work_items[original.work_item_id] = original
    store.put_work_item(original)
    followup = WorkItem(
        work_item_id="work-qi-runtime-learning-work-daemon",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="governance",
        owner="qi",
        status="queued",
        expected_output="Review this runtime signal as a capability candidate.",
    )
    control_plane.work_items[followup.work_item_id] = followup
    store.put_work_item(followup)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner()},
        daemon_id="daemon-runtime-learning-followup-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == [followup.work_item_id]
    assert recovered.work_items[followup.work_item_id].status == "done"
    assert recovered.missions[mission.mission_id].status == "awaiting_acceptance"


def test_daemon_retires_superseded_plan_work_while_mission_is_running(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    old_work = control_plane.work_items["work-daemon"]
    next_plan = TaskPlan(
        plan_id="plan-daemon-v2",
        mission_id=mission.mission_id,
        version="v2",
        objective=mission.objective,
        acceptance_criteria=["current plan work remains active"],
        constraints=[],
        approval_status="approved",
    )
    current_work = WorkItem(
        work_item_id="work-daemon-v2",
        mission_id=mission.mission_id,
        task_plan_version=next_plan.version,
        type="execution",
        owner="kun",
        priority=80,
        expected_output="current plan result",
    )
    mission = mission.model_copy(
        update={"status": "running", "current_plan_version": next_plan.version}
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[next_plan.plan_id] = next_plan
    control_plane.work_items[current_work.work_item_id] = current_work
    store.put_mission(mission)
    store.put_task_plan(next_plan)
    store.put_work_item(current_work)

    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-cleanup-test")

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert report.retired_work_item_ids == [old_work.work_item_id]
    assert recovered.work_items[old_work.work_item_id].status == "cancelled"
    assert recovered.work_items[current_work.work_item_id].status == "queued"


def test_daemon_routes_unrecovered_failed_work_to_qi_and_nuo(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    failed = control_plane.work_items["work-daemon"].model_copy(update={"status": "failed"})
    control_plane.work_items[failed.work_item_id] = failed
    store.put_work_item(failed)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-failed-work-observation-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert sorted(report.observation_followup_ids) == [
        "work-nuo-observation-msn-daemon-failed_work_without_recovery-e188ff3c555f",
        "work-qi-observation-msn-daemon-failed_work_without_recovery-recovery_v1-e188ff3c555f",
    ]
    assert (
        recovered.work_items[
            "work-qi-observation-msn-daemon-failed_work_without_recovery-recovery_v1-e188ff3c555f"
        ].owner
        == "qi"
    )
    assert (
        recovered.work_items[
            "work-nuo-observation-msn-daemon-failed_work_without_recovery-e188ff3c555f"
        ].owner
        == "nuo"
    )


def test_daemon_runs_skill_preflight_for_activated_work_items(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
    control_plane, store, mission = _runtime(tmp_path)
    contract = control_plane.contracts["contract-daemon"].model_copy(
        update={"delivery_contract": {"project_path": str(workspace)}}
    )
    control_plane.contracts[contract.contract_id] = contract
    store.put_execution_contract(contract)
    item = control_plane.work_items["work-daemon"].model_copy(
        update={"expected_output": "inspect app files before execution"}
    )
    control_plane.work_items[item.work_item_id] = item
    store.put_work_item(item)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-preflight-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    recovered = InMemoryControlPlane(store=store)

    assert report.preflight_artifact_refs
    preflight_artifact = recovered.artifacts[report.preflight_artifact_refs[0]]
    assert "skill_preflight" in preflight_artifact.supports
    assert "skill:shell-exec" in preflight_artifact.supports
    preflight_payload = json.loads(Path(preflight_artifact.path_or_uri).read_text(encoding="utf-8"))
    assert preflight_payload["result"]["ok"] is True
    assert str(workspace) in preflight_payload["result"]["metadata"]["sandbox_roots"]
    assert preflight_artifact.path_or_uri.endswith(".json")
    assert recovered.work_items["work-daemon"].workspace_ref == f"workspace://{workspace}"


def test_daemon_routes_preflight_failures_to_qi_and_nuo_when_available(
    tmp_path,
    monkeypatch,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)

    def fake_preflight(*, control_plane, work_item, actor, observed_at):
        return WorkItemPreflight(
            artifacts=[
                ArtifactRecord(
                    artifact_id="artifact-preflight-failed",
                    kind="test_result",
                    path_or_uri="mem://preflight-failed",
                    content_hash="hash-preflight-failed",
                    created_by=actor,
                    mission_id=work_item.mission_id,
                    work_item_id=work_item.work_item_id,
                    supports=["skill_preflight_failure", "skill:shell-exec"],
                )
            ],
            failed_skill_ids=["shell-exec"],
        )

    monkeypatch.setattr("kun.control_plane.daemon.run_work_item_preflight", fake_preflight)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "kun": StaticRunner(),
            "nuo": StaticRunner(),
            "qi": StaticRunner(),
        },
        daemon_id="daemon-preflight-failure-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.preflight_failed_skill_ids == ["shell-exec"]
    assert {
        "work-nuo-preflight-work-daemon",
        "work-qi-preflight-work-daemon",
    }.issubset(set(report.created_work_item_ids))
    assert recovered.work_items["work-nuo-preflight-work-daemon"].owner == "nuo"
    assert recovered.work_items["work-qi-preflight-work-daemon"].owner == "qi"
    assert (
        "artifact-preflight-failed"
        in recovered.work_items["work-nuo-preflight-work-daemon"].recovery_refs
    )


def test_daemon_prioritizes_preflight_followups_before_downstream_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    downstream = WorkItem(
        work_item_id="work-final-delivery",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="merge",
        owner="kun",
        dependencies=["work-daemon"],
        priority=85,
        expected_output="deliver only after governance follow-ups are clear",
    )
    control_plane.work_items[downstream.work_item_id] = downstream
    store.put_work_item(downstream)

    def fake_preflight(*, control_plane, work_item, actor, observed_at):
        if work_item.work_item_id != "work-daemon":
            return WorkItemPreflight()
        return WorkItemPreflight(
            artifacts=[
                ArtifactRecord(
                    artifact_id="artifact-preflight-failed",
                    kind="test_result",
                    path_or_uri="mem://preflight-failed",
                    content_hash="hash-preflight-failed",
                    created_by=actor,
                    mission_id=work_item.mission_id,
                    work_item_id=work_item.work_item_id,
                    supports=["skill_preflight_failure", "skill:shell-exec"],
                )
            ],
            failed_skill_ids=["shell-exec"],
        )

    monkeypatch.setattr("kun.control_plane.daemon.run_work_item_preflight", fake_preflight)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "kun": StaticRunner(),
            "nuo": StaticRunner(),
            "qi": StaticRunner(),
        },
        daemon_id="daemon-preflight-priority-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=3,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids[0] == "work-daemon"
    assert set(report.ran_work_item_ids[1:]) == {
        "work-nuo-preflight-work-daemon",
        "work-qi-preflight-work-daemon",
    }
    assert recovered.work_items["work-final-delivery"].status == "queued"


def test_daemon_does_not_recurse_preflight_followups(
    tmp_path,
    monkeypatch,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    qi_followup = control_plane.work_items["work-daemon"].model_copy(
        update={
            "work_item_id": "work-qi-preflight-work-daemon",
            "owner": "qi",
            "type": "governance",
        }
    )
    control_plane.work_items.pop("work-daemon")
    control_plane.work_items[qi_followup.work_item_id] = qi_followup
    store.put_work_item(qi_followup)

    def fake_preflight(*, control_plane, work_item, actor, observed_at):
        raise AssertionError("Qi/Nuo runtime follow-ups should not run skill preflight")

    monkeypatch.setattr("kun.control_plane.daemon.run_work_item_preflight", fake_preflight)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": StaticRunner(), "nuo": StaticRunner()},
        daemon_id="daemon-preflight-recursion-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-qi-preflight-work-daemon"]
    assert report.preflight_failed_skill_ids == []
    assert report.preflight_artifact_refs == []
    assert report.created_work_item_ids == []
    assert all(
        not item_id.startswith("work-qi-preflight-work-qi-preflight")
        for item_id in recovered.work_items
    )


def test_daemon_productization_runner_executes_canonical_work_items(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "daemon-productization.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = submit_productization_dogfood_mission(
        control_plane,
        build_productization_dogfood_mission(mission_id="msn-daemon-productization"),
    )
    runner = ProductizationDogfoodRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "control-plane": runner,
            "qi": runner,
            "nuo": runner,
        },
        daemon_id="daemon-productization-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=1,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.ran_work_item_ids == ["work-v6-persistence-recovery"]
    assert recovered.work_items["work-v6-persistence-recovery"].status == "done"
    assert report.no_runner_work_item_ids == []


def test_daemon_productization_runner_finalizes_delivery_when_queue_done(tmp_path) -> None:
    store = FileControlPlaneStore(tmp_path / "daemon-productization-finalize.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = submit_productization_dogfood_mission(
        control_plane,
        build_productization_dogfood_mission(mission_id="msn-daemon-productization"),
    )
    _prepare_productization_closures(control_plane, mission.mission_id)
    runner = ProductizationDogfoodRunner(
        control_plane=control_plane,
        ab_round_dir=_write_passing_ab_round(tmp_path),
        ab_round_id="round-02-regression",
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "control-plane": runner,
            "qi": runner,
            "nuo": runner,
        },
        daemon_id="daemon-productization-test",
    )

    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW,
        max_work_items=10,
    )
    recovered = InMemoryControlPlane(store=store)
    manifest_refs_after_first_tick = list(
        recovered.missions[mission.mission_id].artifact_manifest_refs
    )
    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(seconds=1),
        max_work_items=10,
    )
    after_second_tick = InMemoryControlPlane(store=store)

    assert len(report.ran_work_item_ids) == 6
    assert "work-v6-collaboration-tickets" not in report.ran_work_item_ids
    assert recovered.work_items["work-v6-collaboration-tickets"].status == "done"
    assert report.finalized_mission_ids == [mission.mission_id]
    assert report.delivery_manifest_refs == ["manifest-msn-daemon-productization-delivery"]
    assert report.final_gate_refs == ["gate-msn-daemon-productization-delivery"]
    assert recovered.missions[mission.mission_id].status == "delivering"
    assert recovered.artifact_manifests[
        "manifest-msn-daemon-productization-delivery"
    ].supports_delivery
    assert second_report.ran_work_item_ids == []
    assert after_second_tick.missions[mission.mission_id].artifact_manifest_refs == (
        manifest_refs_after_first_tick
    )


def test_daemon_runner_can_run_guard_prevents_misrouting_generic_qi_work(
    tmp_path,
) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    generic_qi_work = control_plane.work_items["work-daemon"].model_copy(
        update={"owner": "qi", "type": "research", "work_item_id": "work-generic-qi"}
    )
    control_plane.work_items.pop("work-daemon")
    control_plane.work_items[generic_qi_work.work_item_id] = generic_qi_work
    assert control_plane.store is not None
    control_plane.store.put_work_item(generic_qi_work)
    runner = ProductizationDogfoodRunner(control_plane=control_plane)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": runner},
        daemon_id="daemon-productization-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)

    assert report.no_runner_work_item_ids == ["work-generic-qi"]
    assert report.ran_work_item_ids == []


def test_daemon_skips_no_runner_item_within_tick_and_runs_next_ready_work(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    no_runner_work = WorkItem(
        work_item_id="work-control-plane-report-gap",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="research",
        owner="control-plane",
        priority=100,
        expected_output="Collect a missing report artifact.",
    )
    control_plane.work_items[no_runner_work.work_item_id] = no_runner_work
    store.put_work_item(no_runner_work)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-no-runner-skip-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=2)

    assert report.no_runner_work_item_ids == ["work-control-plane-report-gap"]
    assert report.ran_work_item_ids == ["work-daemon"]


def test_daemon_runs_control_plane_nuo_recovery_with_repair_runner(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    failed_subject = control_plane.work_items["work-daemon"].model_copy(update={"status": "failed"})
    control_plane.work_items[failed_subject.work_item_id] = failed_subject
    store.put_work_item(failed_subject)
    recovery = WorkItem(
        work_item_id="work-nuo-work-main-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        priority=100,
        idempotency_key="nuo-recovery:work-daemon:fix_wrapper:sandbox_permission_blocked",
        expected_output="Repair wrapper availability or contract compatibility.",
        workspace_ref=str(project_path),
        resource_locks=[f"workspace:{project_path}"],
    )
    control_plane.work_items[recovery.work_item_id] = recovery
    store.put_work_item(recovery)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "control-plane": NuoRuntimeRepairRunner(control_plane=control_plane),
            "nuo": NuoRuntimeRepairRunner(control_plane=control_plane),
            "kun": StaticRunner(),
        },
        daemon_id="daemon-control-plane-nuo-recovery-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered = InMemoryControlPlane(store=store)

    assert report.no_runner_work_item_ids == []
    assert report.ran_work_item_ids == [recovery.work_item_id]
    assert recovered.work_items[recovery.work_item_id].status == "partial"
    retest_ids = [
        work_item_id
        for work_item_id in report.created_work_item_ids
        if work_item_id.startswith("work-nuo-clean-retest-")
    ]
    assert len(retest_ids) == 1
    retest = recovered.work_items[retest_ids[0]]
    assert retest.status == "queued"
    assert retest.owner == "nuo"
    assert retest.type == "retest"
    assert recovery.work_item_id in retest.recovery_refs
    assert retest.workspace_ref == f"workspace://{project_path}"

    daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered_after_retest = InMemoryControlPlane(store=store)
    assert recovered_after_retest.work_items[retest.work_item_id].status == "done"
    clean_retest_gates = [
        gate
        for gate in recovered_after_retest.gate_evaluations.values()
        if gate.subject_ref == retest.work_item_id
    ]
    assert clean_retest_gates
    assert clean_retest_gates[0].governance_signal == "nuo_clean_retest_passed"
    requeued_subject = recovered_after_retest.work_items["work-daemon"]
    assert requeued_subject.status == "queued"
    assert any(
        ref.startswith("artifact-nuo-runtime-repair-runner-work-nuo-clean-retest")
        for ref in requeued_subject.recovery_refs
    )


def test_daemon_requeues_stale_waiting_human_clean_retest_when_workspace_writable(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    partial_repair = WorkItem(
        work_item_id="work-nuo-work-daemon-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        status="partial",
        priority=95,
        idempotency_key="nuo-recovery:work-daemon:fix_wrapper:sandbox_permission_blocked",
        expected_output="Repair wrapper availability or contract compatibility.",
        workspace_ref=f"workspace://{project_path}",
        resource_locks=[f"workspace:{project_path}"],
    )
    control_plane.work_items[partial_repair.work_item_id] = partial_repair
    store.put_work_item(partial_repair)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"nuo": NuoRuntimeRepairRunner(control_plane=control_plane)},
        daemon_id="daemon-stale-clean-retest-test",
    )
    first_report = DaemonTickReport(
        daemon_id="daemon-stale-clean-retest-test",
        observed_at=NOW,
    )

    daemon._queue_nuo_clean_retest_for_partial_repair(
        mission_id=mission.mission_id,
        partial_repair=partial_repair,
        evidence_refs=["artifact-runtime-observation-before"],
        priority=95,
        report=first_report,
    )

    retest_id = first_report.created_work_item_ids[0]
    stale_retest = control_plane.work_items[retest_id].model_copy(
        update={
            "status": "waiting_human",
            "priority": 90,
            "recovery_refs": [partial_repair.work_item_id],
        }
    )
    control_plane.work_items[retest_id] = stale_retest
    store.put_work_item(stale_retest)
    second_report = DaemonTickReport(
        daemon_id="daemon-stale-clean-retest-test",
        observed_at=NOW,
    )

    daemon._queue_nuo_clean_retest_for_partial_repair(
        mission_id=mission.mission_id,
        partial_repair=partial_repair,
        evidence_refs=["artifact-runtime-observation-after"],
        priority=95,
        report=second_report,
    )

    recovered = InMemoryControlPlane(store=store)
    retest = recovered.work_items[retest_id]
    assert retest.status == "queued"
    assert retest.priority == 95
    assert "artifact-runtime-observation-after" in retest.recovery_refs
    assert second_report.recovered_work_item_ids == [retest_id]
    assert second_report.observation_followup_ids == [retest_id]


def test_daemon_recovers_orphan_waiting_human_clean_retest_without_closing_user_ticket(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    stale_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-orphan",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="waiting_human",
        priority=85,
        idempotency_key="nuo-clean-retest:work-nuo-retired-repair",
        expected_output="Clean retest workspace write access before asking an operator.",
        workspace_ref=f"workspace://{project_path}",
        resource_locks=[f"workspace:{project_path}"],
    )
    clean_retest_ticket = CollaborationTicket(
        ticket_id="ticket-clean-retest",
        mission_id=mission.mission_id,
        type="operator_action",
        role_needed="operator",
        why_needed="Workspace clean retest was previously blocked.",
        context_ref=stale_retest.work_item_id,
        risk_if_skipped="The machine retest may remain stale.",
        deadline=NOW + timedelta(hours=1),
        output_contract="Allow the clean retest to run.",
        auto_resolvable_by=[stale_retest.work_item_id],
    )
    product_decision_ticket = CollaborationTicket(
        ticket_id="ticket-product-direction",
        mission_id=mission.mission_id,
        type="user_decision",
        role_needed="product owner",
        why_needed="Choose the next product direction.",
        decision_options=["continue polish", "stop"],
        recommended_option="continue polish",
        context_ref="ctx-product-direction",
        risk_if_skipped="KUN may optimize the wrong product target.",
        deadline=NOW + timedelta(hours=1),
        output_contract="Choose the product direction.",
    )
    control_plane.work_items[stale_retest.work_item_id] = stale_retest
    store.put_work_item(stale_retest)
    control_plane.record_collaboration_ticket(clean_retest_ticket, actor="test")
    control_plane.record_collaboration_ticket(product_decision_ticket, actor="test")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={},
        daemon_id="daemon-orphan-clean-retest-test",
    )
    report = DaemonTickReport(
        daemon_id="daemon-orphan-clean-retest-test",
        observed_at=NOW,
    )

    daemon._recover_stale_waiting_human_nuo_clean_retests(
        mission_id=mission.mission_id,
        report=report,
    )

    recovered = InMemoryControlPlane(store=store)
    assert recovered.work_items[stale_retest.work_item_id].status == "queued"
    assert report.recovered_work_item_ids == [stale_retest.work_item_id]
    assert report.observation_followup_ids == [stale_retest.work_item_id]
    assert recovered.collaboration_tickets[clean_retest_ticket.ticket_id].status == "cancelled"
    assert (
        stale_retest.work_item_id
        in recovered.collaboration_tickets[clean_retest_ticket.ticket_id].resolution_refs
    )
    assert recovered.collaboration_tickets[product_decision_ticket.ticket_id].status == "open"


def test_daemon_does_not_retire_active_waiting_clean_retest_ticket(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    mission = mission.model_copy(update={"current_plan_version": "v1"})
    control_plane.missions[mission.mission_id] = mission
    store.put_mission(mission)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    plan = TaskPlan(
        plan_id="plan-active",
        mission_id=mission.mission_id,
        version=mission.current_plan_version,
        objective="Keep active operator blockers visible.",
        acceptance_criteria=["Active waiting human clean retest tickets remain visible."],
        constraints=[],
        assumptions=[],
        known_facts=[],
        unknowns=[],
        info_gaps=[],
        decomposition=[],
        worker_plan=[],
        evidence_plan=[],
        test_plan=[],
        rollback_plan=[],
        merge_plan=[],
        human_confirmation_points=[],
        risk_register=[],
    )
    waiting_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-active",
        mission_id=mission.mission_id,
        task_plan_version=mission.current_plan_version,
        type="retest",
        owner="nuo",
        status="waiting_human",
        priority=85,
        idempotency_key="nuo-clean-retest:work-nuo-active-repair",
        expected_output="Clean retest workspace write access before asking an operator.",
        workspace_ref=f"workspace://{project_path}",
        resource_locks=[f"workspace:{project_path}"],
    )
    clean_retest_ticket = CollaborationTicket(
        ticket_id="ticket-active-clean-retest",
        mission_id=mission.mission_id,
        type="operator_action",
        role_needed="operator_with_workspace_write_access",
        why_needed="Workspace clean retest is still blocked.",
        context_ref=waiting_retest.work_item_id,
        risk_if_skipped="The machine retest remains blocked.",
        deadline=NOW + timedelta(hours=1),
        output_contract="Restore write access or approve a writable workspace.",
        auto_resolvable_by=[waiting_retest.work_item_id],
    )
    control_plane.task_plans[plan.plan_id] = plan
    store.put_task_plan(plan)
    control_plane.work_items[waiting_retest.work_item_id] = waiting_retest
    store.put_work_item(waiting_retest)
    control_plane.record_collaboration_ticket(clean_retest_ticket, actor="test")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={},
        daemon_id="daemon-active-clean-retest-ticket-test",
    )
    report = DaemonTickReport(
        daemon_id="daemon-active-clean-retest-ticket-test",
        observed_at=NOW,
    )

    daemon._retire_superseded_collaboration_tickets(
        mission_id=mission.mission_id,
        observed_at=NOW,
        report=report,
    )

    recovered = InMemoryControlPlane(store=store)
    assert recovered.collaboration_tickets[clean_retest_ticket.ticket_id].status == "open"
    assert report.retired_collaboration_ticket_ids == []


def test_daemon_reopens_cancelled_waiting_clean_retest_ticket(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    missing_project_path = tmp_path / "missing-wordforge"
    waiting_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-still-blocked",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="waiting_human",
        priority=85,
        idempotency_key="nuo-clean-retest:work-nuo-still-blocked",
        expected_output="Clean retest workspace write access before asking an operator.",
        workspace_ref=f"workspace://{missing_project_path}",
        resource_locks=[f"workspace:{missing_project_path}"],
    )
    cancelled_ticket = CollaborationTicket(
        ticket_id="ticket-cancelled-clean-retest",
        mission_id=mission.mission_id,
        type="operator_action",
        role_needed="operator_with_workspace_write_access",
        why_needed="Workspace clean retest was previously blocked.",
        context_ref=waiting_retest.work_item_id,
        risk_if_skipped="The machine retest remains blocked.",
        deadline=NOW + timedelta(hours=1),
        output_contract="Restore write access or approve a writable workspace.",
        auto_resolvable_by=[waiting_retest.work_item_id],
        status="cancelled",
    )
    control_plane.work_items[waiting_retest.work_item_id] = waiting_retest
    store.put_work_item(waiting_retest)
    control_plane.record_collaboration_ticket(cancelled_ticket, actor="test")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={},
        daemon_id="daemon-reopen-clean-retest-ticket-test",
    )
    report = DaemonTickReport(
        daemon_id="daemon-reopen-clean-retest-ticket-test",
        observed_at=NOW,
    )

    daemon._recover_stale_waiting_human_nuo_clean_retests(
        mission_id=mission.mission_id,
        report=report,
    )

    recovered = InMemoryControlPlane(store=store)
    assert recovered.work_items[waiting_retest.work_item_id].status == "waiting_human"
    assert recovered.collaboration_tickets[cancelled_ticket.ticket_id].status == "open"
    assert report.created_collaboration_ticket_ids == [cancelled_ticket.ticket_id]


def test_daemon_requeues_failed_subject_after_existing_clean_retest(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    failed_subject = control_plane.work_items["work-daemon"].model_copy(update={"status": "failed"})
    partial_repair = WorkItem(
        work_item_id="work-nuo-work-daemon-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        status="partial",
        priority=95,
        idempotency_key="nuo-recovery:work-daemon:fix_wrapper:sandbox_permission_blocked",
        expected_output="Repair wrapper availability or contract compatibility.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-work-daemon-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        priority=95,
        expected_output="Nuo clean retest passed.",
        recovery_refs=[
            partial_repair.work_item_id,
            "artifact-nuo-clean-retest-work-daemon",
        ],
    )
    for item in (failed_subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-existing-clean-retest-requeue-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert "work-daemon" in report.recovered_work_item_ids
    assert recovered.work_items["work-daemon"].status == "queued"
    assert partial_repair.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[partial_repair.work_item_id].status == "done"
    assert (
        clean_retest.work_item_id in recovered.work_items[partial_repair.work_item_id].recovery_refs
    )
    assert clean_retest.work_item_id in recovered.work_items["work-daemon"].recovery_refs
    assert (
        "artifact-nuo-clean-retest-work-daemon" in recovered.work_items["work-daemon"].recovery_refs
    )


def test_daemon_requeues_browser_environment_failed_rainflow_subject_after_clean_retest(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    rainflow_subject = control_plane.work_items["work-daemon"].model_copy(
        update={
            "work_item_id": "work-rainflow-phase1-01-mixed-edit-repair",
            "status": "failed",
            "expected_output": "Produce RainFlow Phase 1 mixed-edit repair evidence.",
        }
    )
    control_plane.work_items.pop("work-daemon")
    failed_run = RunRecord(
        run_id="run-rainflow-browser-blocked",
        work_item_id=rainflow_subject.work_item_id,
        runner_type="agent",
        runner_identity="daemon-test-runner",
        started_at=NOW - timedelta(minutes=1),
        ended_at=NOW,
        exit_status="failed",
        failure_category="delivery_failure",
    )
    browser_gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-browser-blocked",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref=rainflow_subject.work_item_id,
        stage="workitem",
        task_type="product_development",
        rubric_version="kun-v6-runtime-task-v1",
        metric_pack_version="kun-v6-runtime-task-v1",
        north_star_verdict="fail",
        result_quality=0.28,
        speed=0.7,
        cost=0.75,
        risk=0.65,
        evidence_quality=0.2,
        collaboration_quality=0.72,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["local_browser_player_gate_blocked"],
        failure_category="delivery_failure",
        root_cause="Browser player gate was blocked by the local review environment.",
        responsibility_scope="kun_auto",
        confidence=0.55,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="kun_runtime_task_executed",
        created_by="kun-runtime-task-runner",
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete-abc",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        priority=95,
        idempotency_key=(
            "runtime-observation:nuo:"
            "work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete-abc"
        ),
        expected_output="Diagnose failed_work_recovery_incomplete for RainFlow.",
        recovery_refs=[rainflow_subject.work_item_id],
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-rainflow-browser-blocked",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        priority=95,
        expected_output="Nuo clean retest passed.",
        recovery_refs=[
            partial_repair.work_item_id,
            "artifact-nuo-clean-retest-rainflow-browser-blocked",
        ],
    )
    for item in (rainflow_subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    control_plane.runs[failed_run.run_id] = failed_run
    control_plane.gate_evaluations[browser_gate.gate_evaluation_id] = browser_gate
    store.put_run_record(failed_run)
    store.put_gate_evaluation(browser_gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-rainflow-browser-clean-retest-requeue-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert rainflow_subject.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[rainflow_subject.work_item_id].status == "queued"
    assert (
        clean_retest.work_item_id
        in recovered.work_items[rainflow_subject.work_item_id].recovery_refs
    )


def test_daemon_requeues_local_evidence_linkage_failed_rainflow_subject_after_clean_retest(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    rainflow_subject = control_plane.work_items["work-daemon"].model_copy(
        update={
            "work_item_id": "work-rainflow-03-assembly-and-creator-integration",
            "status": "failed",
            "expected_output": "Produce RainFlow assembly and creator integration evidence.",
        }
    )
    control_plane.work_items.pop("work-daemon")
    failed_run = RunRecord(
        run_id="run-rainflow-local-evidence-linkage",
        work_item_id=rainflow_subject.work_item_id,
        runner_type="agent",
        runner_identity="daemon-test-runner",
        started_at=NOW - timedelta(minutes=1),
        ended_at=NOW,
        exit_status="failed",
        failure_category="delivery_failure",
    )
    evidence_gate = GateEvaluation(
        gate_evaluation_id="gate-rainflow-local-evidence-linkage",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        subject_ref=rainflow_subject.work_item_id,
        stage="workitem",
        task_type="product_development",
        rubric_version="kun-v6-runtime-task-v1",
        metric_pack_version="kun-v6-runtime-task-v1",
        north_star_verdict="fail",
        result_quality=0.28,
        speed=0.7,
        cost=0.75,
        risk=0.65,
        evidence_quality=0.2,
        collaboration_quality=0.72,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["local_runtime_evidence_missing"],
        failure_category="delivery_failure",
        root_cause="Local RainFlow evidence files were not linked into the runtime gate.",
        responsibility_scope="kun_auto",
        confidence=0.55,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="kun_runtime_task_executed",
        created_by="kun-runtime-task-runner",
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete-def",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="nuo",
        status="partial",
        priority=95,
        idempotency_key=(
            "runtime-observation:nuo:"
            "work-nuo-observation-msn-rainflow-failed_work_recovery_incomplete-def"
        ),
        expected_output="Diagnose failed_work_recovery_incomplete for RainFlow.",
        recovery_refs=[rainflow_subject.work_item_id],
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-rainflow-local-evidence-linkage",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        priority=95,
        expected_output="Nuo clean retest passed.",
        recovery_refs=[
            partial_repair.work_item_id,
            "artifact-nuo-clean-retest-rainflow-local-evidence-linkage",
        ],
    )
    for item in (rainflow_subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    control_plane.runs[failed_run.run_id] = failed_run
    control_plane.gate_evaluations[evidence_gate.gate_evaluation_id] = evidence_gate
    store.put_run_record(failed_run)
    store.put_gate_evaluation(evidence_gate)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-rainflow-local-evidence-clean-retest-requeue-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert rainflow_subject.work_item_id in report.recovered_work_item_ids
    assert recovered.work_items[rainflow_subject.work_item_id].status == "queued"
    assert (
        clean_retest.work_item_id
        in recovered.work_items[rainflow_subject.work_item_id].recovery_refs
    )


def test_daemon_requeues_blocked_subject_with_existing_clean_retest_evidence(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    blocked_subject = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "blocked",
            "recovery_refs": ["artifact-nuo-clean-retest-work-daemon"],
        }
    )
    partial_repair = WorkItem(
        work_item_id="work-nuo-work-daemon-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="repair",
        owner="control-plane",
        status="partial",
        priority=95,
        idempotency_key="nuo-recovery:work-daemon:fix_wrapper:sandbox_permission_blocked",
        expected_output="Repair wrapper availability or contract compatibility.",
    )
    clean_retest = WorkItem(
        work_item_id="work-nuo-clean-retest-work-daemon-fix-wrapper",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="retest",
        owner="nuo",
        status="done",
        priority=95,
        expected_output="Nuo clean retest passed.",
        recovery_refs=[
            partial_repair.work_item_id,
            "artifact-nuo-clean-retest-work-daemon",
        ],
    )
    for item in (blocked_subject, partial_repair, clean_retest):
        control_plane.work_items[item.work_item_id] = item
        store.put_work_item(item)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-existing-clean-retest-blocked-requeue-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)

    assert "work-daemon" in report.recovered_work_item_ids
    assert recovered.work_items["work-daemon"].status == "queued"
    assert clean_retest.work_item_id in recovered.work_items["work-daemon"].recovery_refs


def test_daemon_queues_clean_retest_for_blocked_current_plan_work(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    project_path = tmp_path / "wordforge"
    project_path.mkdir()
    blocked_subject = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "blocked",
            "workspace_ref": str(project_path),
            "resource_locks": [f"workspace:{project_path}"],
            "expected_output": "Patch the project workspace and rerun the build after tool failure.",
        }
    )
    control_plane.work_items[blocked_subject.work_item_id] = blocked_subject
    store.put_work_item(blocked_subject)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "kun": StaticRunner(),
            "nuo": NuoRuntimeRepairRunner(control_plane=control_plane),
        },
        daemon_id="daemon-blocked-clean-retest-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)
    retest_ids = [
        work_item_id
        for work_item_id in report.created_work_item_ids
        if work_item_id.startswith("work-nuo-clean-retest-")
    ]

    assert len(retest_ids) == 1
    assert recovered.work_items[retest_ids[0]].status == "queued"
    assert blocked_subject.work_item_id in recovered.work_items[retest_ids[0]].recovery_refs

    daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=1)
    recovered_after_retest = InMemoryControlPlane(store=store)

    assert recovered_after_retest.work_items[retest_ids[0]].status == "done"
    assert recovered_after_retest.work_items["work-daemon"].status == "queued"
    assert any(
        ref.startswith("artifact-nuo-runtime-repair-runner-work-nuo-clean-retest")
        for ref in recovered_after_retest.work_items["work-daemon"].recovery_refs
    )


def test_daemon_queues_clean_retest_for_blocked_work_from_latest_failed_run(
    tmp_path,
) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    blocked_subject = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "blocked",
            "expected_output": "Needs follow-up after runner failure.",
        }
    )
    failed_run = RunRecord(
        run_id="run-blocked-tool-failure",
        work_item_id=blocked_subject.work_item_id,
        runner_type="agent",
        runner_identity="daemon-test-runner",
        started_at=NOW - timedelta(minutes=1),
        ended_at=NOW,
        exit_status="failed",
        failure_category="tool_failure",
    )
    control_plane.work_items[blocked_subject.work_item_id] = blocked_subject
    control_plane.runs[failed_run.run_id] = failed_run
    store.put_work_item(blocked_subject)
    store.put_run_record(failed_run)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "kun": StaticRunner(),
            "nuo": NuoRuntimeRepairRunner(control_plane=control_plane),
        },
        daemon_id="daemon-blocked-latest-run-clean-retest-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW, max_work_items=0)
    recovered = InMemoryControlPlane(store=store)
    retest_ids = [
        work_item_id
        for work_item_id in report.created_work_item_ids
        if work_item_id.startswith("work-nuo-clean-retest-")
    ]

    assert len(retest_ids) == 1
    assert recovered.work_items[retest_ids[0]].status == "queued"
    assert blocked_subject.work_item_id in recovered.work_items[retest_ids[0]].recovery_refs


def test_daemon_recovers_stale_running_work_after_restart(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path, retry_budget=1)
    control_plane.transition_mission(
        mission_id=mission.mission_id,
        target="running",
        actor="test",
        reason="simulate daemon-owned run before restart",
        subject_ref="work-daemon",
    )
    stale_item = control_plane.work_items["work-daemon"].model_copy(
        update={
            "status": "running",
            "lease": "lease-stale",
            "heartbeat": NOW - timedelta(minutes=20),
            "timeout": NOW - timedelta(minutes=1),
        }
    )
    stale_run = RunRecord(
        run_id="run-stale",
        work_item_id="work-daemon",
        runner_type="agent",
        runner_identity="daemon-test-runner",
        started_at=NOW - timedelta(minutes=20),
    )
    control_plane.work_items[stale_item.work_item_id] = stale_item
    control_plane.runs[stale_run.run_id] = stale_run
    store.put_work_item(stale_item)
    store.put_run_record(stale_run)

    recovered = InMemoryControlPlane(store=store)
    daemon = ControlPlaneDaemon(control_plane=recovered, daemon_id="daemon-test")
    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)
    after = InMemoryControlPlane(store=store)

    assert report.recovered_work_item_ids == ["work-daemon"]
    assert report.recovery_gate_refs
    assert report.no_runner_work_item_ids == ["work-daemon"]
    assert after.missions[mission.mission_id].status == "blocked"
    assert after.work_items["work-daemon"].status == "queued"
    assert after.work_items["work-daemon"].retry_budget == 0
    assert after.work_items["work-daemon"].lease is None
    assert after.runs["run-stale"].exit_status == "failed"
    assert after.runs["run-stale"].failure_category == "environment_failure"
    assert (
        after.gate_evaluations[report.recovery_gate_refs[0]].responsibility_scope == "environment"
    )


def test_daemon_releases_claimed_lease_when_worker_start_is_rejected(tmp_path) -> None:
    control_plane, store, _mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(control_plane=control_plane, daemon_id="daemon-test")
    work_item = control_plane.work_items["work-daemon"]
    holder_id = "lease:daemon-test:worker-1:work-daemon:20260519T090000Z"
    claimed = daemon._claim_work_item_lease(work_item=work_item, lease=holder_id, now=NOW)
    assert claimed is not None

    def reject_start(**_kwargs):
        raise ValueError("transition 'info_gap' -> 'running' is not allowed")

    control_plane.start_work_item_run = reject_start  # type: ignore[method-assign]
    prepared = _PreparedWorkItemRun(
        work_item=claimed,
        runner=StaticRunner(),
        slot=WorkerSlotSnapshot(
            slot_id="daemon-test-pool:1",
            worker_id="worker-1",
            machine_id="test-machine",
            status="running",
            mission_id=claimed.mission_id,
            work_item_id=claimed.work_item_id,
            runner_identity="daemon-test-runner",
        ),
        holder_id=holder_id,
        preserve_mission_status=False,
        preflight_failed_skill_ids=[],
        preflight_artifact_refs=[],
    )

    assert daemon._execute_prepared_work_item(prepared) is None

    recovered = FileControlPlaneStore(store.path).get_work_item("work-daemon")
    assert recovered is not None
    assert recovered.status == "queued"
    assert recovered.lease is None
    assert recovered.timeout is None


def test_daemon_loop_auto_wakes_until_idle_and_writes_periodic_progress(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-loop-test",
    )
    tick = 0

    def now_factory() -> datetime:
        nonlocal tick
        tick += 1
        return NOW + timedelta(seconds=tick)

    report = daemon.run_loop(
        mission_ids=[mission.mission_id],
        poll_interval_sec=0,
        stop_when_idle=True,
        idle_ticks_to_stop=1,
        sleeper=lambda _seconds: None,
        now_factory=now_factory,
    )
    recovered = InMemoryControlPlane(store=store)

    assert report.stopped_reason == "idle"
    assert report.tick_count == 2
    assert report.tick_reports[0].ran_work_item_ids == ["work-daemon"]
    assert report.tick_reports[1].ran_work_item_ids == []
    assert len(report.tick_reports[0].progress_artifact_refs) == 1
    assert len(report.tick_reports[1].progress_artifact_refs) == 1
    assert recovered.work_items["work-daemon"].status == "done"
    assert (
        len(
            [
                artifact
                for artifact in recovered.artifacts.values()
                if "daemon_progress" in artifact.supports
            ]
        )
        == 2
    )


def test_managed_daemon_loop_persists_service_state_until_idle(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-service-test",
    )
    tick = 0

    def now_factory() -> datetime:
        nonlocal tick
        tick += 1
        return NOW + timedelta(seconds=tick)

    report = daemon.run_managed_loop(
        config=DaemonServiceConfig(
            poll_interval_sec=5,
            stop_when_idle=True,
            idle_ticks_to_stop=1,
        ),
        state_store=state_store,
        mission_ids=[mission.mission_id],
        sleeper=lambda _seconds: None,
        now_factory=now_factory,
    )
    recovered = InMemoryControlPlane(store=store)
    final_state = FileDaemonServiceStateStore(state_store.path).load()

    assert report.stopped_reason == "idle"
    assert report.tick_count == 2
    assert final_state is not None
    assert final_state.status == "stopped"
    assert final_state.stopped_reason == "idle"
    assert final_state.tick_count == 2
    assert final_state.consecutive_idle_ticks == 1
    assert final_state.active_mission_ids == [mission.mission_id]
    assert final_state.last_heartbeat_at == NOW + timedelta(seconds=4)
    assert final_state.last_tick_progress_artifact_refs
    assert recovered.work_items["work-daemon"].status == "done"


def test_managed_daemon_loop_exposes_running_worker_state_during_long_tick(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    runner = ServiceHeartbeatProbeRunner(state_store)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="daemon-service-heartbeat-test",
    )

    report = daemon.run_managed_loop(
        config=DaemonServiceConfig(
            max_ticks=1,
            worker_heartbeat_interval_sec=0.01,
        ),
        state_store=state_store,
        mission_ids=[mission.mission_id],
        sleeper=lambda _seconds: None,
        now_factory=lambda: NOW,
    )
    observed = runner.observed_state
    final_state = FileDaemonServiceStateStore(state_store.path).load()

    assert report.tick_count == 1
    assert observed is not None
    assert observed.status == "running"
    assert observed.last_heartbeat_at is not None
    assert observed.last_tick_ran_work_item_ids == ["work-daemon"]
    assert observed.last_tick_worker_slots
    assert observed.last_tick_worker_slots[0].status == "running"
    assert observed.last_tick_worker_slots[0].work_item_id == "work-daemon"
    assert final_state is not None
    assert all(slot.status != "running" for slot in final_state.last_tick_worker_slots)


def test_daemon_service_state_detects_crash_stale_heartbeat() -> None:
    state = DaemonServiceState(
        daemon_id="daemon-stale-test",
        status="running",
        started_at=NOW - timedelta(hours=1),
        updated_at=NOW - timedelta(minutes=31),
        process_id=12345,
        last_heartbeat_at=NOW - timedelta(minutes=31),
    )

    assert state.is_stale(now=NOW, stale_after=timedelta(minutes=30)) is True

    stopped = state.model_copy(update={"status": "stopped", "stopped_at": NOW})
    assert stopped.is_stale(now=NOW + timedelta(days=1), stale_after=timedelta(minutes=30)) is False


def test_daemon_service_store_blocks_duplicate_start_and_claims_stale_service(
    tmp_path,
) -> None:
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    state_store.save(
        DaemonServiceState(
            daemon_id="daemon-service-test",
            status="running",
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW - timedelta(minutes=1),
            process_id=os.getpid(),
            last_heartbeat_at=NOW - timedelta(minutes=1),
        )
    )

    duplicate = state_store.claim_start(
        daemon_id="daemon-service-test",
        config=DaemonServiceConfig(stale_heartbeat_after_sec=1800),
        now=NOW,
        process_id=222,
    )

    assert duplicate.accepted is False
    assert duplicate.stale_previous is False
    assert duplicate.state is not None
    assert duplicate.state.process_id == os.getpid()


def test_daemon_managed_loop_exits_cleanly_when_duplicate_service_is_alive(tmp_path) -> None:
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    state_store.save(
        DaemonServiceState(
            daemon_id="daemon-service-test",
            status="running",
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW - timedelta(minutes=1),
            process_id=os.getpid(),
            last_heartbeat_at=NOW - timedelta(minutes=1),
        )
    )
    daemon = ControlPlaneDaemon(
        control_plane=InMemoryControlPlane(),
        daemon_id="daemon-service-test",
        runners_by_owner={},
    )

    report = daemon.run_managed_loop(
        config=DaemonServiceConfig(stale_heartbeat_after_sec=1800),
        state_store=state_store,
        mission_ids=[],
        sleeper=lambda _seconds: None,
        now_factory=lambda: NOW,
    )
    loaded = state_store.load()

    assert report.tick_count == 0
    assert report.stopped_reason == "idle"
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.process_id == os.getpid()


def test_daemon_service_store_claims_stale_service_after_duplicate_check(
    tmp_path,
) -> None:
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    state_store.request_stop(daemon_id="daemon-service-test", requested_by="operator", now=NOW)
    state_store.save(
        DaemonServiceState(
            daemon_id="daemon-service-test",
            status="running",
            started_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(minutes=45),
            process_id=111,
            last_heartbeat_at=NOW - timedelta(minutes=45),
        )
    )

    replacement = state_store.claim_start(
        daemon_id="daemon-service-test",
        config=DaemonServiceConfig(stale_heartbeat_after_sec=1800),
        now=NOW,
        process_id=333,
    )
    loaded = state_store.load()

    assert replacement.accepted is True
    assert replacement.stale_previous is True
    assert replacement.previous_state is not None
    assert loaded is not None
    assert loaded.status == "starting"
    assert loaded.process_id == 333
    assert loaded.last_heartbeat_at == NOW
    assert state_store.stop_requested(daemon_id="daemon-service-test") is True


def test_daemon_service_store_claims_dead_idle_process_without_waiting_for_stale_timeout(
    tmp_path,
) -> None:
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    state_store.save(
        DaemonServiceState(
            daemon_id="daemon-service-test",
            status="idle",
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW - timedelta(seconds=10),
            process_id=999_999_999,
            last_heartbeat_at=NOW - timedelta(seconds=10),
        )
    )

    replacement = state_store.claim_start(
        daemon_id="daemon-service-test",
        config=DaemonServiceConfig(stale_heartbeat_after_sec=1800),
        now=NOW,
        process_id=444,
    )
    loaded = state_store.load()

    assert replacement.accepted is True
    assert replacement.stale_previous is True
    assert "进程已不存在" in replacement.text
    assert loaded is not None
    assert loaded.status == "starting"
    assert loaded.process_id == 444


def test_managed_daemon_loop_consumes_durable_stop_request(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-service-test",
    )
    now_tick = 0
    stop_checks = 0

    def now_factory() -> datetime:
        nonlocal now_tick
        now_tick += 1
        return NOW + timedelta(seconds=now_tick)

    def stop_requested() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        if stop_checks > 1:
            state_store.request_stop(
                daemon_id="daemon-service-test",
                requested_by="operator",
                reason="maintenance",
                now=NOW + timedelta(seconds=stop_checks),
            )
        return state_store.stop_requested(daemon_id="daemon-service-test")

    report = daemon.run_managed_loop(
        config=DaemonServiceConfig(poll_interval_sec=0, max_ticks=5),
        state_store=state_store,
        mission_ids=[mission.mission_id],
        stop_requested=stop_requested,
        sleeper=lambda _seconds: None,
        now_factory=now_factory,
    )
    final_state = state_store.load()

    assert report.stopped_reason == "stop_requested"
    assert report.tick_count == 1
    assert final_state is not None
    assert final_state.status == "stopped"
    assert final_state.stopped_reason == "stop_requested"
    assert state_store.stop_requested(daemon_id="daemon-service-test") is True


def test_managed_daemon_loop_honors_existing_stop_request_before_tick(tmp_path) -> None:
    control_plane, _store, mission = _runtime(tmp_path)
    state_store = FileDaemonServiceStateStore(tmp_path / "daemon-service-state.json")
    state_store.request_stop(
        daemon_id="daemon-service-test",
        requested_by="operator",
        reason="launchd_shutdown",
        now=NOW,
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-service-test",
    )

    report = daemon.run_managed_loop(
        config=DaemonServiceConfig(poll_interval_sec=0, max_ticks=5),
        state_store=state_store,
        mission_ids=[mission.mission_id],
        stop_requested=lambda: state_store.stop_requested(daemon_id="daemon-service-test"),
        sleeper=lambda _seconds: None,
        now_factory=lambda: NOW,
    )
    final_state = state_store.load()

    assert report.stopped_reason == "stop_requested"
    assert report.tick_count == 0
    assert final_state is not None
    assert final_state.status == "stopped"
    assert final_state.stopped_reason == "stop_requested"
    assert state_store.stop_requested(daemon_id="daemon-service-test") is True


def test_daemon_skips_explicit_non_active_mission_with_queued_leftovers(tmp_path) -> None:
    control_plane, store, mission = _runtime(tmp_path)
    delivered = control_plane.missions[mission.mission_id].model_copy(
        update={"status": "delivering"}
    )
    control_plane.missions[mission.mission_id] = delivered
    store.put_mission(delivered)
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="daemon-delivered-leftover-test",
    )

    report = daemon.tick_once(mission_ids=[mission.mission_id], now=NOW)

    assert report.ran_work_item_ids == []
    assert control_plane.work_items["work-daemon"].status == "queued"
    assert control_plane.missions[mission.mission_id].status == "delivering"
