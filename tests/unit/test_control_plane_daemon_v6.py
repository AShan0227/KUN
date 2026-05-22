from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from kun.control_plane import (
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
    WorkingContext,
    WorkItem,
    WorkItemResult,
)
from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.daemon import _acceptance_rework_plan_version
from kun.control_plane.preflight import WorkItemPreflight
from kun.control_plane.productization import (
    ProductizationDogfoodRunner,
    build_productization_dogfood_mission,
    close_productization_collaboration_loop,
    distill_external_behavior_signals,
    materialize_external_behavior_distillation,
    submit_productization_dogfood_mission,
)
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger

NOW = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "daemon-test-runner"

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        return WorkItemResult(status="done", summary="daemon executed ready work")


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
    assert all(
        item.resource_locks == [f"workspace:{tmp_path / 'wordforge'}"]
        for item in created
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


def test_daemon_waits_for_acceptance_when_final_product_pressure_evidence_passes(
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
        constraints=["Do not reopen pressure after passed external game-feel evidence."],
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

    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert control_plane.missions[mission.mission_id].current_plan_version == plan.version
    assert len(report.created_collaboration_ticket_ids) == 1
    assert report.created_work_item_ids == []
    assert not any("open-acceptance-pressure" in gate for gate in report.recovery_gate_refs)

    legacy_pressure_gate = GateEvaluation(
        gate_evaluation_id="gate-wordforge-passed-evidence-open-acceptance-pressure",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref=report.created_collaboration_ticket_ids[0],
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
        thresholds={"result_quality": 0.95},
        hard_gate_failures=["human_acceptance_missing"],
        failure_category="delivery_failure",
        root_cause="Legacy pressure gate from before final product evidence passed.",
        responsibility_scope="kun_auto",
        confidence=0.88,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="open_acceptance_requires_continued_product_pressure",
        created_by="older-daemon",
    )
    control_plane.gate_evaluations[legacy_pressure_gate.gate_evaluation_id] = legacy_pressure_gate

    second_report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=NOW + timedelta(minutes=1),
        max_work_items=0,
        write_progress=False,
    )

    assert control_plane.missions[mission.mission_id].status == "awaiting_acceptance"
    assert control_plane.missions[mission.mission_id].current_plan_version == plan.version
    assert second_report.created_work_item_ids == []


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
        "work-nuo-observation-msn-daemon-failed_work_without_recovery-f04c52eae6f4",
        "work-qi-observation-msn-daemon-failed_work_without_recovery-recovery_v1-f04c52eae6f4",
    ]
    assert (
        recovered.work_items[
            "work-qi-observation-msn-daemon-failed_work_without_recovery-recovery_v1-f04c52eae6f4"
        ].owner
        == "qi"
    )
    assert (
        recovered.work_items[
            "work-nuo-observation-msn-daemon-failed_work_without_recovery-f04c52eae6f4"
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
