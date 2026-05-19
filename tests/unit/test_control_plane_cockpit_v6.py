from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from kun.control_plane import (
    ArtifactManifest,
    ArtifactRecord,
    CollaborationTicket,
    ControlPlaneDaemon,
    DaemonServiceState,
    ExecutionContract,
    GateEvaluation,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    build_task_cockpit_view,
)


def _runtime_with_mission(*, mission_id: str = "msn-cockpit-v6") -> InMemoryControlPlane:
    runtime = InMemoryControlPlane()
    mission = Mission(
        mission_id=mission_id,
        owner="customer",
        objective="Ship a long-running productization task without terminal babysitting.",
        task_type="product_development",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["User can see progress, risk, next step, and deliverables."],
        constraints=["Quality cannot be traded away for speed or cost."],
        risk_register=["Daemon heartbeat is required for unattended operation."],
        evidence_plan=["Attach gate, artifact, and daemon progress refs."],
        decomposition=["execute", "verify", "deliver"],
        worker_plan=["kun-control-plane"],
        merge_plan=["single delivery manifest"],
        test_plan=["unit and API tests"],
        rollback_plan=["requeue repair work item"],
        human_confirmation_points=["Ask user before high-risk external actions."],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version=plan.version,
        allowed_actions=["execute", "test", "report"],
        forbidden_actions=["silent_high_risk_external_action"],
    )
    context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version=plan.version,
        audience="user",
        scope="mission",
        summary="The cockpit must be readable without terminal logs.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
        risk_flags=plan.risk_register,
    )
    item = WorkItem(
        work_item_id=f"work-{mission_id}",
        mission_id=mission_id,
        task_plan_version=plan.version,
        type="execution",
        owner="kun",
        expected_output="Build a user-readable cockpit.",
    )
    runtime.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[item],
    )
    return runtime


class WaitingRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "waiting-runner"

    def __init__(self, ticket: CollaborationTicket) -> None:
        self.ticket = ticket

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        return WorkItemResult(
            status="waiting_human",
            summary="Need a user decision.",
            collaboration_tickets=[self.ticket],
        )


class DeliveryRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "delivery-runner"

    def run(self, work_item: WorkItem) -> WorkItemResult:
        artifact = ArtifactRecord(
            artifact_id="artifact-cockpit-delivery",
            kind="report",
            path_or_uri="control-plane://delivery/cockpit",
            content_hash="delivery-hash",
            created_by="kun",
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=["task_cockpit", "delivery"],
            source_quality="primary",
        )
        manifest = ArtifactManifest(
            manifest_id="manifest-cockpit-delivery",
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            kind="delivery",
            artifact_refs=[artifact.artifact_id],
            primary_artifact_ref=artifact.artifact_id,
            evidence_refs=[artifact.artifact_id],
            created_by="kun",
            content_hash="manifest-hash",
            supports_delivery=True,
        )
        gate = GateEvaluation(
            gate_evaluation_id="gate-cockpit-delivery",
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            subject_ref=work_item.work_item_id,
            stage="delivery",
            task_type="product_development",
            rubric_version="v6",
            metric_pack_version="v6",
            north_star_verdict="pass",
            result_quality=0.93,
            speed=0.7,
            cost=0.7,
            risk=0.2,
            evidence_quality=0.9,
            collaboration_quality=0.8,
            artifact_refs=[artifact.artifact_id],
            evidence_refs=[artifact.artifact_id],
            confidence=0.9,
            next_action="ready_to_deliver",
            next_state="delivering",
            created_by="kun-gate",
        )
        return WorkItemResult(
            status="done",
            summary="Cockpit delivered.",
            artifacts=[artifact],
            artifact_manifest=manifest,
            gate_evaluation=gate,
        )


@pytest.mark.unit
def test_task_cockpit_view_explains_ready_work_without_terminal_logs() -> None:
    runtime = _runtime_with_mission()

    cockpit = build_task_cockpit_view(runtime, "msn-cockpit-v6")

    assert cockpit.headline == "KUN 正在推进任务。"
    assert cockpit.progress.total == 1
    assert cockpit.progress.ready == 1
    assert cockpit.plan.acceptance_criteria
    assert cockpit.work_items[0].lane == "ready"
    assert cockpit.work_items[0].needs_attention is False
    assert cockpit.quality_gate.text.startswith("还没有质量门禁结论")
    assert cockpit.daemon.healthy is False
    assert "plan-msn-cockpit-v6" in cockpit.technical_refs


@pytest.mark.unit
def test_task_cockpit_view_surfaces_human_ticket_and_resume_contract() -> None:
    runtime = _runtime_with_mission(mission_id="msn-cockpit-human")
    ticket = CollaborationTicket(
        ticket_id="ticket-cockpit-approval",
        mission_id="msn-cockpit-human",
        type="approval",
        role_needed="customer",
        why_needed="Need approval before an external action.",
        decision_options=["approve", "pause"],
        recommended_option="approve",
        context_ref="work-msn-cockpit-human",
        risk_if_skipped="KUN may cross the user's intended boundary.",
        deadline=datetime.now(UTC) + timedelta(hours=2),
        resume_after_response=True,
        output_contract="Choose approve or pause.",
    )
    runtime.run_next_ready(mission_id="msn-cockpit-human", runner=WaitingRunner(ticket))

    cockpit = build_task_cockpit_view(runtime, "msn-cockpit-human")

    assert cockpit.headline == "需要人类确认后继续。"
    assert cockpit.collaboration.human_needed is True
    assert cockpit.collaboration.open_ticket_count == 1
    assert "approve" in cockpit.collaboration.next_human_action
    assert cockpit.work_items[0].lane == "waiting"
    assert cockpit.work_items[0].needs_attention is True
    assert cockpit.safe_to_continue is False


@pytest.mark.unit
def test_task_cockpit_view_shows_delivery_gate_artifacts_and_daemon_health() -> None:
    runtime = _runtime_with_mission(mission_id="msn-cockpit-delivery")
    runtime.run_next_ready(mission_id="msn-cockpit-delivery", runner=DeliveryRunner())
    ControlPlaneDaemon(control_plane=runtime).tick_once(
        mission_ids=["msn-cockpit-delivery"],
        max_work_items=0,
    )

    cockpit = build_task_cockpit_view(runtime, "msn-cockpit-delivery")

    assert cockpit.headline == "交付物已准备好验收。"
    assert cockpit.quality_gate.status == "pass"
    assert cockpit.quality_gate.result_quality == 0.93
    assert cockpit.artifacts.delivery_ready is True
    assert cockpit.artifacts.latest_delivery_manifest_ref == "manifest-cockpit-delivery"
    assert cockpit.artifacts.deliverables[0].path_or_uri == "control-plane://delivery/cockpit"
    assert cockpit.daemon.healthy is True
    assert cockpit.daemon.latest_progress_artifact_ref is not None


@pytest.mark.unit
def test_task_cockpit_view_uses_daemon_service_state_for_background_health() -> None:
    runtime = _runtime_with_mission(mission_id="msn-cockpit-daemon-state")
    state = DaemonServiceState(
        daemon_id="daemon-cockpit",
        status="running",
        started_at=datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 19, 9, 5, tzinfo=UTC),
        process_id=1234,
        tick_count=3,
        active_mission_ids=["msn-cockpit-daemon-state"],
        last_heartbeat_at=datetime(2026, 5, 19, 9, 5, tzinfo=UTC),
        next_wakeup_at=datetime(2026, 5, 19, 9, 6, tzinfo=UTC),
    )

    cockpit = build_task_cockpit_view(
        runtime,
        "msn-cockpit-daemon-state",
        daemon_service_state=state,
        now=datetime(2026, 5, 19, 9, 5, tzinfo=UTC),
    )

    assert cockpit.daemon.healthy is True
    assert cockpit.daemon.service_status == "running"
    assert cockpit.daemon.last_heartbeat_at == datetime(2026, 5, 19, 9, 5, tzinfo=UTC)
    assert cockpit.daemon.next_wakeup_at == datetime(2026, 5, 19, 9, 6, tzinfo=UTC)
    assert cockpit.daemon.stale is False
    assert "心跳正常" in cockpit.daemon.text


@pytest.mark.unit
def test_task_cockpit_view_warns_when_daemon_service_heartbeat_is_stale() -> None:
    runtime = _runtime_with_mission(mission_id="msn-cockpit-daemon-stale")
    state = DaemonServiceState(
        daemon_id="daemon-cockpit",
        status="running",
        started_at=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 19, 8, 20, tzinfo=UTC),
        process_id=1234,
        last_heartbeat_at=datetime(2026, 5, 19, 8, 20, tzinfo=UTC),
    )

    cockpit = build_task_cockpit_view(
        runtime,
        "msn-cockpit-daemon-stale",
        daemon_service_state=state,
        now=datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
    )

    assert cockpit.daemon.healthy is False
    assert cockpit.daemon.service_status == "running"
    assert cockpit.daemon.stale is True
    assert "心跳已过期" in cockpit.daemon.text
