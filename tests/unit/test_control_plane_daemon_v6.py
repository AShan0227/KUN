from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from kun.control_plane import (
    CapabilityProfile,
    ControlPlaneDaemon,
    DaemonServiceConfig,
    DaemonServiceState,
    ExecutionContract,
    FileControlPlaneStore,
    FileDaemonServiceStateStore,
    InMemoryControlPlane,
    Mission,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
)
from kun.control_plane.productization import (
    ProductizationDogfoodRunner,
    build_productization_dogfood_mission,
    close_productization_collaboration_loop,
    distill_external_behavior_signals,
    materialize_external_behavior_distillation,
    submit_productization_dogfood_mission,
)

NOW = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "daemon-test-runner"

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        return WorkItemResult(status="done", summary="daemon executed ready work")


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
    assert recovered.work_items["work-daemon"].status == "done"
    assert len(recovered.runs) == 1
    assert next(iter(recovered.runs.values())).exit_status == "succeeded"
    assert report.progress_artifact_refs[0] in recovered.artifacts


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

    assert len(report.ran_work_item_ids) == 7
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
    assert after.missions[mission.mission_id].status == "queued"
    assert after.work_items["work-daemon"].status == "queued"
    assert after.work_items["work-daemon"].retry_budget == 0
    assert after.work_items["work-daemon"].lease is None
    assert after.runs["run-stale"].exit_status == "failed"
    assert after.runs["run-stale"].failure_category == "environment_failure"
    assert (
        after.gate_evaluations[report.recovery_gate_refs[0]].responsibility_scope == "environment"
    )


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
    assert final_state.last_heartbeat_at == NOW + timedelta(seconds=3)
    assert final_state.last_tick_progress_artifact_refs
    assert recovered.work_items["work-daemon"].status == "done"


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
    assert state_store.stop_requested(daemon_id="daemon-service-test") is False


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
    assert state_store.stop_requested(daemon_id="daemon-service-test") is False
